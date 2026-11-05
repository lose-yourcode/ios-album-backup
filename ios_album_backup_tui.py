#!/usr/bin/env python3
"""
iOS Photos album backup TUI for Windows.

This tool avoids manual PTP/DCIM browsing. It reads Photos.sqlite to recover
Photos album structure, then exports matching DCIM media paths either directly
over AFC or from a local iOS backup.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "ios_album_backup_config.json"
INDEX_PATH = APP_DIR / "ios_album_backup_index.sqlite"
DEFAULT_BACKUP_ROOT = APP_DIR / "ios_device_backups"
DEFAULT_EXPORT_ROOT = APP_DIR / "album_exports"
DEFAULT_METADATA_ROOT = APP_DIR / "ios_photo_metadata"
IPHONE_EPOCH_OFFSET = 978307200


IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".heic",
    ".heif",
    ".png",
    ".gif",
    ".tif",
    ".tiff",
    ".dng",
    ".raw",
    ".bmp",
}
VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".avi"}
SIDECAR_EXTENSIONS = {".aae", ".mov"}


@dataclass
class ManifestRecord:
    file_id: str
    domain: str
    relative_path: str
    physical_path: Path
    size: int = 0

    @property
    def key(self) -> str:
        return f"{self.domain}:{normalize_relpath(self.relative_path)}"


@dataclass
class AFCRecord:
    remote_path: str
    relative_path: str
    required: bool = True
    size: int = 0

    @property
    def key(self) -> str:
        return f"AFC:{normalize_relpath(self.remote_path)}"


MediaRecord = ManifestRecord | AFCRecord


@dataclass
class Asset:
    pk: int
    directory: str
    filename: str
    kind: int | None = None
    hidden: int | None = None
    favorite: int | None = None
    trashed: int | None = None
    cloud_guid: str | None = None
    uuid: str | None = None
    date_created: float | None = None
    date_added: float | None = None

    @property
    def rel_media_path(self) -> str:
        return normalize_relpath(f"Media/{self.directory}/{self.filename}")

    @property
    def ext(self) -> str:
        return Path(self.filename).suffix.lower()

    @property
    def source_key(self) -> str:
        stable = self.cloud_guid or self.uuid
        if stable:
            return f"asset-guid:{stable}"
        return f"asset-path:{normalize_relpath(self.directory)}/{self.filename}"

    @property
    def display_name(self) -> str:
        return f"{self.filename} ({self.directory})"


@dataclass
class Album:
    pk: int
    title: str
    kind: int | None = None
    parent_id: int | None = None
    trashed: int | None = None
    path: str = ""
    synthetic: bool = False
    asset_pks: set[int] = field(default_factory=set)

    @property
    def export_name(self) -> str:
        return self.path or self.title or f"album_{self.pk}"


@dataclass
class PhotosLibrary:
    db_path: Path
    asset_table: str
    album_table: str | None
    join_table: str | None
    assets: dict[int, Asset]
    albums: list[Album]


class AppError(Exception):
    pass


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def normalize_relpath(value: str) -> str:
    return value.replace("\\", "/").strip("/")


def sanitize_part(value: str, fallback: str = "untitled") -> str:
    value = (value or "").strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or fallback


def safe_album_path(album_name: str) -> Path:
    parts = [sanitize_part(part, "untitled") for part in normalize_relpath(album_name).split("/") if part]
    return Path(*parts) if parts else Path("untitled")


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def apple_time_to_iso(value: float | None) -> str:
    if value is None:
        return ""
    try:
        timestamp = float(value) + IPHONE_EPOCH_OFFSET
        return dt.datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def prompt(message: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    value = input(f"{message}{suffix}: ").strip()
    return value if value else (default or "")


def press_enter() -> None:
    input("\n按 Enter 继续...")


def find_pymobiledevice3() -> str | None:
    return shutil.which("pymobiledevice3")


def run_streaming(command: list[str]) -> int:
    print("\n运行命令：")
    print(" ".join(f'"{part}"' if " " in part else part for part in command))
    print()
    try:
        process = subprocess.Popen(command)
        return process.wait()
    except FileNotFoundError:
        return 127


def list_connected_devices() -> None:
    exe = find_pymobiledevice3()
    if not exe:
        print_missing_pymobiledevice3()
        return
    result = subprocess.run([exe, "usbmux", "list"], text=True)
    if result.returncode != 0:
        print("没有成功列出设备。请确认 iPhone 已解锁，并点过“信任此电脑”。")


def print_missing_pymobiledevice3() -> None:
    print("没有找到 pymobiledevice3。")
    print("请先在 Windows PowerShell 里安装：")
    print("  py -m pip install -U pymobiledevice3")
    print("如果 py 不可用，试：")
    print("  python -m pip install -U pymobiledevice3")


def create_ios_backup(backup_root: Path, metadata_root: Path) -> tuple[str, Path] | None:
    exe = find_pymobiledevice3()
    if not exe:
        print_missing_pymobiledevice3()
        return None
    backup_root.mkdir(parents=True, exist_ok=True)
    print("\n选择 iOS 备份类型：")
    print("1. 仅相簿元数据：直接读取 PhotoData/Photos.sqlite，用于查看相簿结构（推荐先用这个）")
    print("2. 含媒体逻辑备份：备用离线方案，先把可备份媒体放到本地，再按相簿导出")
    mode = prompt("选择", "1")
    if mode == "1":
        print("\n准备直接读取 iOS 相簿元数据（不是媒体/App 备份）。")
        print("这个模式通常只需要几秒到几十秒；导出照片/视频时保持手机连接即可。")
    elif mode == "2":
        command = [exe, "backup2", "backup", "--full", str(backup_root)]
        print("\n准备创建 iOS 含媒体逻辑备份（备用离线方案，不是整机 NAND 镜像）。")
    else:
        print("已取消。")
        return None
    print("手机需要：USB 连接、解锁、信任此电脑；过程中可能要求输入锁屏密码。")
    print("如果你启用了 iCloud 照片并且手机是“优化 iPhone 储存空间”，部分原片可能不在本机备份里。")
    answer = prompt("开始？输入 y 继续", "n").lower()
    if answer != "y":
        return None
    if mode == "1":
        return ("metadata", pull_photos_metadata(metadata_root, exe))
    code = run_streaming(command)
    if code == 0:
        print("\n备份完成。")
        return ("backup", backup_root)
    print(f"\n备份命令退出码：{code}")
    return None


def pull_photos_metadata(metadata_root: Path, exe: str) -> Path:
    snapshot_dir = metadata_root / now_stamp()
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    files = [
        "PhotoData/Photos.sqlite",
        "PhotoData/Photos.sqlite-wal",
        "PhotoData/Photos.sqlite-shm",
    ]
    for remote in files:
        local = snapshot_dir / Path(remote).name
        code = run_streaming([exe, "afc", "pull", "-i", remote, str(local)])
        if remote.endswith("Photos.sqlite") and code != 0:
            raise AppError("没有成功读取 PhotoData/Photos.sqlite。请确认手机已解锁并信任此电脑。")
    sqlite_path = snapshot_dir / "Photos.sqlite"
    if not sqlite_path.exists():
        raise AppError("没有找到拉取后的 Photos.sqlite。")
    print(f"\n相簿元数据已保存：{snapshot_dir}")
    return snapshot_dir


def find_backup_candidates(root: Path) -> list[Path]:
    if not root.exists():
        return []
    candidates: list[Path] = []
    if (root / "Manifest.db").exists():
        candidates.append(root)
    for manifest in root.glob("*/Manifest.db"):
        candidates.append(manifest.parent)
    for manifest in root.glob("*/*/Manifest.db"):
        candidates.append(manifest.parent)
    unique = sorted(set(candidates), key=lambda p: (p / "Manifest.db").stat().st_mtime, reverse=True)
    return unique


def find_metadata_candidates(root: Path) -> list[Path]:
    if not root.exists():
        return []
    candidates: list[Path] = []
    if (root / "Photos.sqlite").exists():
        candidates.append(root)
    for sqlite_path in root.glob("*/Photos.sqlite"):
        candidates.append(sqlite_path.parent)
    return sorted(set(candidates), key=lambda p: (p / "Photos.sqlite").stat().st_mtime, reverse=True)


class BackupIndex:
    def __init__(self, backup_path: Path, records: list[ManifestRecord]) -> None:
        self.backup_path = backup_path
        self.records = records
        self.by_key: dict[tuple[str, str], ManifestRecord] = {}
        self.by_relpath: dict[str, list[ManifestRecord]] = {}
        self.by_directory: dict[tuple[str, str], list[ManifestRecord]] = {}
        for record in records:
            rel = normalize_relpath(record.relative_path)
            self.by_key[(record.domain, rel)] = record
            self.by_relpath.setdefault(rel.lower(), []).append(record)
            directory = str(Path(rel).parent).replace("\\", "/").lower()
            filename = Path(rel).name.lower()
            self.by_directory.setdefault((directory, filename), []).append(record)

    @classmethod
    def load(cls, backup_path: Path) -> "BackupIndex":
        manifest = backup_path / "Manifest.db"
        if not manifest.exists():
            raise AppError(f"找不到 Manifest.db：{manifest}")
        try:
            conn = sqlite3.connect(str(manifest))
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT fileID, domain, relativePath FROM Files").fetchall()
        except sqlite3.DatabaseError as exc:
            raise AppError(
                "Manifest.db 不能直接读取。这个备份可能是加密备份；请先关闭本次备份加密，"
                "或用 pyiosbackup 解密后再让本工具读取解密目录。"
            ) from exc
        finally:
            try:
                conn.close()
            except Exception:
                pass

        records: list[ManifestRecord] = []
        for row in rows:
            file_id = row["fileID"]
            if not file_id:
                continue
            physical = backup_path / file_id[:2] / file_id
            if not physical.exists():
                flat = backup_path / file_id
                physical = flat if flat.exists() else physical
            size = physical.stat().st_size if physical.exists() else 0
            records.append(
                ManifestRecord(
                    file_id=file_id,
                    domain=row["domain"],
                    relative_path=row["relativePath"] or "",
                    physical_path=physical,
                    size=size,
                )
            )
        return cls(backup_path, records)

    def find_exact(self, domain: str, relpath: str) -> ManifestRecord | None:
        return self.by_key.get((domain, normalize_relpath(relpath)))

    def find_photos_sqlite(self) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
        candidates = [
            self.find_exact("CameraRollDomain", "Media/PhotoData/Photos.sqlite"),
            self.find_exact("MediaDomain", "Media/PhotoData/Photos.sqlite"),
        ]
        candidates.extend(
            record
            for record in self.records
            if normalize_relpath(record.relative_path).lower().endswith("photodata/photos.sqlite")
        )
        record = next((item for item in candidates if item and item.physical_path.exists()), None)
        if not record:
            raise AppError("备份里找不到 CameraRollDomain/Media/PhotoData/Photos.sqlite。")

        temp_dir = tempfile.TemporaryDirectory(prefix="ios_photos_sqlite_")
        temp_path = Path(temp_dir.name) / "Photos.sqlite"
        shutil.copy2(record.physical_path, temp_path)
        for suffix in ("-wal", "-shm"):
            sidecar = self.find_exact(record.domain, record.relative_path + suffix)
            if sidecar and sidecar.physical_path.exists():
                shutil.copy2(sidecar.physical_path, Path(temp_dir.name) / f"Photos.sqlite{suffix}")
        return temp_path, temp_dir

    def find_media_for_asset(self, asset: Asset) -> list[ManifestRecord]:
        direct_candidates = [
            self.find_exact("CameraRollDomain", asset.rel_media_path),
            self.find_exact("MediaDomain", asset.rel_media_path),
        ]
        rel_matches = self.by_relpath.get(asset.rel_media_path.lower(), [])
        records: list[ManifestRecord] = []
        for record in direct_candidates + rel_matches:
            if record and record.physical_path.exists() and record not in records:
                records.append(record)

        stem = Path(asset.filename).stem.lower()
        media_dir = str(Path(asset.rel_media_path).parent).replace("\\", "/").lower()
        for ext in SIDECAR_EXTENSIONS:
            if asset.ext == ext:
                continue
            related_name = f"{stem}{ext}".lower()
            for record in self.by_directory.get((media_dir, related_name), []):
                if record.physical_path.exists() and record not in records:
                    records.append(record)
        return records


class AFCMediaSource:
    def __init__(self, exe: str) -> None:
        self.exe = exe
        self.directory_cache: dict[str, set[str] | None] = {}

    def find_media_for_asset(self, asset: Asset) -> list[AFCRecord]:
        remote_dir = normalize_relpath(asset.directory)
        main_name = asset.filename
        records = [
            AFCRecord(
                remote_path=normalize_relpath(f"{remote_dir}/{main_name}"),
                relative_path=normalize_relpath(f"{remote_dir}/{main_name}"),
                required=True,
            )
        ]

        names = self._list_directory(remote_dir)
        if names is None:
            return records

        stem = Path(main_name).stem.lower()
        for ext in SIDECAR_EXTENSIONS:
            if asset.ext == ext:
                continue
            related = f"{stem}{ext}".lower()
            actual_name = next((name for name in names if name.lower() == related), None)
            if actual_name:
                records.append(
                    AFCRecord(
                        remote_path=normalize_relpath(f"{remote_dir}/{actual_name}"),
                        relative_path=normalize_relpath(f"{remote_dir}/{actual_name}"),
                        required=False,
                    )
                )
        return records

    def _list_directory(self, remote_dir: str) -> set[str] | None:
        if remote_dir in self.directory_cache:
            return self.directory_cache[remote_dir]
        result = subprocess.run(
            [self.exe, "afc", "ls", remote_dir],
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            self.directory_cache[remote_dir] = None
            return None
        names = {
            Path(line.strip().rstrip("/")).name
            for line in result.stdout.splitlines()
            if line.strip() and not line.strip().startswith("[")
        }
        self.directory_cache[remote_dir] = names
        return names

    def pull(self, record: AFCRecord, dest_path: Path) -> None:
        code = run_streaming([self.exe, "afc", "pull", "-i", record.remote_path, str(dest_path)])
        if code != 0 or not dest_path.exists():
            if dest_path.exists():
                dest_path.unlink()
            raise AppError(f"AFC 拉取失败：{record.remote_path}")


class BackupCatalog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def close(self) -> None:
        self.conn.close()

    def _ensure_schema(self) -> None:
        self.conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS media_backup (
                source_key TEXT PRIMARY KEY,
                asset_source_key TEXT NOT NULL,
                source_record_key TEXT NOT NULL,
                filename TEXT NOT NULL,
                created TEXT,
                size INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT,
                first_exported_at TEXT NOT NULL,
                last_exported_at TEXT NOT NULL,
                export_count INTEGER NOT NULL DEFAULT 1,
                last_album TEXT,
                last_path TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_media_backup_asset
                ON media_backup(asset_source_key);
            CREATE INDEX IF NOT EXISTS idx_media_backup_sha256
                ON media_backup(sha256);

            CREATE TABLE IF NOT EXISTS album_backup (
                album TEXT NOT NULL,
                source_key TEXT NOT NULL,
                first_exported_at TEXT NOT NULL,
                last_exported_at TEXT NOT NULL,
                export_count INTEGER NOT NULL DEFAULT 1,
                last_path TEXT,
                PRIMARY KEY (album, source_key),
                FOREIGN KEY (source_key) REFERENCES media_backup(source_key)
            );
            CREATE INDEX IF NOT EXISTS idx_album_backup_source
                ON album_backup(source_key);

            CREATE TABLE IF NOT EXISTS backup_runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                albums TEXT NOT NULL,
                copied INTEGER NOT NULL,
                duplicate_copied INTEGER NOT NULL,
                skipped INTEGER NOT NULL,
                missing INTEGER NOT NULL,
                csv_path TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def media_record(self, source_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM media_backup WHERE source_key = ?",
            (source_key,),
        ).fetchone()
        return dict(row) if row else None

    def album_record(self, album: str, source_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM album_backup WHERE album = ? AND source_key = ?",
            (album, source_key),
        ).fetchone()
        return dict(row) if row else None

    def record_success(self, entry: dict[str, Any]) -> None:
        source_key = entry["source_key"]
        exported_at = entry["exported_at"]
        sha256 = entry.get("sha256") or None
        self.conn.execute(
            """
            INSERT INTO media_backup (
                source_key, asset_source_key, source_record_key, filename, created,
                size, sha256, first_exported_at, last_exported_at, export_count,
                last_album, last_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
                asset_source_key = excluded.asset_source_key,
                source_record_key = excluded.source_record_key,
                filename = excluded.filename,
                created = COALESCE(NULLIF(excluded.created, ''), media_backup.created),
                size = excluded.size,
                sha256 = COALESCE(excluded.sha256, media_backup.sha256),
                last_exported_at = excluded.last_exported_at,
                export_count = media_backup.export_count + 1,
                last_album = excluded.last_album,
                last_path = excluded.last_path
            """,
            (
                source_key,
                entry["asset_source_key"],
                entry["source_record_key"],
                entry["filename"],
                entry.get("created") or "",
                int(entry.get("size") or 0),
                sha256,
                exported_at,
                exported_at,
                entry["album"],
                entry["exported_path"],
            ),
        )
        self.conn.execute(
            """
            INSERT INTO album_backup (
                album, source_key, first_exported_at, last_exported_at,
                export_count, last_path
            )
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(album, source_key) DO UPDATE SET
                last_exported_at = excluded.last_exported_at,
                export_count = album_backup.export_count + 1,
                last_path = excluded.last_path
            """,
            (
                entry["album"],
                source_key,
                exported_at,
                exported_at,
                entry["exported_path"],
            ),
        )
        self.conn.commit()

    def record_run(
        self,
        run_id: str,
        started_at: str,
        album_names: list[str],
        copied: int,
        duplicate_copied: int,
        skipped: int,
        missing: int,
        csv_path: Path,
    ) -> None:
        finished_at = dt.datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            """
            INSERT OR REPLACE INTO backup_runs (
                run_id, started_at, finished_at, albums, copied,
                duplicate_copied, skipped, missing, csv_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                started_at,
                finished_at,
                json.dumps(album_names, ensure_ascii=False),
                copied,
                duplicate_copied,
                skipped,
                missing,
                str(csv_path),
            ),
        )
        self.conn.commit()

    def summary(self) -> dict[str, Any]:
        media_count = self.conn.execute("SELECT COUNT(*) FROM media_backup").fetchone()[0]
        album_count = self.conn.execute("SELECT COUNT(DISTINCT album) FROM album_backup").fetchone()[0]
        album_rows = self.conn.execute("SELECT COUNT(*) FROM album_backup").fetchone()[0]
        run_count = self.conn.execute("SELECT COUNT(*) FROM backup_runs").fetchone()[0]
        duplicate_groups = self.conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT sha256
                FROM media_backup
                WHERE sha256 IS NOT NULL AND sha256 != ''
                GROUP BY sha256
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
        return {
            "media_count": media_count,
            "album_count": album_count,
            "album_rows": album_rows,
            "run_count": run_count,
            "duplicate_groups": duplicate_groups,
        }

    def recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM backup_runs
            ORDER BY finished_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def top_albums(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT album, COUNT(*) AS media_count, SUM(export_count) AS export_count
            FROM album_backup
            GROUP BY album
            ORDER BY album COLLATE NOCASE
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def duplicate_groups(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT sha256, COUNT(*) AS media_count, GROUP_CONCAT(filename, ' | ') AS filenames
            FROM media_backup
            WHERE sha256 IS NOT NULL AND sha256 != ''
            GROUP BY sha256
            HAVING COUNT(*) > 1
            ORDER BY media_count DESC, MIN(last_exported_at) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


class PhotosParser:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.tables = set(self._table_names())

    def close(self) -> None:
        self.conn.close()

    def parse(self) -> PhotosLibrary:
        asset_table = self._find_asset_table()
        album_table = self._find_album_table()
        assets = self._load_assets(asset_table)
        albums: list[Album] = []
        join_table: str | None = None
        if album_table:
            album_map = self._load_albums(album_table)
            join_table, pairs = self._find_album_asset_pairs(album_map.keys(), assets.keys())
            for album_pk, asset_pk in pairs:
                if album_pk in album_map and asset_pk in assets:
                    album_map[album_pk].asset_pks.add(asset_pk)
            self._fill_album_paths(album_map)
            albums = [
                album
                for album in album_map.values()
                if album.asset_pks and not truthy_int(album.trashed)
            ]

        synthetic = self._build_synthetic_albums(assets)
        all_albums = synthetic + sorted(albums, key=lambda item: item.export_name.lower())
        return PhotosLibrary(
            db_path=self.db_path,
            asset_table=asset_table,
            album_table=album_table,
            join_table=join_table,
            assets=assets,
            albums=all_albums,
        )

    def _table_names(self) -> list[str]:
        rows = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return [row[0] for row in rows]

    def _columns(self, table: str) -> dict[str, str]:
        rows = self.conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        return {row["name"].upper(): row["name"] for row in rows}

    def _find_asset_table(self) -> str:
        candidates: list[tuple[int, str]] = []
        for table in self.tables:
            cols = self._columns(table)
            score = 0
            if "Z_PK" in cols:
                score += 1
            if "ZFILENAME" in cols:
                score += 4
            if "ZDIRECTORY" in cols:
                score += 4
            if table.upper() == "ZASSET":
                score += 4
            if table.upper() == "ZGENERICASSET":
                score += 3
            if score >= 9:
                candidates.append((score, table))
        if not candidates:
            raise AppError("Photos.sqlite 里没有找到可识别的照片资产表（ZASSET/ZGENERICASSET）。")
        return sorted(candidates, reverse=True)[0][1]

    def _find_album_table(self) -> str | None:
        if "ZGENERICALBUM" in self.tables:
            return "ZGENERICALBUM"
        candidates: list[tuple[int, str]] = []
        for table in self.tables:
            cols = self._columns(table)
            score = 0
            if "Z_PK" in cols:
                score += 1
            if "ZTITLE" in cols:
                score += 5
            if "ZKIND" in cols:
                score += 2
            if "ALBUM" in table.upper():
                score += 2
            if score >= 7:
                candidates.append((score, table))
        return sorted(candidates, reverse=True)[0][1] if candidates else None

    def _select_expr(self, columns: dict[str, str], candidates: Iterable[str], alias: str) -> str:
        for candidate in candidates:
            if candidate.upper() in columns:
                return f'"{columns[candidate.upper()]}" AS "{alias}"'
        return f'NULL AS "{alias}"'

    def _load_assets(self, asset_table: str) -> dict[int, Asset]:
        cols = self._columns(asset_table)
        select = [
            '"Z_PK" AS "pk"',
            self._select_expr(cols, ["ZDIRECTORY"], "directory"),
            self._select_expr(cols, ["ZFILENAME"], "filename"),
            self._select_expr(cols, ["ZKIND", "ZMEDIAKIND"], "kind"),
            self._select_expr(cols, ["ZHIDDEN", "ZISWARNINGHIDDEN"], "hidden"),
            self._select_expr(cols, ["ZFAVORITE"], "favorite"),
            self._select_expr(cols, ["ZTRASHEDSTATE"], "trashed"),
            self._select_expr(cols, ["ZCLOUDASSETGUID", "ZCLOUDGUID"], "cloud_guid"),
            self._select_expr(cols, ["ZUUID"], "uuid"),
            self._select_expr(cols, ["ZDATECREATED", "ZCREATIONDATE"], "date_created"),
            self._select_expr(cols, ["ZADDDATE", "ZADDEDDATE"], "date_added"),
        ]
        rows = self.conn.execute(f'SELECT {", ".join(select)} FROM "{asset_table}"').fetchall()
        assets: dict[int, Asset] = {}
        for row in rows:
            directory = row["directory"]
            filename = row["filename"]
            if not directory or not filename:
                continue
            asset = Asset(
                pk=int(row["pk"]),
                directory=normalize_relpath(str(directory)),
                filename=str(filename),
                kind=optional_int(row["kind"]),
                hidden=optional_int(row["hidden"]),
                favorite=optional_int(row["favorite"]),
                trashed=optional_int(row["trashed"]),
                cloud_guid=optional_str(row["cloud_guid"]),
                uuid=optional_str(row["uuid"]),
                date_created=optional_float(row["date_created"]),
                date_added=optional_float(row["date_added"]),
            )
            if not truthy_int(asset.trashed):
                assets[asset.pk] = asset
        return assets

    def _load_albums(self, album_table: str) -> dict[int, Album]:
        cols = self._columns(album_table)
        parent_candidates = [
            "ZPARENTFOLDER",
            "ZPARENTALBUM",
            "ZPARENT",
            "ZFOLDER",
            "ZCONTAINER",
        ]
        select = [
            '"Z_PK" AS "pk"',
            self._select_expr(cols, ["ZTITLE", "ZNAME"], "title"),
            self._select_expr(cols, ["ZKIND"], "kind"),
            self._select_expr(cols, ["ZTRASHEDSTATE"], "trashed"),
            self._select_expr(cols, parent_candidates, "parent_id"),
        ]
        rows = self.conn.execute(f'SELECT {", ".join(select)} FROM "{album_table}"').fetchall()
        albums: dict[int, Album] = {}
        for row in rows:
            pk = int(row["pk"])
            title = optional_str(row["title"]) or f"未命名相簿_{pk}"
            album = Album(
                pk=pk,
                title=title,
                kind=optional_int(row["kind"]),
                parent_id=optional_int(row["parent_id"]),
                trashed=optional_int(row["trashed"]),
            )
            albums[pk] = album
        return albums

    def _find_album_asset_pairs(
        self, album_pks: Iterable[int], asset_pks: Iterable[int]
    ) -> tuple[str | None, list[tuple[int, int]]]:
        album_pk_set = set(album_pks)
        asset_pk_set = set(asset_pks)
        best_table: str | None = None
        best_pairs: list[tuple[int, int]] = []
        best_score = 0

        for table in self.tables:
            upper_table = table.upper()
            if not upper_table.startswith("Z_") or "ASSET" not in upper_table:
                continue
            cols = self._columns(table)
            album_cols = [name for upper, name in cols.items() if "ALBUM" in upper]
            asset_cols = [name for upper, name in cols.items() if "ASSET" in upper]
            if not album_cols or not asset_cols:
                continue
            for album_col in album_cols:
                for asset_col in asset_cols:
                    if album_col == asset_col:
                        continue
                    pairs = self._read_pair_table(table, album_col, asset_col)
                    score = sum(1 for a, b in pairs if a in album_pk_set and b in asset_pk_set)
                    if score > best_score:
                        best_score = score
                        best_table = table
                        best_pairs = [(a, b) for a, b in pairs if a in album_pk_set and b in asset_pk_set]

        return best_table, best_pairs

    def _read_pair_table(self, table: str, album_col: str, asset_col: str) -> list[tuple[int, int]]:
        try:
            rows = self.conn.execute(
                f'SELECT "{album_col}" AS album_pk, "{asset_col}" AS asset_pk FROM "{table}"'
            ).fetchall()
        except sqlite3.DatabaseError:
            return []
        pairs: list[tuple[int, int]] = []
        for row in rows:
            album_pk = optional_int(row["album_pk"])
            asset_pk = optional_int(row["asset_pk"])
            if album_pk is not None and asset_pk is not None:
                pairs.append((album_pk, asset_pk))
        return pairs

    def _fill_album_paths(self, albums: dict[int, Album]) -> None:
        cache: dict[int, str] = {}

        def build(album_id: int, seen: set[int]) -> str:
            if album_id in cache:
                return cache[album_id]
            album = albums[album_id]
            title = album.title
            if album.parent_id and album.parent_id in albums and album.parent_id not in seen:
                parent_path = build(album.parent_id, seen | {album_id})
                path = f"{parent_path}/{title}" if parent_path else title
            else:
                path = title
            cache[album_id] = path
            return path

        for album_id in albums:
            albums[album_id].path = build(album_id, set())

    def _build_synthetic_albums(self, assets: dict[int, Asset]) -> list[Album]:
        all_asset_pks = set(assets)
        synthetic: list[Album] = [
            Album(pk=-1, title="全部照片视频", path="全部照片视频", synthetic=True, asset_pks=all_asset_pks)
        ]
        hidden = {pk for pk, asset in assets.items() if truthy_int(asset.hidden)}
        if hidden:
            synthetic.append(Album(pk=-2, title="隐藏", path="隐藏", synthetic=True, asset_pks=hidden))
        favorites = {pk for pk, asset in assets.items() if truthy_int(asset.favorite)}
        if favorites:
            synthetic.append(Album(pk=-3, title="收藏", path="收藏", synthetic=True, asset_pks=favorites))
        videos = {pk for pk, asset in assets.items() if asset.ext in VIDEO_EXTENSIONS}
        if videos:
            synthetic.append(Album(pk=-4, title="视频", path="视频", synthetic=True, asset_pks=videos))
        images = {pk for pk, asset in assets.items() if asset.ext in IMAGE_EXTENSIONS}
        if images:
            synthetic.append(Album(pk=-5, title="照片", path="照片", synthetic=True, asset_pks=images))
        return synthetic


def optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def truthy_int(value: int | None) -> bool:
    return bool(value and value != 0)


def choose_backup(backup_root: Path) -> Path | None:
    candidates = find_backup_candidates(backup_root)
    if not candidates:
        print(f"没有在 {backup_root} 找到 iOS 备份。")
        manual = prompt("可以输入已有备份目录，或直接回车取消")
        if not manual:
            return None
        path = Path(manual).expanduser()
        return path if (path / "Manifest.db").exists() else None

    print("\n找到这些备份：")
    for index, path in enumerate(candidates, start=1):
        mtime = dt.datetime.fromtimestamp((path / "Manifest.db").stat().st_mtime)
        print(f"{index:>2}. {path}  ({mtime:%Y-%m-%d %H:%M:%S})")
    value = prompt("选择备份编号", "1")
    try:
        selected = int(value)
    except ValueError:
        return None
    if selected < 1 or selected > len(candidates):
        return None
    return candidates[selected - 1]


def choose_metadata(metadata_root: Path) -> Path | None:
    candidates = find_metadata_candidates(metadata_root)
    if not candidates:
        print(f"没有在 {metadata_root} 找到 Photos.sqlite 元数据快照。")
        manual = prompt("可以输入包含 Photos.sqlite 的目录，或直接回车取消")
        if not manual:
            return None
        path = Path(manual).expanduser()
        return path if (path / "Photos.sqlite").exists() else None

    print("\n找到这些元数据快照：")
    for index, path in enumerate(candidates, start=1):
        mtime = dt.datetime.fromtimestamp((path / "Photos.sqlite").stat().st_mtime)
        print(f"{index:>2}. {path}  ({mtime:%Y-%m-%d %H:%M:%S})")
    value = prompt("选择元数据编号", "1")
    try:
        selected = int(value)
    except ValueError:
        return None
    if selected < 1 or selected > len(candidates):
        return None
    return candidates[selected - 1]


def load_library_from_backup(backup_path: Path) -> tuple[BackupIndex, PhotosLibrary, tempfile.TemporaryDirectory[str]]:
    index = BackupIndex.load(backup_path)
    db_path, temp_dir = index.find_photos_sqlite()
    parser = PhotosParser(db_path)
    try:
        library = parser.parse()
    finally:
        parser.close()
    return index, library, temp_dir


def load_library_from_metadata(metadata_path: Path) -> PhotosLibrary:
    db_path = metadata_path / "Photos.sqlite" if metadata_path.is_dir() else metadata_path
    if not db_path.exists():
        raise AppError(f"找不到 Photos.sqlite：{db_path}")
    parser = PhotosParser(db_path)
    try:
        return parser.parse()
    finally:
        parser.close()


def make_afc_media_source() -> AFCMediaSource:
    exe = find_pymobiledevice3()
    if not exe:
        raise AppError("没有找到 pymobiledevice3，不能通过 AFC 直连导出手机文件。")
    return AFCMediaSource(exe)


def print_library_summary(library: PhotosLibrary, backup_path: Path) -> None:
    print("\n已加载来源：", backup_path)
    print("Photos 数据库：", library.db_path)
    print(f"资产表：{library.asset_table}")
    print(f"相簿表：{library.album_table or '未识别'}")
    print(f"相簿关系表：{library.join_table or '未识别'}")
    print(f"资产数量：{len(library.assets)}")
    print(f"可选分类/相簿：{len(library.albums)}")


def list_albums(library: PhotosLibrary) -> None:
    print("\n相簿列表：")
    for index, album in enumerate(library.albums, start=1):
        tag = "系统" if album.synthetic else "相簿"
        print(f"{index:>3}. [{tag}] {album.export_name}  ({len(album.asset_pks)} 项)")


def parse_selection(value: str, max_index: int) -> list[int]:
    value = value.strip().lower()
    if value in {"all", "a", "全部"}:
        return list(range(1, max_index + 1))
    result: set[int] = set()
    for part in re.split(r"[,\s]+", value):
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            try:
                start, end = int(start_text), int(end_text)
            except ValueError:
                continue
            for item in range(min(start, end), max(start, end) + 1):
                if 1 <= item <= max_index:
                    result.add(item)
        else:
            try:
                item = int(part)
            except ValueError:
                continue
            if 1 <= item <= max_index:
                result.add(item)
    return sorted(result)


def choose_duplicate_policy() -> str:
    print("\n重复处理策略：")
    print("1. 同一相簿里已经导出过就跳过；同一文件在别的相簿仍正常导出（推荐）")
    print("2. 只要以前任何相簿导出过，就跳过")
    print("3. 只要以前任何相簿导出过，仍复制，但文件名加 __DUP")
    value = prompt("选择", "1")
    return {"1": "album-skip", "2": "global-skip", "3": "global-mark"}.get(value, "album-skip")


def export_selected_albums(
    media_source: BackupIndex | AFCMediaSource,
    library: PhotosLibrary,
    export_root: Path,
) -> None:
    if not library.albums:
        print("没有可导出的相簿。")
        return
    list_albums(library)
    value = prompt("\n输入相簿编号，例如 1,3-5；输入 all 导出全部")
    selected_indexes = parse_selection(value, len(library.albums))
    if not selected_indexes:
        print("没有选择任何相簿。")
        return
    selected_albums = [library.albums[i - 1] for i in selected_indexes]
    policy = choose_duplicate_policy()
    export_root.mkdir(parents=True, exist_ok=True)
    run_id = now_stamp()
    started_at = dt.datetime.now().isoformat(timespec="seconds")
    run_date = dt.date.today().isoformat()
    run_log = export_root / f"export_log_{run_id}.csv"
    catalog = BackupCatalog(INDEX_PATH)

    copied = skipped = missing = duplicate_copied = 0
    log_rows: list[dict[str, Any]] = []

    try:
        print("\n开始导出。大视频计算哈希会花一点时间。")
        for album in selected_albums:
            album_dir = export_root / safe_album_path(album.export_name) / run_date
            album_dir.mkdir(parents=True, exist_ok=True)
            print(f"\n[{album.export_name}] {len(album.asset_pks)} 项 -> {album_dir}")
            for asset_pk in sorted(album.asset_pks):
                asset = library.assets.get(asset_pk)
                if not asset:
                    continue
                media_records = media_source.find_media_for_asset(asset)
                if not media_records:
                    missing += 1
                    log_rows.append(log_entry(run_id, album, asset, None, "", "missing", "备份里没有找到媒体文件"))
                    continue
                for record in media_records:
                    source_key = f"{asset.source_key}|{media_identity(record)}"
                    previous_album = catalog.album_record(album.export_name, source_key)
                    previous_global = catalog.media_record(source_key)
                    if previous_album:
                        skipped += 1
                        note = previous_note(previous_album, "同一相簿已导出过")
                        log_rows.append(
                            log_entry(
                                run_id,
                                album,
                                asset,
                                record,
                                previous_album.get("last_path") or "",
                                "skipped",
                                note,
                                source_key=source_key,
                            )
                        )
                        continue
                    if policy == "global-skip" and previous_global:
                        skipped += 1
                        note = previous_note(previous_global, "全局已导出过")
                        log_rows.append(
                            log_entry(
                                run_id,
                                album,
                                asset,
                                record,
                                previous_global.get("last_path") or "",
                                "skipped",
                                note,
                                source_key=source_key,
                            )
                        )
                        continue

                    mark_duplicate = policy == "global-mark" and previous_global is not None
                    dest_name = destination_filename(asset.filename, record.relative_path, mark_duplicate)
                    dest_path = unique_dest_path(album_dir / dest_name)
                    try:
                        sha256, size = export_media_record(media_source, record, dest_path)
                    except AppError as exc:
                        if isinstance(record, AFCRecord) and not record.required:
                            continue
                        missing += 1
                        log_rows.append(
                            log_entry(
                                run_id,
                                album,
                                asset,
                                record,
                                "",
                                "missing",
                                str(exc),
                                source_key=source_key,
                            )
                        )
                        continue
                    status = "duplicate-copied" if mark_duplicate else "copied"
                    if mark_duplicate:
                        duplicate_copied += 1
                    else:
                        copied += 1
                    note = previous_note(previous_global, "全局重复，已加 __DUP") if mark_duplicate else ""
                    entry = log_entry(
                        run_id,
                        album,
                        asset,
                        record,
                        str(dest_path),
                        status,
                        note,
                        sha256=sha256,
                        size=size,
                        source_key=source_key,
                    )
                    catalog.record_success(entry)
                    log_rows.append(entry)
        catalog.record_run(
            run_id,
            started_at,
            [album.export_name for album in selected_albums],
            copied,
            duplicate_copied,
            skipped,
            missing,
            run_log,
        )
    finally:
        catalog.close()

    write_export_log(run_log, log_rows)
    print("\n导出完成。")
    print(f"复制：{copied}，重复复制：{duplicate_copied}，跳过：{skipped}，缺失：{missing}")
    print(f"索引数据库：{INDEX_PATH}")
    print(f"本次 CSV：{run_log}")


def previous_note(record: dict[str, Any] | None, prefix: str) -> str:
    if not record:
        return prefix
    parts = [prefix]
    last_path = record.get("last_path")
    last_exported_at = record.get("last_exported_at")
    export_count = record.get("export_count")
    if last_exported_at:
        parts.append(f"上次：{last_exported_at}")
    if last_path:
        parts.append(f"路径：{last_path}")
    if export_count:
        parts.append(f"次数：{export_count}")
    return "；".join(parts)


def media_identity(record: MediaRecord) -> str:
    relpath = normalize_relpath(record.relative_path)
    if relpath.lower().startswith("media/"):
        relpath = relpath[6:]
    return f"media-path:{relpath}"


def destination_filename(asset_filename: str, source_relpath: str, mark_duplicate: bool) -> str:
    source_name = Path(source_relpath).name or asset_filename
    clean_name = sanitize_part(source_name, "media")
    path = Path(clean_name)
    if mark_duplicate:
        return f"{path.stem}__DUP{path.suffix}"
    return clean_name


def export_media_record(
    media_source: BackupIndex | AFCMediaSource,
    record: MediaRecord,
    dest_path: Path,
) -> tuple[str, int]:
    if isinstance(record, AFCRecord):
        if not isinstance(media_source, AFCMediaSource):
            raise AppError("AFC 媒体记录需要 AFC 来源。")
        media_source.pull(record, dest_path)
        return hash_file(dest_path)
    if not record.physical_path.exists():
        raise AppError(f"本地备份文件不存在：{record.relative_path}")
    return copy_with_hash(record.physical_path, dest_path)


def unique_dest_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem}__{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def copy_with_hash(source: Path, dest: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with source.open("rb") as src, dest.open("wb") as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
            dst.write(chunk)
            size += len(chunk)
    shutil.copystat(source, dest, follow_symlinks=True)
    return hasher.hexdigest(), size


def hash_file(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def log_entry(
    run_id: str,
    album: Album,
    asset: Asset,
    record: MediaRecord | None,
    exported_path: str,
    status: str,
    note: str,
    sha256: str = "",
    size: int | None = None,
    source_key: str | None = None,
) -> dict[str, Any]:
    source = record.key if record else f"{asset.directory}/{asset.filename}"
    return {
        "run_id": run_id,
        "exported_at": dt.datetime.now().isoformat(timespec="seconds"),
        "album": album.export_name,
        "asset_pk": asset.pk,
        "asset_source_key": asset.source_key,
        "source_key": source_key or "",
        "source_record_key": record.key if record else "",
        "source": source,
        "filename": asset.filename,
        "created": apple_time_to_iso(asset.date_created),
        "exported_path": exported_path,
        "size": size if size is not None else (record.size if record else 0),
        "sha256": sha256,
        "status": status,
        "note": note,
    }


def write_export_log(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fields = [
        "run_id",
        "exported_at",
        "album",
        "asset_pk",
        "asset_source_key",
        "source_key",
        "source_record_key",
        "source",
        "filename",
        "created",
        "exported_path",
        "size",
        "sha256",
        "status",
        "note",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def show_state_summary() -> None:
    catalog = BackupCatalog(INDEX_PATH)
    try:
        summary = catalog.summary()
        albums = catalog.top_albums()
        duplicates = catalog.duplicate_groups()
        recent_runs = catalog.recent_runs()
    finally:
        catalog.close()
    print("\n备份记录：")
    print(f"索引数据库：{INDEX_PATH}")
    print(f"唯一媒体：{summary['media_count']}")
    print(f"相簿-媒体索引：{summary['album_rows']}")
    print(f"涉及相簿：{summary['album_count']}")
    print(f"备份运行次数：{summary['run_count']}")
    print(f"内容哈希重复组：{summary['duplicate_groups']}")
    if albums:
        print("\n相簿：")
        for row in albums:
            print(f"  - {row['album']}：{row['media_count']} 个唯一媒体，累计导出 {row['export_count']} 次")
    if duplicates:
        print("\n内容完全相同的重复组（前 20 组）：")
        for row in duplicates:
            digest = row["sha256"][:12]
            print(f"  - {digest}...：{row['media_count']} 个文件，{row['filenames']}")
    if recent_runs:
        print("\n最近运行：")
        for row in recent_runs:
            print(
                f"  - {row['finished_at']}：复制 {row['copied']}，"
                f"重复复制 {row['duplicate_copied']}，跳过 {row['skipped']}，缺失 {row['missing']}"
            )


def make_delete_review(
    media_source: BackupIndex | AFCMediaSource,
    library: PhotosLibrary,
    export_root: Path,
) -> None:
    print("\n说明：这个功能不会删除 iPhone 上的原件。")
    print("原因是 iOS 没有通过 USB 暴露“按 Photos 相簿安全删除”的公开接口；直接删 AFC/DCIM 文件会让照片库数据库失配。")
    print("这里生成一份清单，方便你在手机 Photos App 内核对后手动删除。")
    list_albums(library)
    value = prompt("\n输入要生成清单的相簿编号，例如 1,3-5")
    selected_indexes = parse_selection(value, len(library.albums))
    if not selected_indexes:
        print("没有选择任何相簿。")
        return
    path = export_root / f"delete_review_{now_stamp()}.csv"
    rows: list[dict[str, Any]] = []
    for i in selected_indexes:
        album = library.albums[i - 1]
        for asset_pk in sorted(album.asset_pks):
            asset = library.assets.get(asset_pk)
            if not asset:
                continue
            records = media_source.find_media_for_asset(asset)
            rows.append(
                {
                    "album": album.export_name,
                    "asset_pk": asset.pk,
                    "filename": asset.filename,
                    "created": apple_time_to_iso(asset.date_created),
                    "phone_library_path": f"{asset.directory}/{asset.filename}",
                    "backup_sources": "; ".join(record.key for record in records),
                    "note": "请在 iPhone 照片 App 内手动确认并删除",
                }
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "album",
                "asset_pk",
                "filename",
                "created",
                "phone_library_path",
                "backup_sources",
                "note",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"已生成清单：{path}")


def load_config() -> dict[str, Any]:
    config = load_json(
        CONFIG_PATH,
        {
            "backup_root": str(DEFAULT_BACKUP_ROOT),
            "export_root": str(DEFAULT_EXPORT_ROOT),
            "metadata_root": str(DEFAULT_METADATA_ROOT),
        },
    )
    config.setdefault("backup_root", str(DEFAULT_BACKUP_ROOT))
    config.setdefault("export_root", str(DEFAULT_EXPORT_ROOT))
    config.setdefault("metadata_root", str(DEFAULT_METADATA_ROOT))
    return config


def settings_menu(config: dict[str, Any]) -> dict[str, Any]:
    print("\n当前设置：")
    print(f"1. iOS 备份目录：{config['backup_root']}")
    print(f"2. 相簿导出目录：{config['export_root']}")
    print(f"3. 相簿元数据目录：{config['metadata_root']}")
    value = prompt("输入要修改的编号，或回车返回")
    if value == "1":
        new_path = prompt("新的 iOS 备份目录", config["backup_root"])
        config["backup_root"] = str(Path(new_path).expanduser())
    elif value == "2":
        new_path = prompt("新的相簿导出目录", config["export_root"])
        config["export_root"] = str(Path(new_path).expanduser())
    elif value == "3":
        new_path = prompt("新的相簿元数据目录", config["metadata_root"])
        config["metadata_root"] = str(Path(new_path).expanduser())
    save_json(CONFIG_PATH, config)
    return config


def main() -> int:
    config = load_config()
    media_source: BackupIndex | AFCMediaSource | None = None
    library: PhotosLibrary | None = None
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    loaded_source_path: Path | None = None
    loaded_source_kind = "未加载"

    while True:
        backup_root = Path(config["backup_root"]).expanduser()
        export_root = Path(config["export_root"]).expanduser()
        metadata_root = Path(config["metadata_root"]).expanduser()
        print("\n" + "=" * 64)
        print("iOS 相簿备份管理 TUI")
        print("=" * 64)
        print(f"备份目录：{backup_root}")
        print(f"元数据目录：{metadata_root}")
        print(f"导出目录：{export_root}")
        print(f"当前来源：{loaded_source_kind} - {loaded_source_path or '未加载'}")
        print("\n1. 检查 USB iPhone 连接")
        print("2. 获取/刷新数据（仅相簿元数据或含媒体备份）")
        print("3. 选择并加载现有元数据/备份")
        print("4. 查看相簿/分类")
        print("5. 按相簿导出原始文件")
        print("6. 查看导出记录/重复统计")
        print("7. 生成待删除清单（不直接删除手机）")
        print("8. 设置目录")
        print("0. 退出")
        choice = prompt("选择")

        try:
            if choice == "1":
                list_connected_devices()
                press_enter()
            elif choice == "2":
                created = create_ios_backup(backup_root, metadata_root)
                if created and created[0] == "metadata":
                    if temp_dir:
                        temp_dir.cleanup()
                        temp_dir = None
                    media_source = make_afc_media_source()
                    library = load_library_from_metadata(created[1])
                    loaded_source_path = created[1]
                    loaded_source_kind = "相簿元数据（AFC直连）"
                    print_library_summary(library, created[1])
                elif created and created[0] == "backup":
                    selected = choose_backup(backup_root)
                    if selected:
                        if temp_dir:
                            temp_dir.cleanup()
                        media_source, library, temp_dir = load_library_from_backup(selected)
                        loaded_source_path = selected
                        loaded_source_kind = "含媒体备份"
                        print_library_summary(library, selected)
                press_enter()
            elif choice == "3":
                print("\n1. 加载相簿元数据快照")
                print("2. 加载含媒体 iOS 备份")
                load_choice = prompt("选择", "1")
                if load_choice == "1":
                    selected = choose_metadata(metadata_root)
                    if selected:
                        if temp_dir:
                            temp_dir.cleanup()
                            temp_dir = None
                        media_source = make_afc_media_source()
                        library = load_library_from_metadata(selected)
                        loaded_source_path = selected
                        loaded_source_kind = "相簿元数据（AFC直连）"
                        print_library_summary(library, selected)
                    else:
                        print("没有加载元数据。")
                elif load_choice == "2":
                    selected = choose_backup(backup_root)
                    if selected:
                        if temp_dir:
                            temp_dir.cleanup()
                        media_source, library, temp_dir = load_library_from_backup(selected)
                        loaded_source_path = selected
                        loaded_source_kind = "含媒体备份"
                        print_library_summary(library, selected)
                    else:
                        print("没有加载备份。")
                else:
                    print("没有加载任何来源。")
                press_enter()
            elif choice == "4":
                require_library(library)
                list_albums(library)
                press_enter()
            elif choice == "5":
                require_library(library)
                require_media_source(media_source)
                export_selected_albums(media_source, library, export_root)
                press_enter()
            elif choice == "6":
                show_state_summary()
                press_enter()
            elif choice == "7":
                require_library(library)
                require_media_source(media_source)
                make_delete_review(media_source, library, export_root)
                press_enter()
            elif choice == "8":
                config = settings_menu(config)
            elif choice == "0":
                break
            else:
                print("无效选择。")
        except AppError as exc:
            print(f"\n错误：{exc}")
            press_enter()
        except KeyboardInterrupt:
            print("\n已中断当前操作。")
            press_enter()

    if temp_dir:
        temp_dir.cleanup()
    return 0


def require_library(library: PhotosLibrary | None) -> None:
    if library is None:
        raise AppError("请先获取或加载相簿元数据/备份。")


def require_media_source(source: BackupIndex | AFCMediaSource | None) -> None:
    if source is None:
        raise AppError("请先加载相簿元数据（AFC直连）或含媒体备份。")


if __name__ == "__main__":
    raise SystemExit(main())

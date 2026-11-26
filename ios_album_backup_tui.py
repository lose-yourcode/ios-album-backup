#!/usr/bin/env python3
"""
iOS Photos album backup TUI for Windows (v1.2).

This tool avoids manual PTP/DCIM browsing. It reads Photos.sqlite to recover
Photos album structure, then exports matching DCIM media paths directly over
AFC (in-process, single USB or Wi-Fi connection).

Copyright (C) 2026  lose-yourcode

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import csv
import datetime as dt
import hashlib
import json
import os
import plistlib
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable


__version__ = "1.2.0"


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "ios_album_backup_config.json"
DEFAULT_INDEX_PATH = APP_DIR / "ios_album_backup_index.sqlite"
DEFAULT_EXPORT_ROOT = APP_DIR / "album_exports"
DEFAULT_METADATA_ROOT = APP_DIR / "ios_photo_metadata"
IPHONE_EPOCH_OFFSET = 978307200

CONNECTION_MODE_AUTO = "auto"
CONNECTION_MODE_WIFI = "wifi"
CONNECTION_MODE_USB = "usb"
CONNECTION_MODE_TCP = "tcp"
VALID_CONNECTION_MODES = (
    CONNECTION_MODE_AUTO,
    CONNECTION_MODE_WIFI,
    CONNECTION_MODE_USB,
    CONNECTION_MODE_TCP,
)
CONNECTION_MODE_LABELS = {
    CONNECTION_MODE_AUTO: "自动（USB 优先，未插线自动走 Wi-Fi）",
    CONNECTION_MODE_WIFI: "仅 Wi-Fi 无线连接",
    CONNECTION_MODE_USB: "仅 USB 有线连接",
    CONNECTION_MODE_TCP: "手动 Wi-Fi IP 直连（TCP）",
}

# 与 pymobiledevice3 的 MAXIMUM_READ_SIZE 一致：单次 AFC 读取 4MB。
CHUNK_SIZE = 4 * 1024 * 1024
# 导出索引每累计 N 个文件提交一次 SQL，避免逐文件 fsync。
SQL_COMMIT_EVERY = 32
# 导出 CSV 每累计 N 行落盘一次，中断时已完成部分仍在。
CSV_FLUSH_EVERY = 20
# 周期性汇总行的间隔（按资产数）。逐文件的进度在拉取时实时显示。
PROGRESS_EVERY = 50


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
class AFCRecord:
    remote_path: str
    relative_path: str
    required: bool = True
    size: int = 0

    @property
    def key(self) -> str:
        return f"AFC:{normalize_relpath(self.remote_path)}"


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


class ConnectionLostError(AppError):
    """底层 USB/Wi-Fi 连接断开且重连失败。"""
    pass


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


# 多线程共用控制台：后台拉取线程和主线程都打印进度行，加锁防止半行串写。
PRINT_LOCK = threading.Lock()


def human_size(num_bytes: float) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}GB"


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
    try:
        input("\n按 Enter 继续...")
    except EOFError:
        pass
    except KeyboardInterrupt:
        print()


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


def print_missing_pymobiledevice3() -> None:
    print("没有找到 pymobiledevice3。")
    print("请先在 Windows PowerShell 里安装：")
    print("  py -m pip install -U pymobiledevice3")
    print("如果 py 不可用，试：")
    print("  python -m pip install -U pymobiledevice3")


# ============================================================================
# pymobiledevice3 集成：进程内 AFC（USB / Wi-Fi）+ 命令行 AFC（兜底）
# ============================================================================
# v1.0 起导出直接使用 pymobiledevice3 的 Python 接口，全程复用一条连接；
# v1.2 起支持通过 Wi-Fi 无线连接（usbmux Network / Bonjour mobdev2 / 手动 IP TCP）。


def _import_pmd3() -> SimpleNamespace:
    from pymobiledevice3 import exceptions as pmd3_exceptions
    from pymobiledevice3 import usbmux
    from pymobiledevice3.common import get_home_folder
    from pymobiledevice3.lockdown import TcpLockdownClient, create_using_tcp, create_using_usbmux
    from pymobiledevice3.osu.os_utils import get_os_utils
    from pymobiledevice3.pair_records import get_preferred_pair_record
    from pymobiledevice3.services.afc import AfcService

    try:
        from pymobiledevice3.bonjour import browse_mobdev2
    except ImportError:
        browse_mobdev2 = None

    return SimpleNamespace(
        exceptions=pmd3_exceptions,
        usbmux=usbmux,
        TcpLockdownClient=TcpLockdownClient,
        create_using_usbmux=create_using_usbmux,
        create_using_tcp=create_using_tcp,
        browse_mobdev2=browse_mobdev2,
        get_home_folder=get_home_folder,
        get_preferred_pair_record=get_preferred_pair_record,
        get_os_utils=get_os_utils,
        AfcService=AfcService,
    )


def friendly_afc_connect_error(exc: Exception) -> str:
    known = {
        "NoDeviceConnectedError": "没有检测到 iPhone。请插上 USB 数据线，或确认已开启 Wi-Fi 同步且处于同一局域网。",
        "ConnectionFailedToUsbmuxdError": "无法连接本机 Apple Mobile Device Service (usbmuxd)。如需 USB 或 usbmux Wi-Fi，请确认已安装 iTunes / Apple Devices；也可使用菜单 1 的手动 Wi-Fi IP 直连。",
        "NotTrustedError": "手机还没有信任此电脑。请先插 USB 解锁 iPhone，在弹窗里点“信任”并输入锁屏密码。",
        "NotPairedError": "设备尚未与本电脑完成配对。请先插一次 USB 线点“信任此电脑”。",
        "PairingDialogResponsePendingError": "请在 iPhone 上点“信任此电脑”弹窗，然后再试一次。",
        "PasswordRequiredError": "需要先在手机上输入锁屏密码完成配对或解锁。",
        "InvalidServiceError": "手机拒绝了 AFC 服务请求。请解锁屏幕后重试（重启后需至少解锁一次）。",
        "StartServiceError": "无法在手机上启动 AFC 服务。请解锁屏幕后重试。",
        "ConnectionFailedError": "连接 iPhone 失败，请检查 USB 线或 Wi-Fi 网络是否畅通。",
        "ConnectionTerminatedError": "与 iPhone 的连接已断开。",
    }
    message = known.get(type(exc).__name__)
    if message:
        return message
    return f"无法连接 iPhone 的 AFC 服务：{type(exc).__name__}: {exc}"


@dataclass
class DiscoveredEndpoint:
    transport_kind: str  # "usb" | "wifi-usbmux" | "wifi-bonjour" | "wifi-tcp"
    transport_label: str
    name: str
    version: str
    udid: str = ""
    paired: bool = False
    wifi_enabled: bool | None = None
    serial: str | None = None
    host: str | None = None
    error: str | None = None

    @property
    def is_usb(self) -> bool:
        return self.transport_kind == "usb"

    @property
    def is_wifi(self) -> bool:
        return self.transport_kind != "usb"


def _normalize_mac(value: str | None) -> str:
    return (value or "").strip().lower().replace(":", "").replace("-", "")


def _normalize_udid(value: str | None) -> str:
    return (value or "").strip().lower().replace("-", "")


def _load_pair_records(pmd3: SimpleNamespace) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """从 pymobiledevice3 缓存与 Apple Lockdown 目录读取配对记录（仅内存使用，不落盘）。"""
    by_udid: dict[str, dict[str, Any]] = {}
    by_mac: dict[str, dict[str, Any]] = {}
    folders: list[Path] = []
    for getter in (pmd3.get_home_folder, lambda: pmd3.get_os_utils().pair_record_path):
        try:
            folder = getter()
        except Exception:
            continue
        if folder and folder not in folders and folder.exists():
            folders.append(folder)

    for folder in folders:
        try:
            files = list(folder.glob("*.plist"))
        except OSError:
            continue
        for file in files:
            if file.name.startswith("remote_"):
                continue
            try:
                record = plistlib.loads(file.read_bytes())
            except (OSError, plistlib.InvalidFileException):
                continue
            if not isinstance(record, dict):
                continue
            udid_key = _normalize_udid(file.stem)
            if udid_key and udid_key not in by_udid:
                by_udid[udid_key] = record
            mac_key = _normalize_mac(record.get("WiFiMACAddress"))
            if mac_key and mac_key not in by_mac:
                by_mac[mac_key] = record
    return by_udid, by_mac


async def _safe_close_lockdown(lockdown: Any) -> None:
    if lockdown is None:
        return
    try:
        await lockdown.close()
    except Exception:
        pass


def _collect_dns_sd_lines(args: list[str], max_wait: float, stop_pattern: re.Pattern[str] | None = None, idle_grace: float = 0.35) -> list[str]:
    """运行持续输出型的 Windows dns-sd 命令，并在匹配目标或短时无新输出后提前结束。"""
    exe = shutil.which("dns-sd")
    if not exe:
        return []
    proc = subprocess.Popen(
        [exe, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines: list[str] = []
    matched_event = threading.Event()
    last_activity = [time.monotonic()]

    def reader() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                text = line.rstrip("\r\n")
                if not text:
                    continue
                lines.append(text)
                last_activity[0] = time.monotonic()
                if stop_pattern is not None and stop_pattern.search(text):
                    matched_event.set()
                    break
        except Exception:
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    deadline = time.monotonic() + max_wait
    try:
        while time.monotonic() < deadline:
            if matched_event.is_set() or proc.poll() is not None:
                break
            if lines and stop_pattern is None and (time.monotonic() - last_activity[0]) >= idle_grace:
                # 跳过前两行表头，至少收到一条记录后再提前结束
                if any(" Add " in item for item in lines):
                    break
            time.sleep(0.05)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
        t.join(timeout=0.5)
    return lines


def _discover_dns_sd_hosts_sync() -> list[tuple[str, list[str]]]:
    """通过 Windows Bonjour (dns-sd.exe) 发现 _apple-mobdev2._tcp 实例及其 IP 地址。"""
    browse_lines = _collect_dns_sd_lines(["-B", "_apple-mobdev2._tcp", "local."], max_wait=1.8)
    instances: list[str] = []
    for line in browse_lines:
        m = re.search(r"\sAdd\s+\d+\s+\d+\s+\S+\s+_apple-mobdev2\._tcp\.\s+(.+)$", line)
        if m:
            inst = m.group(1).strip()
            if inst not in instances:
                instances.append(inst)
    if not instances:
        return []

    reach_re = re.compile(r"can be reached at\s+(.+?):(\d+)")
    results: list[tuple[str, list[str]]] = []
    for inst in instances:
        mac_hint = inst.split("@", 1)[0] if "@" in inst else ""
        resolve_lines = _collect_dns_sd_lines(
            ["-L", inst, "_apple-mobdev2._tcp", "local."],
            max_wait=1.2,
            stop_pattern=reach_re,
        )
        target = ""
        for line in resolve_lines:
            m = reach_re.search(line)
            if m:
                target = m.group(1).strip().rstrip(".")
                break
        if not target:
            continue

        ips: list[str] = []
        for family in (socket.AF_INET, socket.AF_INET6):
            try:
                infos = socket.getaddrinfo(target, 62078, family, socket.SOCK_STREAM)
            except OSError:
                continue
            for info in infos:
                addr = info[4][0]
                if addr not in ips:
                    ips.append(addr)

        if not ips:
            v4_re = re.compile(r"\sAdd\s+\d+\s+\d+\s+\S+\s+(\d+\.\d+\.\d+\.\d+)\s+")
            g_lines = _collect_dns_sd_lines(["-G", "v4", target], max_wait=1.2, stop_pattern=v4_re)
            for line in g_lines:
                m = v4_re.search(line)
                if m and m.group(1) not in ips:
                    ips.append(m.group(1))

        if ips:
            results.append((mac_hint, ips))
    return results


def _candidate_pair_records(
    by_udid: dict[str, dict[str, Any]],
    by_mac: dict[str, dict[str, Any]],
    mac_hint: str = "",
    target_udid: str = "",
) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    seen_ids: set[int] = set()

    def add(rec: dict[str, Any] | None) -> None:
        if rec is not None and id(rec) not in seen_ids:
            seen_ids.add(id(rec))
            ordered.append(rec)

    if target_udid:
        add(by_udid.get(_normalize_udid(target_udid)))
    if mac_hint:
        add(by_mac.get(_normalize_mac(mac_hint)))
    if not target_udid:
        # iOS 私有无线局域网地址下，Bonjour 广播 MAC 可能与硬件 WiFiMACAddress 不同，逐一校验本地配对记录。
        for rec in by_udid.values():
            add(rec)
    return ordered


async def _connect_tcp_lockdown(
    pmd3: SimpleNamespace,
    hostname: str,
    pair_record: dict[str, Any] | None = None,
    mac_hint: str = "",
    target_udid: str = "",
    timeout: float = 6.0,
) -> Any:
    """建立已验证配对的 Wi-Fi TCP Lockdown 连接。"""
    if pair_record is not None:
        records = [pair_record]
    else:
        by_udid, by_mac = _load_pair_records(pmd3)
        records = _candidate_pair_records(by_udid, by_mac, mac_hint=mac_hint, target_udid=target_udid)

    for rec in records:
        cand = None
        try:
            cand = await asyncio.wait_for(
                pmd3.create_using_tcp(hostname=hostname, autopair=False, pair_record=rec),
                timeout=timeout,
            )
        except Exception:
            await _safe_close_lockdown(cand)
            # 若 TCP 本身连通失败，后续记录也无需继续试同一个 IP
            raise
        if cand.paired:
            if target_udid and _normalize_udid(cand.udid) != _normalize_udid(target_udid):
                await _safe_close_lockdown(cand)
                continue
            return cand
        await _safe_close_lockdown(cand)

    # 没有任何本地配对记录匹配时，仅做一次无配对探活以区分“设备不可达”与“未配对”
    probe = await asyncio.wait_for(
        pmd3.create_using_tcp(hostname=hostname, autopair=False),
        timeout=timeout,
    )
    await _safe_close_lockdown(probe)
    raise AppError(
        f"已连上 {hostname}，但未找到匹配的信任配对记录。"
        "请先插一次 USB 线点“信任此电脑”，并在菜单 1 开启 Wi-Fi 同步。"
    )


async def _probe_tcp_unpaired_info(pmd3: SimpleNamespace, hostname: str, timeout: float = 4.0) -> dict[str, str] | None:
    cand = None
    try:
        cand = await asyncio.wait_for(
            pmd3.create_using_tcp(hostname=hostname, autopair=False),
            timeout=timeout,
        )
        vals = cand.all_values or {}
        return {
            "name": vals.get("DeviceName") or hostname,
            "version": vals.get("ProductVersion") or "?",
        }
    except Exception:
        return None
    finally:
        await _safe_close_lockdown(cand)


async def _discover_bonjour_candidates(
    pmd3: SimpleNamespace,
    timeout: float = 2.5,
) -> list[tuple[str, list[str]]]:
    """发现局域网内通过 Bonjour 广播的 iOS 设备，返回 [(mac_hint, [ip, ...]), ...]。"""
    dns_sd_hosts = await asyncio.to_thread(_discover_dns_sd_hosts_sync)
    if dns_sd_hosts:
        return dns_sd_hosts

    if pmd3.browse_mobdev2 is None:
        return []
    try:
        instances = await pmd3.browse_mobdev2(timeout=timeout)
    except Exception:
        return []

    results: list[tuple[str, list[str]]] = []
    for answer in instances:
        mac_hint = answer.instance.split("@", 1)[0] if "@" in answer.instance else ""
        ips = [addr.full_ip for addr in answer.addresses if addr.full_ip]
        if ips:
            results.append((mac_hint, ips))
    return results


async def _discover_bonjour_lockdowns(
    pmd3: SimpleNamespace,
    target_udid: str = "",
    timeout: float = 2.5,
) -> list[tuple[str, Any]]:
    candidates = await _discover_bonjour_candidates(pmd3, timeout=timeout)
    if not candidates:
        return []

    connected: list[tuple[str, Any]] = []
    seen_udids: set[str] = set()
    for mac_hint, ips in candidates:
        for ip in ips:
            try:
                lockdown = await _connect_tcp_lockdown(
                    pmd3,
                    ip,
                    mac_hint=mac_hint,
                    target_udid=target_udid,
                    timeout=5.0,
                )
            except Exception:
                continue
            udid = _normalize_udid(getattr(lockdown, "udid", None))
            if udid and udid in seen_udids:
                await _safe_close_lockdown(lockdown)
                break
            if udid:
                seen_udids.add(udid)
            connected.append((ip, lockdown))
            break
    return connected


async def _run_heartbeat(lockdown: Any, ready_event: asyncio.Event) -> None:
    """为 Wi-Fi TCP 直连维持 com.apple.mobile.heartbeat 心跳，避免 iOS 立即关闭 AFC 服务。"""
    service = None
    try:
        service = await lockdown.start_lockdown_service("com.apple.mobile.heartbeat")
        ready_event.set()
        while True:
            await service.recv_plist()
            await service.send_plist({"Command": "Polo"})
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        ready_event.set()
        if service is not None:
            with contextlib.suppress(Exception):
                await service.close()


async def _connect_usbmux_device(
    pmd3: SimpleNamespace,
    serial: str,
    connection_type: str,
    autopair: bool,
) -> Any:
    lockdown = await pmd3.create_using_usbmux(
        serial=serial,
        connection_type=connection_type,
        autopair=autopair,
    )
    if not lockdown.paired:
        await _safe_close_lockdown(lockdown)
        raise AppError(
            "设备尚未配对或信任已失效。请先插一次 USB 线解锁手机并在弹窗中点“信任此电脑”。"
        )
    return lockdown


async def _discover_all_endpoints_coro(
    pmd3: SimpleNamespace,
    wifi_host: str = "",
) -> tuple[list[DiscoveredEndpoint], str | None]:
    """枚举当前可达的 USB / Wi-Fi (usbmux / Bonjour / 手动 IP) 设备端点（不落盘）。"""
    endpoints: list[DiscoveredEndpoint] = []
    mux_error: str | None = None
    seen_wifi_udids: set[str] = set()
    seen_wifi_hosts: set[str] = set()

    try:
        mux_devices = await pmd3.usbmux.list_devices()
    except Exception as exc:
        mux_devices = []
        mux_error = f"{type(exc).__name__}: {exc}"

    for dev in mux_devices:
        kind = "usb" if dev.is_usb else "wifi-usbmux"
        label = "USB" if dev.is_usb else "Wi-Fi (usbmux)"
        ep = DiscoveredEndpoint(
            transport_kind=kind,
            transport_label=label,
            name="iPhone",
            version="?",
            serial=dev.serial,
        )
        lockdown = None
        try:
            lockdown = await pmd3.create_using_usbmux(
                serial=dev.serial,
                connection_type=dev.connection_type,
                autopair=False,
            )
            vals = lockdown.all_values or {}
            ep.name = vals.get("DeviceName") or "iPhone"
            ep.version = vals.get("ProductVersion") or "?"
            ep.udid = _normalize_udid(lockdown.udid or dev.serial)
            ep.paired = bool(lockdown.paired)
            if lockdown.paired:
                try:
                    ep.wifi_enabled = await lockdown.get_enable_wifi_connections()
                except Exception:
                    ep.wifi_enabled = None
            if not dev.is_usb and ep.udid:
                seen_wifi_udids.add(ep.udid)
        except Exception as exc:
            ep.error = friendly_afc_connect_error(exc)
        finally:
            await _safe_close_lockdown(lockdown)
        endpoints.append(ep)

    bonjour_candidates = await _discover_bonjour_candidates(pmd3, timeout=2.5)
    for mac_hint, ips in bonjour_candidates:
        connected_lockdown = None
        connected_ip = ""
        last_err: Exception | None = None
        for ip in ips:
            try:
                connected_lockdown = await _connect_tcp_lockdown(
                    pmd3,
                    ip,
                    mac_hint=mac_hint,
                    timeout=4.5,
                )
                connected_ip = ip
                break
            except AppError as exc:
                last_err = exc
                connected_ip = ip
                break
            except Exception as exc:
                last_err = exc

        if connected_lockdown is not None:
            try:
                udid = _normalize_udid(getattr(connected_lockdown, "udid", None))
                seen_wifi_hosts.add(connected_ip)
                if udid and udid in seen_wifi_udids:
                    continue
                if udid:
                    seen_wifi_udids.add(udid)
                vals = connected_lockdown.all_values or {}
                wifi_enabled = None
                try:
                    wifi_enabled = await connected_lockdown.get_enable_wifi_connections()
                except Exception:
                    pass
                endpoints.append(
                    DiscoveredEndpoint(
                        transport_kind="wifi-bonjour",
                        transport_label=f"Wi-Fi (Bonjour {connected_ip})",
                        name=vals.get("DeviceName") or "iPhone",
                        version=vals.get("ProductVersion") or "?",
                        udid=udid,
                        paired=True,
                        wifi_enabled=wifi_enabled,
                        host=connected_ip,
                    )
                )
            finally:
                await _safe_close_lockdown(connected_lockdown)
        elif connected_ip and isinstance(last_err, AppError):
            info = await _probe_tcp_unpaired_info(pmd3, connected_ip)
            if info:
                seen_wifi_hosts.add(connected_ip)
                endpoints.append(
                    DiscoveredEndpoint(
                        transport_kind="wifi-bonjour",
                        transport_label=f"Wi-Fi (Bonjour {connected_ip})",
                        name=info["name"],
                        version=info["version"],
                        paired=False,
                        host=connected_ip,
                    )
                )

    host = (wifi_host or "").strip()
    if host and host not in seen_wifi_hosts:
        ep = DiscoveredEndpoint(
            transport_kind="wifi-tcp",
            transport_label=f"Wi-Fi (TCP {host})",
            name=host,
            version="?",
            host=host,
        )
        lockdown = None
        try:
            lockdown = await _connect_tcp_lockdown(pmd3, host, timeout=4.0)
            vals = lockdown.all_values or {}
            ep.name = vals.get("DeviceName") or host
            ep.version = vals.get("ProductVersion") or "?"
            ep.udid = _normalize_udid(getattr(lockdown, "udid", None))
            ep.paired = True
            try:
                ep.wifi_enabled = await lockdown.get_enable_wifi_connections()
            except Exception:
                pass
        except Exception as exc:
            ep.error = str(exc) if isinstance(exc, AppError) else f"{type(exc).__name__}: {exc}"
        finally:
            await _safe_close_lockdown(lockdown)
        endpoints.append(ep)

    return endpoints, mux_error


async def _open_lockdown(
    pmd3: SimpleNamespace,
    connection_mode: str = CONNECTION_MODE_AUTO,
    wifi_host: str = "",
    target_udid: str = "",
    prefer_transport: str = "",
    preferred_host: str = "",
) -> tuple[Any, str]:
    """按配置模式与指定设备建立 Lockdown 连接，返回 (lockdown, 传输方式说明)。"""
    mode = connection_mode if connection_mode in VALID_CONNECTION_MODES else CONNECTION_MODE_AUTO
    host = (preferred_host or wifi_host or "").strip()
    want_udid = _normalize_udid(target_udid)

    mux_devices: list[Any] = []
    mux_error: Exception | None = None
    try:
        mux_devices = await pmd3.usbmux.list_devices()
    except Exception as exc:
        mux_error = exc

    def matching_mux(is_usb: bool) -> list[Any]:
        items = [d for d in mux_devices if (d.is_usb if is_usb else d.is_network)]
        if want_udid:
            items = [d for d in items if _normalize_udid(d.serial) == want_udid]
        return items

    if mode == CONNECTION_MODE_USB:
        if mux_error is not None:
            raise AppError(friendly_afc_connect_error(mux_error)) from mux_error
        usb_devs = matching_mux(True)
        if not usb_devs:
            raise AppError("当前设为仅 USB 模式，但未检测到匹配的 USB iPhone。请插好数据线并解锁手机。")
        lockdown = await _connect_usbmux_device(pmd3, usb_devs[0].serial, "USB", autopair=True)
        return lockdown, "USB"

    if mode == CONNECTION_MODE_TCP:
        if not host:
            raise AppError("当前设为手动 IP 直连，但尚未配置 iPhone 的 Wi-Fi IP。请先进入菜单 1 设置。")
        try:
            lockdown = await _connect_tcp_lockdown(pmd3, host, target_udid=want_udid)
            return lockdown, f"Wi-Fi (TCP {host})"
        except AppError:
            raise
        except Exception as exc:
            raise AppError(f"无法通过 Wi-Fi TCP 连接到 {host}:62078（{type(exc).__name__}: {exc}）") from exc

    if mode == CONNECTION_MODE_AUTO and prefer_transport != "wifi":
        usb_devs = matching_mux(True)
        if usb_devs:
            lockdown = await _connect_usbmux_device(pmd3, usb_devs[0].serial, "USB", autopair=True)
            return lockdown, "USB"

    # Wi-Fi 链路：1. usbmux Network -> 2. Bonjour (dns-sd / mobdev2) -> 3. 手动 IP TCP
    last_wifi_error: Exception | None = None
    for dev in matching_mux(False):
        try:
            lockdown = await _connect_usbmux_device(pmd3, dev.serial, "Network", autopair=False)
            return lockdown, "Wi-Fi (usbmux)"
        except Exception as exc:
            last_wifi_error = exc

    bonjour_conns = await _discover_bonjour_lockdowns(pmd3, target_udid=want_udid, timeout=2.5)
    if bonjour_conns:
        chosen_ip, chosen_lockdown = bonjour_conns[0]
        for _, extra in bonjour_conns[1:]:
            await _safe_close_lockdown(extra)
        return chosen_lockdown, f"Wi-Fi (Bonjour {chosen_ip})"

    if host:
        try:
            lockdown = await _connect_tcp_lockdown(pmd3, host, target_udid=want_udid)
            return lockdown, f"Wi-Fi (TCP {host})"
        except Exception as exc:
            last_wifi_error = exc

    # 若指定只优先 Wi-Fi 但 auto 模式下仅有 USB 可用，则退回 USB
    if mode == CONNECTION_MODE_AUTO and prefer_transport == "wifi":
        usb_devs = matching_mux(True)
        if usb_devs:
            lockdown = await _connect_usbmux_device(pmd3, usb_devs[0].serial, "USB", autopair=True)
            return lockdown, "USB"

    if last_wifi_error is not None:
        if isinstance(last_wifi_error, AppError):
            raise last_wifi_error
        raise AppError(
            f"Wi-Fi 连接尝试失败：{friendly_afc_connect_error(last_wifi_error)}\n"
            "提示：请确认手机与电脑在同一局域网、手机已解锁，且已在“菜单 1 → 开启 Wi-Fi 同步”完成设置。"
        ) from last_wifi_error

    if mode == CONNECTION_MODE_WIFI:
        raise AppError(
            "没有通过 Wi-Fi 检测到可连接的已配对 iPhone。\n"
            "请确认：\n"
            "  1. iPhone 与电脑处于同一 Wi-Fi 局域网（未开启路由器 AP 隔离）\n"
            "  2. 曾用 USB 连过一次并在“菜单 1 → 开启 Wi-Fi 同步”完成开启\n"
            "  3. 若当前被路由器屏蔽了 mDNS 广播，可在菜单 1 设置 iPhone 的手动 Wi-Fi IP"
        )
    if mux_error is not None and not host:
        raise AppError(friendly_afc_connect_error(mux_error)) from mux_error
    raise AppError(
        "没有检测到通过 USB 或 Wi-Fi 连接的 iPhone。\n"
        "请插上 USB 数据线，或确认已在菜单 1 开启 Wi-Fi 同步且手机与电脑处于同一局域网。"
    )


async def _enable_wifi_sync_coro(pmd3: SimpleNamespace, target_udid: str = "") -> dict[str, Any]:
    """通过 USB 连接一次，完成信任配对、开启 EnableWifiConnections 并缓存配对记录。"""
    try:
        usb_devs = [d for d in await pmd3.usbmux.list_devices() if d.is_usb]
    except Exception as exc:
        raise AppError(friendly_afc_connect_error(exc)) from exc
    if target_udid:
        matched = [d for d in usb_devs if _normalize_udid(d.serial) == _normalize_udid(target_udid)]
        if matched:
            usb_devs = matched
    if not usb_devs:
        raise AppError("请先用 USB 数据线连接 iPhone 并解锁屏幕，再执行此操作。")

    lockdown = await pmd3.create_using_usbmux(
        serial=usb_devs[0].serial,
        connection_type="USB",
        autopair=True,
    )
    try:
        if not lockdown.paired:
            await lockdown.pair()
        already_enabled = False
        try:
            already_enabled = await lockdown.get_enable_wifi_connections()
        except Exception:
            already_enabled = False
        if not already_enabled:
            await lockdown.set_enable_wifi_connections(True)
        enabled_now = await lockdown.get_enable_wifi_connections()

        # 确保 ~/.pymobiledevice3/<UDID>.plist 也有配对记录，便于无 usbmuxd 时 Bonjour/TCP 使用
        cached_local = False
        if lockdown.pair_record and lockdown.udid:
            cache_dir = pmd3.get_home_folder()
            cache_file = cache_dir / f"{lockdown.udid}.plist"
            if not cache_file.exists():
                cache_file.write_bytes(plistlib.dumps(lockdown.pair_record))
                cached_local = True

        values = lockdown.all_values or {}
        return {
            "name": values.get("DeviceName") or "iPhone",
            "version": values.get("ProductVersion") or "?",
            "udid": _normalize_udid(lockdown.udid or usb_devs[0].serial),
            "already_enabled": already_enabled,
            "enabled_now": enabled_now,
            "cached_local": cached_local,
        }
    finally:
        await _safe_close_lockdown(lockdown)


def inspect_devices(wifi_host: str = "") -> tuple[list[DiscoveredEndpoint], str | None]:
    pmd3 = _import_pmd3()
    return asyncio.run(_discover_all_endpoints_coro(pmd3, wifi_host=wifi_host))


def enable_wifi_sync_over_usb(target_udid: str = "") -> dict[str, Any]:
    pmd3 = _import_pmd3()
    return asyncio.run(_enable_wifi_sync_coro(pmd3, target_udid=target_udid))


class AFCMediaSourceBase:
    """AFC 媒体源公共逻辑：主文件 + 可选附属文件（.AAE 编辑记录 / Live Photo .MOV）。"""

    transport_label: str = "AFC"

    def __init__(self) -> None:
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

    def pull(self, record: AFCRecord, dest_path: Path) -> tuple[str, int]:
        return self.pull_file(record.remote_path, dest_path)

    def close(self) -> None:
        pass

    def _list_directory(self, remote_dir: str) -> set[str] | None:
        raise NotImplementedError

    def pull_file(self, remote: str, dest_path: Path) -> tuple[str, int]:
        raise NotImplementedError


class AFCMediaSource(AFCMediaSourceBase):
    """命令行兜底方案：每个文件调用一次 `pymobiledevice3 afc` 子进程（较慢）。"""

    def __init__(self, exe: str, use_mobdev2: bool = False) -> None:
        super().__init__()
        self.exe = exe
        self.use_mobdev2 = use_mobdev2
        self.transport_label = "Wi-Fi (命令行)" if use_mobdev2 else "USB/usbmux (命令行)"

    def _cmd(self, *args: str) -> list[str]:
        base = [self.exe, "afc", *args]
        if self.use_mobdev2:
            base.append("--mobdev2")
        return base

    def _list_directory(self, remote_dir: str) -> set[str] | None:
        if remote_dir in self.directory_cache:
            return self.directory_cache[remote_dir]
        result = subprocess.run(
            self._cmd("ls", remote_dir),
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

    def pull_file(self, remote: str, dest_path: Path) -> tuple[str, int]:
        code = run_streaming(self._cmd("pull", "-i", remote, str(dest_path)))
        if code != 0 or not dest_path.exists():
            if dest_path.exists():
                dest_path.unlink()
            raise AppError(f"AFC 拉取失败：{remote}")
        return hash_file(dest_path)


class AFCServiceSource(AFCMediaSourceBase):
    """进程内 AFC 媒体源（支持 USB 与 Wi-Fi）。

    在后台线程里维护一个 asyncio 事件循环和一条 AfcService 连接；
    同步接口通过 run_coroutine_threadsafe 提交任务。拉取文件时用
    fopen/fread 流式读取，边写盘边计算 sha256，避免二次读盘。
    Wi-Fi 瞬断时支持自动重连并重试一次当前文件。
    """

    def __init__(
        self,
        connection_mode: str = CONNECTION_MODE_AUTO,
        wifi_host: str = "",
        target_udid: str = "",
        prefer_transport: str = "",
        preferred_host: str = "",
    ) -> None:
        super().__init__()
        self.connection_mode = connection_mode
        self.wifi_host = wifi_host
        self.target_udid = _normalize_udid(target_udid)
        self.prefer_transport = prefer_transport
        self.preferred_host = preferred_host
        self._pmd3 = _import_pmd3()
        self._pmd3_exceptions = self._pmd3.exceptions
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="afc-event-loop", daemon=True)
        self._thread.start()
        self._lockdown: Any = None
        self._hb_task: asyncio.Task[None] | None = None
        self._afc: Any = None
        self.transport_label = "AFC"
        try:
            future = asyncio.run_coroutine_threadsafe(self._open(), self._loop)
            future.result(timeout=180)
        except concurrent.futures.TimeoutError as exc:
            self.close()
            raise AppError("连接 iPhone 超时（超过 3 分钟）。请解锁手机、确认网络连接后重试。") from exc
        except AppError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise AppError(friendly_afc_connect_error(exc)) from exc

    async def _open(self) -> None:
        try:
            lockdown, transport_label = await _open_lockdown(
                self._pmd3,
                connection_mode=self.connection_mode,
                wifi_host=self.wifi_host,
                target_udid=self.target_udid,
                prefer_transport=self.prefer_transport,
                preferred_host=self.preferred_host,
            )
        except AppError:
            raise
        except Exception as exc:
            raise AppError(friendly_afc_connect_error(exc)) from exc

        hb_task: asyncio.Task[None] | None = None
        if isinstance(lockdown, self._pmd3.TcpLockdownClient):
            ready = asyncio.Event()
            hb_task = asyncio.create_task(_run_heartbeat(lockdown, ready), name="lockdown-heartbeat")
            try:
                await asyncio.wait_for(ready.wait(), timeout=10.0)
            except Exception:
                hb_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await hb_task
                hb_task = None

        afc = self._pmd3.AfcService(lockdown=lockdown)
        try:
            await afc.__aenter__()
        except Exception:
            if hb_task is not None:
                hb_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await hb_task
            await _safe_close_lockdown(lockdown)
            raise
        self._lockdown = lockdown
        self._hb_task = hb_task
        self._afc = afc
        self.transport_label = transport_label
        if not self.target_udid:
            self.target_udid = _normalize_udid(getattr(lockdown, "udid", None))

    async def _close_clients_coro(self) -> None:
        if self._afc is not None:
            try:
                await self._afc.__aexit__(None, None, None)
            except Exception:
                pass
            self._afc = None
        if self._hb_task is not None:
            self._hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._hb_task
            self._hb_task = None
        if self._lockdown is not None:
            await _safe_close_lockdown(self._lockdown)
            self._lockdown = None

    async def _reconnect_coro(self) -> None:
        await self._close_clients_coro()
        await self._open()

    def _call(self, coro: Any, timeout: float | None = None) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def _is_connection_error(self, exc: Exception) -> bool:
        excs = self._pmd3_exceptions
        if isinstance(exc, excs.AfcException):
            return False
        conn_types = [
            ConnectionError,
            TimeoutError,
            asyncio.TimeoutError,
            asyncio.IncompleteReadError,
            EOFError,
            excs.ConnectionTerminatedError,
            excs.ConnectionFailedError,
            excs.MuxException,
        ]
        for attr in ("ChannelClosedError", "StreamClosedError"):
            cls = getattr(excs, attr, None)
            if cls is not None:
                conn_types.append(cls)
        return isinstance(exc, tuple(conn_types))

    def reconnect(self) -> None:
        try:
            self._call(self._reconnect_coro(), timeout=60)
        except AppError as exc:
            raise ConnectionLostError(f"连接已断开且重连失败：{exc}") from exc
        except Exception as exc:
            raise ConnectionLostError(f"连接已断开且重连失败：{friendly_afc_connect_error(exc)}") from exc

    def _list_directory(self, remote_dir: str) -> set[str] | None:
        if remote_dir in self.directory_cache:
            return self.directory_cache[remote_dir]
        for attempt in range(2):
            try:
                names = set(self._call(self._afc.listdir(remote_dir), timeout=60))
                self.directory_cache[remote_dir] = names
                return names
            except self._pmd3_exceptions.AfcException:
                self.directory_cache[remote_dir] = None
                return None
            except Exception as exc:
                if attempt == 0 and self._is_connection_error(exc):
                    try:
                        self.reconnect()
                        continue
                    except ConnectionLostError:
                        return None
                return None
        return None

    async def _pull_file_coro(self, remote: str, dest_path: Path) -> tuple[str, int]:
        afc = self._afc
        stat = await afc.stat(remote)
        src_size = int(stat["st_size"])
        name = remote.rsplit("/", 1)[-1] or remote
        hasher = hashlib.sha256()
        size = 0
        start = time.monotonic()
        big_file = src_size > CHUNK_SIZE
        with dest_path.open("wb") as dst:
            handle = await afc.fopen(remote, "r")
            try:
                remaining = src_size
                while remaining > 0:
                    to_read = min(CHUNK_SIZE, remaining)
                    chunk = await afc.fread(handle, to_read)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    dst.write(chunk)
                    size += len(chunk)
                    remaining -= len(chunk)
                    if big_file:
                        elapsed = time.monotonic() - start
                        speed = size / elapsed if elapsed > 0 else 0.0
                        with PRINT_LOCK:
                            print(
                                f"\r  ↳ {name}  {human_size(size)}/{human_size(src_size)}"
                                f"  {human_size(speed)}/s   ",
                                end="",
                                flush=True,
                            )
            finally:
                try:
                    await afc.fclose(handle)
                except Exception:
                    pass
        elapsed = time.monotonic() - start
        speed = size / elapsed if elapsed > 0 else 0.0
        summary = f"{human_size(size)}  {elapsed:.1f}s  {human_size(speed)}/s"
        with PRINT_LOCK:
            print(f"\r  ↳ {name}  {summary}   " if big_file else f"  ↳ {name}  {summary}")
        try:
            mtime = stat["st_mtime"].timestamp()
            os.utime(dest_path, (mtime, mtime))
        except (OSError, OverflowError, ValueError):
            pass
        return hasher.hexdigest(), size

    def pull_file(self, remote: str, dest_path: Path) -> tuple[str, int]:
        for attempt in range(2):
            try:
                return self._call(self._pull_file_coro(remote, dest_path))
            except self._pmd3_exceptions.AfcException as exc:
                with PRINT_LOCK:
                    print()  # 收掉可能停留在半行的进度行
                if dest_path.exists():
                    dest_path.unlink()
                raise AppError(f"AFC 拉取失败：{remote}（{exc}）") from exc
            except Exception as exc:
                with PRINT_LOCK:
                    print()
                if dest_path.exists():
                    dest_path.unlink()
                if self._is_connection_error(exc):
                    if attempt == 0:
                        print(f"提示：连接波动（{type(exc).__name__}），正在自动重连并重试 {Path(remote).name}……")
                        self.reconnect()
                        continue
                    raise ConnectionLostError(
                        f"与 iPhone 的连接中断（{type(exc).__name__}: {exc}）。请确认设备仍连接后再试。"
                    ) from exc
                raise
        raise ConnectionLostError(f"与 iPhone 的连接中断，无法拉取 {remote}。")

    def close(self) -> None:
        if getattr(self, "_loop", None) is not None and self._loop.is_running():
            try:
                self._call(self._close_clients_coro(), timeout=30)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            self._loop.close()
        elif getattr(self, "_loop", None) is not None and not self._loop.is_closed():
            self._loop.close()


def make_afc_media_source(
    connection_mode: str = CONNECTION_MODE_AUTO,
    wifi_host: str = "",
    target_udid: str = "",
    prefer_transport: str = "",
    preferred_host: str = "",
) -> AFCMediaSourceBase:
    try:
        _import_pmd3()
    except ImportError:
        exe = find_pymobiledevice3()
        if exe:
            print("提示：pymobiledevice3 Python 库导入失败，改用命令行方式（每个文件会重启子进程，较慢）。")
            return AFCMediaSource(exe, use_mobdev2=(connection_mode == CONNECTION_MODE_WIFI))
        print_missing_pymobiledevice3()
        raise AppError("没有可用的 pymobiledevice3，无法通过 AFC 访问 iPhone。")
    try:
        return AFCServiceSource(
            connection_mode=connection_mode,
            wifi_host=wifi_host,
            target_udid=target_udid,
            prefer_transport=prefer_transport,
            preferred_host=preferred_host,
        )
    except AppError:
        raise
    except Exception as exc:
        raise AppError(friendly_afc_connect_error(exc)) from exc


def refresh_photos_metadata(
    metadata_root: Path,
    media_source: AFCMediaSourceBase,
) -> Path:
    """使用现有 AFC 连接从手机拉取 Photos.sqlite 到 metadata_root 下的单个工作文件（覆盖旧副本）。

    相簿结构数据库只是临时工作数据：相簿一变它就过时了，所以不再按时间戳
    留存多份快照，每次刷新直接覆盖。主文件先拉到 .part 再原子替换，
    避免中途断开留下截断的数据库。
    """
    metadata_root.mkdir(parents=True, exist_ok=True)
    main_path = metadata_root / "Photos.sqlite"
    part_path = metadata_root / "Photos.sqlite.part"
    for stale in (metadata_root / "Photos.sqlite-wal", metadata_root / "Photos.sqlite-shm"):
        if stale.exists():
            stale.unlink()
    try:
        media_source.pull_file("PhotoData/Photos.sqlite", part_path)
    except AppError:
        if part_path.exists():
            part_path.unlink()
        raise AppError("没有成功读取 PhotoData/Photos.sqlite。请确认手机已解锁并信任此电脑。")
    os.replace(part_path, main_path)
    for suffix in ("-wal", "-shm"):
        try:
            media_source.pull_file(f"PhotoData/Photos.sqlite{suffix}", metadata_root / f"Photos.sqlite{suffix}")
        except AppError:
            print(f"提示：Photos.sqlite{suffix} 没有拉取到（只可能缺少最近一次改动，不影响使用）。")
    print(f"\n相簿元数据已更新：{main_path}")
    return metadata_root


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


# ============================================================================
# 导出索引数据库
# ============================================================================


class BackupCatalog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self._uncommitted = 0
        self._ensure_schema()

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self.conn.close()

    def flush(self) -> None:
        if self._uncommitted:
            self.conn.commit()
            self._uncommitted = 0

    def _maybe_commit(self) -> None:
        self._uncommitted += 1
        if self._uncommitted >= SQL_COMMIT_EVERY:
            self.flush()

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
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(backup_runs)")}
        if existing and "interrupted" not in existing:
            self.conn.execute("ALTER TABLE backup_runs ADD COLUMN interrupted INTEGER NOT NULL DEFAULT 0")
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

    def media_map(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT source_key, last_exported_at, last_path, export_count FROM media_backup"
        )
        return {row["source_key"]: dict(row) for row in rows}

    def run_exists(self, run_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM backup_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return row is not None

    def album_map(self, album: str) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT source_key, last_exported_at, last_path, export_count "
            "FROM album_backup WHERE album = ?",
            (album,),
        )
        return {row["source_key"]: dict(row) for row in rows}

    def record_success(self, entry: dict[str, Any]) -> None:
        source_key = entry["source_key"]
        exported_at = entry["exported_at"]
        sha256 = entry.get("sha256") or None
        stored_path = entry.get("stored_path") or entry["exported_path"]
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
                stored_path,
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
                stored_path,
            ),
        )
        self._maybe_commit()

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
        interrupted: int = 0,
    ) -> None:
        finished_at = dt.datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            """
            INSERT OR REPLACE INTO backup_runs (
                run_id, started_at, finished_at, albums, copied,
                duplicate_copied, skipped, missing, csv_path, interrupted
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                int(interrupted),
            ),
        )
        self._maybe_commit()

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


# ============================================================================
# 相簿浏览 / 加载 / 选择
# ============================================================================


def load_library_from_metadata(metadata_root: Path) -> PhotosLibrary:
    db_path = metadata_root / "Photos.sqlite" if metadata_root.is_dir() else metadata_root
    if not db_path.exists():
        raise AppError(f"没有找到相簿元数据：{db_path}。请先执行菜单 2 从手机获取。")
    parser = PhotosParser(db_path)
    try:
        return parser.parse()
    finally:
        parser.close()


def refresh_and_load_metadata(
    metadata_root: Path,
    connection_mode: str = CONNECTION_MODE_AUTO,
    wifi_host: str = "",
    selected_device: dict[str, Any] | None = None,
) -> tuple[AFCMediaSourceBase, PhotosLibrary, dict[str, Any] | None]:
    """选择目标设备并建立一次 AFC 连接，拉取最新 Photos.sqlite 与相簿结构（菜单 2 使用）。"""
    chosen, updated_selected = choose_endpoint_for_connect(
        connection_mode=connection_mode,
        wifi_host=wifi_host,
        selected_device=selected_device,
    )
    media_source = make_afc_media_source(
        connection_mode=connection_mode,
        wifi_host=wifi_host,
        target_udid=chosen.udid if chosen else "",
        prefer_transport=("usb" if chosen.is_usb else "wifi") if chosen else "",
        preferred_host=(chosen.host or "") if chosen else "",
    )
    try:
        refresh_photos_metadata(metadata_root, media_source)
        library = load_library_from_metadata(metadata_root)
        print_library_summary(library, media_source)
        return media_source, library, updated_selected
    except Exception:
        media_source.close()
        raise


def print_library_summary(
    library: PhotosLibrary,
    media_source: AFCMediaSourceBase | None = None,
) -> None:
    print("\nPhotos 数据库：", library.db_path)
    print(f"资产表：{library.asset_table}")
    print(f"相簿表：{library.album_table or '未识别'}")
    print(f"相簿关系表：{library.join_table or '未识别'}")
    print(f"资产数量：{len(library.assets)}")
    print(f"可选分类/相簿：{len(library.albums)}")
    if isinstance(media_source, AFCServiceSource):
        print(f"导出方式：进程内 AFC 直连（{media_source.transport_label}，快）")
    elif isinstance(media_source, AFCMediaSource):
        print(f"导出方式：命令行 AFC 直连（{media_source.transport_label}，慢，建议 pip 安装 pymobiledevice3 后重试）")


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


# ============================================================================
# 导出
# ============================================================================


def choose_duplicate_policy() -> str:
    print("\n重复处理策略：")
    print("1. 同一相簿里已经导出过就跳过；同一文件在别的相簿仍正常导出（推荐）")
    print("2. 只要以前任何相簿导出过，就跳过")
    print("3. 只要以前任何相簿导出过，仍复制，但文件名加 __DUP")
    value = prompt("选择", "1")
    return {"1": "album-skip", "2": "global-skip", "3": "global-mark"}.get(value, "album-skip")


def choose_date_layout() -> str:
    print("\n导出目录日期分层：")
    print("1. 按导出当天日期分目录（默认：相簿名/导出日/，当天同相簿文件都在同一目录）")
    print("2. 按拍摄日期分目录（照片/视频的原始创建日期）")
    print("3. 直接放在相簿目录下，不按日期分层")
    value = prompt("选择", "1")
    return {"1": "run", "2": "shot", "3": "none"}.get(value, "run")


def asset_date_subdir(layout: str, asset: Asset) -> str:
    if layout == "run":
        return dt.date.today().isoformat()
    if layout == "shot":
        iso = apple_time_to_iso(asset.date_created) or apple_time_to_iso(asset.date_added)
        return iso[:10] if iso else "未知日期"
    return ""


class ExportLogWriter:
    """CSV 导出日志：边导出边写、定期落盘，中断时已完成部分不会丢。"""

    FIELDS = [
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

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = path.open("w", newline="", encoding="utf-8-sig")
        self._writer = csv.DictWriter(self._file, fieldnames=self.FIELDS)
        self._writer.writeheader()
        self._count = 0

    def write(self, row: dict[str, Any]) -> None:
        self._writer.writerow({field: row.get(field, "") for field in self.FIELDS})
        self._count += 1
        if self._count % CSV_FLUSH_EVERY == 0:
            self._file.flush()

    def close(self) -> None:
        self._file.flush()
        self._file.close()


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


def media_identity(record: AFCRecord) -> str:
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


def export_media_record(media_source: AFCMediaSourceBase, record: AFCRecord, dest_path: Path) -> tuple[str, int]:
    return media_source.pull(record, dest_path)


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
    record: AFCRecord | None,
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


def export_selected_albums(
    media_source: AFCMediaSourceBase,
    library: PhotosLibrary,
    export_root: Path,
    index_path: Path,
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
    date_layout = choose_date_layout()
    export_root.mkdir(parents=True, exist_ok=True)
    catalog = BackupCatalog(index_path)
    base_id = now_stamp()
    run_id = base_id
    suffix = 1
    while catalog.run_exists(run_id):
        suffix += 1
        run_id = f"{base_id}_{suffix}"
    started_at = dt.datetime.now().isoformat(timespec="seconds")
    run_start_mono = time.monotonic()
    run_log = export_root / f"export_log_{run_id}.csv"
    log_writer: ExportLogWriter | None = None

    copied = duplicate_copied = skipped = missing = sidecar_failed = 0
    interrupted = False

    try:
        # 预载去重信息：全局一次，每个相簿一次（而不是每文件两次 SQL）。
        global_media = catalog.media_map()
        album_maps: dict[str, dict[str, dict[str, Any]]] = {}

        def album_map_for(album_name: str) -> dict[str, dict[str, Any]]:
            if album_name not in album_maps:
                album_maps[album_name] = catalog.album_map(album_name)
            return album_maps[album_name]

        log_writer = ExportLogWriter(run_log)

        def handle_export_failure(record: AFCRecord, message: str) -> None:
            nonlocal missing, sidecar_failed
            if isinstance(record, AFCRecord) and not record.required:
                sidecar_failed += 1
                log_writer.write(
                    log_entry(
                        run_id,
                        album,
                        asset,
                        record,
                        "",
                        "sidecar-failed",
                        f"附属文件（编辑记录/Live Photo 视频）导出失败：{message}",
                        source_key=source_key,
                    )
                )
                return
            missing += 1
            log_writer.write(
                log_entry(
                    run_id,
                    album,
                    asset,
                    record,
                    "",
                    "missing",
                    message,
                    source_key=source_key,
                )
            )

        print("\n开始导出……大视频计算哈希会花一点时间。")
        try:
            for album in selected_albums:
                album_base = export_root / safe_album_path(album.export_name)
                exported_in_album = album_map_for(album.export_name)
                total = len(album.asset_pks)
                print(f"\n[{album.export_name}] {total} 项 -> {album_base}")
                done = 0
                for asset_pk in sorted(album.asset_pks):
                    asset = library.assets.get(asset_pk)
                    if not asset:
                        continue
                    done += 1
                    media_records = media_source.find_media_for_asset(asset)
                    for record in media_records:
                        source_key = f"{asset.source_key}|{media_identity(record)}"
                        previous_album = exported_in_album.get(source_key)
                        previous_global = global_media.get(source_key)
                        if previous_album:
                            skipped += 1
                            log_writer.write(
                                log_entry(
                                    run_id,
                                    album,
                                    asset,
                                    record,
                                    previous_album.get("last_path") or "",
                                    "skipped",
                                    previous_note(previous_album, "同一相簿已导出过"),
                                    source_key=source_key,
                                )
                            )
                            continue
                        if policy == "global-skip" and previous_global:
                            skipped += 1
                            log_writer.write(
                                log_entry(
                                    run_id,
                                    album,
                                    asset,
                                    record,
                                    previous_global.get("last_path") or "",
                                    "skipped",
                                    previous_note(previous_global, "全局已导出过"),
                                    source_key=source_key,
                                )
                            )
                            continue

                        mark_duplicate = policy == "global-mark" and previous_global is not None
                        dest_name = destination_filename(asset.filename, record.relative_path, mark_duplicate)
                        subdir = asset_date_subdir(date_layout, asset)
                        dest_dir = album_base / subdir if subdir else album_base
                        dest_path = unique_dest_path(dest_dir / dest_name)
                        try:
                            dest_dir.mkdir(parents=True, exist_ok=True)
                            sha256, size = export_media_record(media_source, record, dest_path)
                        except ConnectionLostError:
                            raise
                        except AppError as exc:
                            handle_export_failure(record, str(exc))
                            continue
                        except (OSError, sqlite3.Error) as exc:
                            handle_export_failure(record, f"本地读写错误：{exc}")
                            continue

                        status = "duplicate-copied" if mark_duplicate else "copied"
                        if mark_duplicate:
                            duplicate_copied += 1
                        else:
                            copied += 1
                        note = previous_note(previous_global, "全局重复，已加 __DUP") if mark_duplicate else ""
                        try:
                            stored_path = dest_path.relative_to(export_root).as_posix()
                        except ValueError:
                            stored_path = str(dest_path)
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
                        entry["stored_path"] = stored_path
                        catalog.record_success(entry)
                        # 同步更新内存去重表：同一资产出现在后面相簿时能立即看到。
                        info = {
                            "last_exported_at": entry["exported_at"],
                            "last_path": stored_path,
                            "export_count": (previous_global or {}).get("export_count", 0) + 1,
                        }
                        global_media[source_key] = info
                        exported_in_album[source_key] = info
                        log_writer.write(entry)

                    if done % PROGRESS_EVERY == 0 or done == total:
                        with PRINT_LOCK:
                            print(
                                f"  已处理 {done}/{total}：复制 {copied}，重复 {duplicate_copied}，"
                                f"跳过 {skipped}，缺失 {missing}"
                            )
        except ConnectionLostError as exc:
            interrupted = True
            print(f"\n\n连接中断：{exc}")
            print("已完成的部分仍记录在本次 CSV 与索引库里，恢复连接后重跑即可继续。")
        except KeyboardInterrupt:
            interrupted = True
            print("\n\n已中断：已完成的部分仍记录在本次 CSV 里。")
        catalog.record_run(
            run_id,
            started_at,
            [album.export_name for album in selected_albums],
            copied,
            duplicate_copied,
            skipped,
            missing,
            run_log,
            interrupted=interrupted,
        )
    finally:
        if log_writer is not None:
            log_writer.close()
        try:
            catalog.flush()
            catalog.close()
        except Exception:
            pass

    print("\n导出完成。" if not interrupted else "\n导出已中断。")
    run_elapsed = time.monotonic() - run_start_mono
    print(
        f"复制：{copied}，重复复制：{duplicate_copied}，跳过：{skipped}，"
        f"缺失：{missing}，附属文件失败：{sidecar_failed}"
        f"（耗时 {int(run_elapsed // 60)}分{int(run_elapsed % 60):02d}秒）"
    )
    print(f"索引数据库：{index_path}")
    print(f"本次 CSV：{run_log}")


# ============================================================================
# 待删除清单 / 状态汇总 / 设置
# ============================================================================


def make_delete_review(
    media_source: AFCMediaSourceBase,
    library: PhotosLibrary,
    export_root: Path,
) -> None:
    print("\n说明：这个功能不会删除 iPhone 上的原件。")
    print("原因是 iOS 没有通过 USB/Wi-Fi 暴露“按 Photos 相簿安全删除”的公开接口；直接删 AFC/DCIM 文件会让照片库数据库失配。")
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
                    "afc_paths": "; ".join(record.key for record in records),
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
                "afc_paths",
                "note",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"已生成清单：{path}")


def show_state_summary(index_path: Path) -> None:
    catalog = BackupCatalog(index_path)
    try:
        summary = catalog.summary()
        albums = catalog.top_albums()
        duplicates = catalog.duplicate_groups()
        recent_runs = catalog.recent_runs()
    finally:
        catalog.close()
    print("\n备份记录：")
    print(f"索引数据库：{index_path}")
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
            interrupted = "（中断）" if row.get("interrupted") else ""
            print(
                f"  - {row['finished_at']}{interrupted}：复制 {row['copied']}，"
                f"重复复制 {row['duplicate_copied']}，跳过 {row['skipped']}，缺失 {row['missing']}"
            )


def load_config() -> dict[str, Any]:
    config = load_json(
        CONFIG_PATH,
        {
            "export_root": str(DEFAULT_EXPORT_ROOT),
            "metadata_root": str(DEFAULT_METADATA_ROOT),
            "index_path": str(DEFAULT_INDEX_PATH),
            "connection_mode": CONNECTION_MODE_AUTO,
            "wifi_host": "",
        },
    )
    # 清理早期版本遗留的完整备份配置键（如 backup_root）
    removed_legacy = False
    for legacy_key in ("backup_root",):
        if legacy_key in config:
            config.pop(legacy_key, None)
            removed_legacy = True
    config.setdefault("export_root", str(DEFAULT_EXPORT_ROOT))
    config.setdefault("metadata_root", str(DEFAULT_METADATA_ROOT))
    config.setdefault("index_path", str(DEFAULT_INDEX_PATH))
    if config.get("connection_mode") not in VALID_CONNECTION_MODES:
        config["connection_mode"] = CONNECTION_MODE_AUTO
    config["wifi_host"] = str(config.get("wifi_host") or "").strip()
    if removed_legacy and CONFIG_PATH.exists():
        save_json(CONFIG_PATH, config)
    return config


def _format_endpoint_line(
    ep: DiscoveredEndpoint,
    index: int | None = None,
    selected_device: dict[str, Any] | None = None,
) -> str:
    prefix = f"  {index}. " if index is not None else "  - "
    is_selected = False
    if selected_device and ep.udid and _normalize_udid(selected_device.get("udid")) == ep.udid:
        pref = selected_device.get("prefer_transport")
        if not pref or (pref == "usb" and ep.is_usb) or (pref == "wifi" and ep.is_wifi):
            is_selected = True
    tag = " [当前已选]" if is_selected else ""
    if ep.error:
        return f"{prefix}[{ep.transport_label}] {ep.name} | 异常：{ep.error}{tag}"
    paired_text = "已配对" if ep.paired else "未配对"
    wifi_state = ep.wifi_enabled
    wifi_text = "已开启" if wifi_state is True else ("未开启" if wifi_state is False else "未知")
    return (
        f"{prefix}[{ep.transport_label}] {ep.name} (iOS {ep.version}) | "
        f"{paired_text} | Wi-Fi同步：{wifi_text}{tag}"
    )


def _endpoint_to_selection(ep: DiscoveredEndpoint) -> dict[str, Any]:
    return {
        "udid": ep.udid,
        "name": ep.name,
        "prefer_transport": "usb" if ep.is_usb else "wifi",
        "transport_label": ep.transport_label,
        "host": ep.host or "",
    }


def _filter_endpoints_for_mode(
    endpoints: list[DiscoveredEndpoint],
    connection_mode: str,
) -> list[DiscoveredEndpoint]:
    paired = [ep for ep in endpoints if ep.paired and not ep.error]
    if connection_mode == CONNECTION_MODE_USB:
        return [ep for ep in paired if ep.is_usb]
    if connection_mode == CONNECTION_MODE_WIFI:
        return [ep for ep in paired if ep.is_wifi]
    if connection_mode == CONNECTION_MODE_TCP:
        return [ep for ep in paired if ep.transport_kind == "wifi-tcp"]
    return paired


def choose_endpoint_for_connect(
    connection_mode: str,
    wifi_host: str,
    selected_device: dict[str, Any] | None = None,
) -> tuple[DiscoveredEndpoint | None, dict[str, Any] | None]:
    """按配置模式与已选设备确定目标端点；发现多个可用设备时提示用户选择。"""
    try:
        _import_pmd3()
    except ImportError:
        return None, selected_device

    if connection_mode == CONNECTION_MODE_TCP and not (wifi_host or "").strip():
        return None, selected_device

    endpoints, _ = inspect_devices(wifi_host=wifi_host)
    candidates = _filter_endpoints_for_mode(endpoints, connection_mode)
    if not candidates:
        return None, selected_device

    if selected_device and selected_device.get("udid"):
        want_udid = _normalize_udid(selected_device["udid"])
        pref = selected_device.get("prefer_transport")
        by_udid = [ep for ep in candidates if ep.udid == want_udid]
        if by_udid:
            if pref == "usb":
                exact = next((ep for ep in by_udid if ep.is_usb), None)
                if exact:
                    return exact, selected_device
            elif pref == "wifi":
                exact = next((ep for ep in by_udid if ep.is_wifi), None)
                if exact:
                    return exact, selected_device
            return by_udid[0], selected_device

        print(f"\n提示：已指定的设备「{selected_device.get('name', 'iPhone')}」当前不可达，以下设备可用：")
        for idx, ep in enumerate(candidates, start=1):
            print(_format_endpoint_line(ep, index=idx))
        raw = prompt("输入要改连的设备编号（回车取消）")
        if not raw:
            raise AppError("已取消连接。")
        try:
            idx = int(raw)
        except ValueError as exc:
            raise AppError("无效的设备编号。") from exc
        if not (1 <= idx <= len(candidates)):
            raise AppError("设备编号超出范围。")
        chosen = candidates[idx - 1]
        return chosen, _endpoint_to_selection(chosen)

    # 未手动指定设备：auto 模式下若同一设备同时在线 USB 与 Wi-Fi，优先保留 USB 避免重复询问
    if connection_mode == CONNECTION_MODE_AUTO:
        deduped: list[DiscoveredEndpoint] = []
        seen_udids: dict[str, int] = {}
        for ep in candidates:
            if ep.udid and ep.udid in seen_udids:
                prev_idx = seen_udids[ep.udid]
                if ep.is_usb and not deduped[prev_idx].is_usb:
                    deduped[prev_idx] = ep
                continue
            if ep.udid:
                seen_udids[ep.udid] = len(deduped)
            deduped.append(ep)
        candidates = deduped

    if len(candidates) == 1:
        return candidates[0], selected_device

    print("\n检测到多个可用设备，请选择要连接的设备：")
    for idx, ep in enumerate(candidates, start=1):
        print(_format_endpoint_line(ep, index=idx))
    raw = prompt("输入设备编号", "1")
    try:
        idx = int(raw)
    except ValueError as exc:
        raise AppError("无效的设备编号。") from exc
    if not (1 <= idx <= len(candidates)):
        raise AppError("设备编号超出范围。")
    chosen = candidates[idx - 1]
    return chosen, _endpoint_to_selection(chosen)


def _choose_connection_mode(config: dict[str, Any]) -> bool:
    print("\n选择连接方式：")
    print("1. 自动（USB 优先，未插线自动走 Wi-Fi）[推荐]")
    print("2. 仅 Wi-Fi 无线连接（忽略 USB 线，强制无线）")
    print("3. 仅 USB 有线连接")
    print("4. 手动 Wi-Fi IP 直连（TCP 62078，mDNS 广播受阻时使用）")
    mapping = {
        "1": CONNECTION_MODE_AUTO,
        "2": CONNECTION_MODE_WIFI,
        "3": CONNECTION_MODE_USB,
        "4": CONNECTION_MODE_TCP,
    }
    current_choice = next((k for k, v in mapping.items() if v == config["connection_mode"]), "1")
    selected = prompt("选择", current_choice)
    new_mode = mapping.get(selected)
    if not new_mode:
        print("无效选择，未修改。")
        return False
    if new_mode == CONNECTION_MODE_TCP and not config.get("wifi_host"):
        ip = prompt("输入 iPhone 局域网 IP（可在 iPhone 设置→无线局域网→i 图标查看）")
        if ip:
            config["wifi_host"] = ip.strip()
        else:
            print("未输入 IP，取消切换为手动直连。")
            return False
    if new_mode != config["connection_mode"]:
        config["connection_mode"] = new_mode
        save_json(CONFIG_PATH, config)
        print(f"已切换为：{CONNECTION_MODE_LABELS[new_mode]}")
        return True
    save_json(CONFIG_PATH, config)
    print("保持当前连接方式。")
    return False


def connection_menu(
    config: dict[str, Any],
    selected_device: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
    """菜单 1：探测 USB / Wi-Fi 设备、选择目标设备、开启 Wi-Fi 同步、切换连接模式。"""
    changed = False
    while True:
        mode = config["connection_mode"]
        wifi_host = config["wifi_host"]
        print("\n" + "-" * 64)
        print("连接管理（USB / Wi-Fi 无线同步 / 设备选择）")
        print("-" * 64)
        print(f"默认连接方式：{CONNECTION_MODE_LABELS.get(mode, mode)}")
        if selected_device:
            print(f"当前指定设备：{selected_device['name']} [{selected_device['transport_label']}]")
        else:
            print("当前指定设备：自动选择（仅 1 台设备时直连，多台时提示选择）")
        print(f"手动 Wi-Fi IP：{wifi_host or '未设置（自动通过 usbmux/Bonjour 发现）'}")
        print("\n正在探测可用设备（USB / Wi-Fi）……")
        endpoints: list[DiscoveredEndpoint] = []
        try:
            endpoints, mux_error = inspect_devices(wifi_host=wifi_host)
            if mux_error:
                print(f"提示：usbmuxd 暂不可用（{mux_error}），已通过 Bonjour/TCP 探测。")
            print("\n发现的设备端点：")
            if endpoints:
                for idx, ep in enumerate(endpoints, start=1):
                    print(_format_endpoint_line(ep, index=idx, selected_device=selected_device))
            else:
                print("  （暂未检测到任何 USB 或 Wi-Fi 设备）")
        except ImportError:
            print_missing_pymobiledevice3()
        except Exception as exc:
            print(f"探测失败：{exc}")

        print("\n操作：")
        print("1. 选择要连接的设备（多设备或指定 USB / Wi-Fi）")
        print("2. 开启 iPhone Wi-Fi 同步（首次需先插一次 USB 线）")
        print("3. 切换默认连接方式（自动 / 仅 Wi-Fi / 仅 USB / 手动 IP）")
        print("4. 设置或清除手动 Wi-Fi IP")
        print("5. 重新扫描设备")
        print("回车. 返回主菜单")
        action = prompt("选择")
        if not action:
            break
        if action == "1":
            selectable = [ep for ep in endpoints if ep.paired and not ep.error]
            if not selectable:
                print("当前没有可选择的已配对设备。请先插上 USB 线信任电脑，或检查 Wi-Fi。")
                press_enter()
                continue
            print("\n可选设备（输入 0 恢复自动选择）：")
            for idx, ep in enumerate(selectable, start=1):
                print(_format_endpoint_line(ep, index=idx, selected_device=selected_device))
            raw = prompt("输入编号（0 恢复自动，回车取消）")
            if not raw:
                continue
            if raw == "0":
                if selected_device is not None:
                    selected_device = None
                    changed = True
                print("已恢复自动选择设备。")
            else:
                try:
                    idx = int(raw)
                except ValueError:
                    print("无效编号。")
                    press_enter()
                    continue
                if 1 <= idx <= len(selectable):
                    chosen = selectable[idx - 1]
                    selected_device = _endpoint_to_selection(chosen)
                    changed = True
                    print(f"已选定设备：{chosen.name} [{chosen.transport_label}]（本次会话有效）。")
                else:
                    print("编号超出范围。")
            press_enter()
        elif action == "2":
            usb_eps = [ep for ep in endpoints if ep.is_usb and not ep.error]
            target_udid = ""
            if len(usb_eps) > 1:
                print("\n检测到多台 USB 设备，请选择要开启 Wi-Fi 同步的设备：")
                for idx, ep in enumerate(usb_eps, start=1):
                    print(_format_endpoint_line(ep, index=idx))
                raw = prompt("输入编号", "1")
                try:
                    idx = int(raw)
                    if 1 <= idx <= len(usb_eps):
                        target_udid = usb_eps[idx - 1].udid
                except ValueError:
                    pass
            result = enable_wifi_sync_over_usb(target_udid=target_udid)
            status = "已开启" if result["enabled_now"] else "未确认开启"
            note = "（之前已开启）" if result["already_enabled"] else "（本次已打开开关）"
            print(f"\n设备：{result['name']} (iOS {result['version']})")
            print(f"Wi-Fi 同步状态：{status} {note}")
            if result["cached_local"]:
                print("已同步保存本机配对记录（无 iTunes 服务时也可直接 Bonjour/TCP 连接）。")
            if result["enabled_now"]:
                print("现在可以拔掉 USB 线，确保手机与电脑在同一 Wi-Fi 下，直接用菜单 2 或菜单 3 导出。")
            press_enter()
        elif action == "3":
            if _choose_connection_mode(config):
                changed = True
            press_enter()
        elif action == "4":
            new_host = prompt("输入 iPhone 的局域网 IP（直接回车保持不变，输入 clear 清除）", wifi_host)
            if new_host.lower() in {"clear", "none", "清除"}:
                new_host = ""
            if new_host != config["wifi_host"]:
                config["wifi_host"] = new_host
                save_json(CONFIG_PATH, config)
                changed = True
                print("已保存手动 Wi-Fi IP。" if new_host else "已清除手动 Wi-Fi IP。")
            else:
                print("没有修改。")
            press_enter()
        elif action == "5":
            continue
        else:
            print("无效选择。")
    return config, selected_device, changed


def settings_menu(config: dict[str, Any]) -> dict[str, Any]:
    fields = [
        ("1", "export_root", "相簿导出目录"),
        ("2", "metadata_root", "相簿元数据缓存目录（Photos.sqlite 临时副本）"),
        ("3", "index_path", "导出索引数据库"),
    ]
    print("\n当前设置：")
    for number, key, label in fields:
        print(f"{number}. {label}：{config[key]}")
    print(f"4. 连接方式：{CONNECTION_MODE_LABELS.get(config['connection_mode'], config['connection_mode'])}")
    print(f"5. 手动 Wi-Fi IP：{config['wifi_host'] or '未设置'}")
    value = prompt("输入要修改的编号，或回车返回")
    if value == "4":
        _choose_connection_mode(config)
        return config
    if value == "5":
        new_host = prompt("新的手动 Wi-Fi IP（输入 clear 清除）", config["wifi_host"])
        if new_host.lower() in {"clear", "none", "清除"}:
            new_host = ""
        if new_host != config["wifi_host"]:
            config["wifi_host"] = new_host
            save_json(CONFIG_PATH, config)
            print("已保存。")
        else:
            print("没有修改。")
        return config
    for number, key, label in fields:
        if value != number:
            continue
        new_path = str(Path(prompt(f"新的{label}", config[key])).expanduser())
        if new_path != config[key]:
            config[key] = new_path
            save_json(CONFIG_PATH, config)
            print("已保存。")
        else:
            print("没有修改。")
        break
    return config


# ============================================================================
# 主循环
# ============================================================================


def main() -> int:
    config = load_config()
    media_source: AFCMediaSourceBase | None = None
    library: PhotosLibrary | None = None
    loaded_transport: str | None = None
    selected_device: dict[str, Any] | None = None

    def drop_current() -> None:
        nonlocal media_source, library, loaded_transport
        if media_source is not None:
            media_source.close()
        media_source = None
        library = None
        loaded_transport = None

    def ensure_loaded(metadata_root: Path) -> tuple[AFCMediaSourceBase, PhotosLibrary]:
        nonlocal media_source, library, loaded_transport, selected_device
        if media_source is None or library is None:
            drop_current()
            media_source, library, selected_device = refresh_and_load_metadata(
                metadata_root,
                connection_mode=config["connection_mode"],
                wifi_host=config["wifi_host"],
                selected_device=selected_device,
            )
            loaded_transport = media_source.transport_label
        return media_source, library

    try:
        while True:
            export_root = Path(config["export_root"]).expanduser()
            metadata_root = Path(config["metadata_root"]).expanduser()
            index_path = Path(config["index_path"]).expanduser()
            mode_label = CONNECTION_MODE_LABELS.get(config["connection_mode"], config["connection_mode"])
            device_label = (
                f"{selected_device['name']} [{selected_device['transport_label']}]"
                if selected_device
                else "自动选择"
            )
            status_text = (
                f"已连接 {loaded_transport}（已加载 {len(library.assets)} 项）"
                if media_source is not None and library is not None and loaded_transport
                else "未连接（选 2 加载或直接选 3 导出时自动连接）"
            )
            print("\n" + "=" * 64)
            print(f"iOS 相簿备份管理 TUI  v{__version__}")
            print("=" * 64)
            print(f"连接方式：{mode_label} | 目标设备：{device_label}")
            print(f"导出目录：{export_root}")
            print(f"当前状态：{status_text}")
            print("\n1. 连接管理与设备选择（USB / Wi-Fi 无线同步）")
            print("2. 连接手机并加载相簿（读取 Photos.sqlite 并列出相簿）")
            print("3. 按相簿导出原始文件")
            print("4. 查看导出记录/重复统计")
            print("5. 生成待删除清单（不直接删除手机）")
            print("6. 设置目录与连接")
            print("0. 退出")
            try:
                choice = prompt("选择")
            except EOFError:
                print("\n输入已结束，退出。")
                break

            try:
                if choice == "1":
                    config, selected_device, changed = connection_menu(config, selected_device)
                    if changed and media_source is not None:
                        drop_current()
                        print("连接设置或目标设备已变更，已断开旧连接；下次操作将按新方式重连。")
                elif choice == "2":
                    drop_current()
                    _, lib = ensure_loaded(metadata_root)
                    list_albums(lib)
                    press_enter()
                elif choice == "3":
                    src, lib = ensure_loaded(metadata_root)
                    export_selected_albums(src, lib, export_root, index_path)
                    press_enter()
                elif choice == "4":
                    show_state_summary(index_path)
                    press_enter()
                elif choice == "5":
                    src, lib = ensure_loaded(metadata_root)
                    make_delete_review(src, lib, export_root)
                    press_enter()
                elif choice == "6":
                    prev_conn = (config["connection_mode"], config["wifi_host"])
                    config = settings_menu(config)
                    if (config["connection_mode"], config["wifi_host"]) != prev_conn and media_source is not None:
                        drop_current()
                        print("连接设置已变更，已断开旧连接；下次操作将按新方式重连。")
                elif choice == "0":
                    break
                else:
                    print("无效选择。")
            except AppError as exc:
                print(f"\n错误：{exc}")
                press_enter()
            except (OSError, sqlite3.Error) as exc:
                print(f"\n本地读写错误：{exc}")
                press_enter()
            except KeyboardInterrupt:
                print("\n已中断当前操作。")
                press_enter()
    finally:
        drop_current()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

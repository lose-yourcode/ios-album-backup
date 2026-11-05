# iOS 相簿备份管理 TUI

这个工具不是 DCIM/PTP 文件浏览器。它的设计目标是按 iPhone「照片」App 里的虚拟相簿结构导出备份。

## 原理

1. 通过 USB 和 `pymobiledevice3 afc` 直接读取 `PhotoData/Photos.sqlite` 相簿元数据。
2. 导出照片/视频时，按数据库里的 `DCIM/...` 路径通过 AFC 直接拉取指定文件。
3. `pymobiledevice3 backup2` 的含媒体逻辑备份只是备用离线方案。
4. 解析 `ZASSET`/`ZGENERICASSET`、`ZGENERICALBUM` 和 `Z_XXASSETS` 关系表。
5. 在文字菜单里选择相簿，按 `相簿名/YYYY-MM-DD/` 目录导出照片和视频。

工具会额外合成这些分类：

- `全部照片视频`
- `隐藏`，来自 Photos 数据库里的 hidden 标记
- `收藏`
- `视频`
- `照片`

## Windows 10 使用

先安装 Python 3，然后在 PowerShell 里执行：

```powershell
py -m pip install -U pymobiledevice3
```

运行：

```powershell
py .\ios_album_backup_tui.py
```

建议流程：

1. 用 USB 连接 iPhone。
2. 解锁 iPhone，并点「信任此电脑」。
3. 菜单 `1` 检查设备。
4. 菜单 `2` 获取数据。建议先选 `仅相簿元数据` 查看结构。
5. 菜单 `4` 查看相簿。
6. 菜单 `5` 按相簿从手机 AFC 直连导出原始文件。

默认目录：

- iOS 备份：`ios_device_backups`
- 相簿元数据：`ios_photo_metadata`
- 相簿导出：`album_exports`
- 导出索引：`ios_album_backup_index.sqlite`

## 重复处理

导出时可以选择：

1. 同一相簿里已经导出过就跳过；同一文件在别的相簿仍正常导出。
2. 只要以前任何相簿导出过，就跳过。
3. 只要以前任何相簿导出过，仍复制，但文件名加 `__DUP`。

每次导出会生成 `export_log_YYYYMMDD_HHMMSS.csv`。

本地长期记录使用 SQLite，不使用臃肿的大 JSON：

- `media_backup`：每个唯一手机媒体一行，记录资产标识、文件来源、文件名、大小、`sha256`、首次/最近导出时间、最近导出路径、累计导出次数。
- `album_backup`：记录某个媒体曾经导出到哪个相簿；同一相簿重复出现会更新次数，不重复堆数据。
- `backup_runs`：每次运行的汇总统计。

所以如果手机上忘记删除某个视频/照片，下次导出同一相簿时脚本会查 SQLite，跳过并在本次 CSV 里写明之前导出的路径、时间和次数。

## 只要 Photos.sqlite 时

如果你只想读取相簿结构，不需要把媒体和 App 存储都备份下来。菜单 `2` 选择 `仅相簿元数据`，脚本会调用：

```powershell
pymobiledevice3 afc pull -i PhotoData/Photos.sqlite LOCAL_FILE
```

脚本还会尝试拉取 `Photos.sqlite-wal` 和 `Photos.sqlite-shm`。这个模式可以查看相簿、隐藏、收藏等结构；导出时脚本会根据数据库里的 `ZDIRECTORY` + `ZFILENAME` 拼出 `DCIM/...` 路径，再通过 `pymobiledevice3 afc pull` 直接拉取指定媒体文件。

`含媒体逻辑备份` 仍保留为备用方案：如果你想先离线保存一份 iOS 允许备份的媒体，再不连接手机地反复导出相簿，可以使用它。

## 删除限制

这个脚本不会直接删除 iPhone 上的照片或视频。

iOS 没有通过 USB 暴露“按 Photos 虚拟相簿安全删除”的公开接口。直接删除 DCIM/AFC 文件可能导致 Photos 数据库和实际文件失配。菜单 `7` 只生成待删除清单，方便你在手机 Photos App 内核对后手动删除。

## 重要限制

- 这不是 NAND/raw image 备份，不包含 iOS 系统分区、完整闪存空闲区、Secure Enclave 原始数据，也不能用于法证级整机镜像恢复。
- 如果开启了 iCloud 照片并使用「优化 iPhone 储存空间」，部分原片可能只在 iCloud，不在本机备份里；脚本会把这些标记为缺失。
- 如果你的 iOS 备份是加密备份，当前脚本可能无法直接读取 `Manifest.db`。可以临时创建未加密备份，或先用 `pyiosbackup` 解密后再加载解密目录。
- Apple 会随 iOS 版本调整 `Photos.sqlite` 结构。脚本使用启发式方式查找资产表和相簿关系表，覆盖常见 iOS 版本，但不保证所有未来版本都完全一致。

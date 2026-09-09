"""磁盘卷、快捷位置与资源管理器集成（Windows 优先，其他平台自动降级）。"""

from __future__ import annotations

import os
import string
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from .formatter import format_size
from .logger import get_logger

log = get_logger("volumes")

_IS_WINDOWS = sys.platform.startswith("win")


@dataclass(slots=True)
class VolumeInfo:
    """一个磁盘分区的摘要信息。"""

    label: str            # 显示名，例如 “本地磁盘 (C:)”
    path: str             # 根路径，例如 “C:\\”
    total: int = 0
    used: int = 0
    free: int = 0

    @property
    def percent(self) -> float:
        """已用空间百分比。"""
        return (self.used / self.total * 100.0) if self.total else 0.0

    def text(self) -> str:
        """“已用 / 总量”描述文本。"""
        if not self.total:
            return self.label
        return f"{self.label} · 已用 {format_size(self.used)} / {format_size(self.total)}"


def list_volumes() -> List[VolumeInfo]:
    """列出本机所有可用分区。

    优先使用 psutil（能拿到卷标），失败时退回 ctypes / 盘符探测，保证不抛异常。
    """
    volumes: List[VolumeInfo] = []
    try:
        import psutil  # 延迟导入：没有装也不影响主流程

        for part in psutil.disk_partitions(all=False):
            mount = part.mountpoint
            try:
                usage = psutil.disk_usage(mount)
            except OSError:
                continue
            name = part.fstype or "磁盘"
            drive = mount.rstrip("\\/") or mount
            volumes.append(VolumeInfo(
                label=f"{_volume_name(mount) or name} ({drive})",
                path=drive if drive.endswith(("\\", "/")) else drive + "\\",
                total=usage.total, used=usage.used, free=usage.free))
        if volumes:
            return volumes
    except Exception as exc:  # pragma: no cover - 依赖缺失时的兜底
        log.debug("psutil 不可用：%s", exc)

    for drive in _fallback_drives():
        try:
            total, used, free = os.disk_usage(drive) if hasattr(os, "disk_usage") else _shutil_usage(drive)
        except OSError:
            continue
        volumes.append(VolumeInfo(label=f"本地磁盘 ({drive})", path=drive,
                                  total=total, used=used, free=free))
    return volumes


def _shutil_usage(path: str) -> Tuple[int, int, int]:
    """用 shutil 获取磁盘用量。"""
    import shutil

    usage = shutil.disk_usage(path)
    return usage.total, usage.used, usage.free


def _fallback_drives() -> List[str]:
    """探测可用盘符（Windows 用 GetLogicalDrives，其它平台用 /）。"""
    if _IS_WINDOWS:
        try:
            import ctypes

            mask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
            return [f"{letter}:\\" for i, letter in enumerate(string.ascii_uppercase)
                    if mask & (1 << i)]
        except Exception:  # pragma: no cover
            return [f"{d}:\\" for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]
    return ["/"]


def _volume_name(mount: str) -> str:
    """获取卷标（如 “系统盘”），失败返回空串。"""
    if not _IS_WINDOWS:
        return ""
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(  # type: ignore[attr-defined]
            mount, buffer, 261)
        if not ok:
            return ""
        volume = buffer.value  # \\?\Volume{guid}\
        name = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(  # type: ignore[attr-defined]
            volume, name, 261, None, None, None, None, 0)
        return name.value if ok else ""
    except Exception:  # pragma: no cover
        return ""


def quick_locations() -> List[Tuple[str, str]]:
    """返回“快捷位置”列表：(显示名, 路径)。"""
    home = Path.home()
    candidates = [
        ("用户主目录", home),
        ("桌面", home / "Desktop"),
        ("文档", home / "Documents"),
        ("下载", home / "Downloads"),
        ("图片", home / "Pictures"),
        ("视频", home / "Videos"),
        ("音乐", home / "Music"),
        ("程序数据", Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))),
    ]
    out: List[Tuple[str, str]] = []
    for label, path in candidates:
        try:
            if path.exists():
                out.append((label, str(path)))
        except OSError:
            continue
    return out


def pick_folder_dialog(parent=None, start: str = "") -> str:
    """调用原生文件夹选择对话框（Qt 可用时）。

    单独抽出该函数，方便在无 GUI 环境（测试）里跳过。
    """
    from PySide6.QtWidgets import QFileDialog  # 延迟导入，core 层不强依赖 Qt

    initial = start if start and os.path.isdir(start) else str(Path.home())
    return QFileDialog.getExistingDirectory(parent, "选择要分析的文件夹", initial,
                                            QFileDialog.Option.ShowDirsOnly)


def open_in_explorer(path: str, select: bool = False) -> bool:
    """在资源管理器中打开目录；``select=True`` 时选中该项。

    :return: 是否成功发起调用
    """
    path = os.path.normpath(path)
    try:
        if _IS_WINDOWS:
            if select and len(path) < 250:
                subprocess.Popen(["explorer", "/select,", path])
            else:
                os.startfile(path)  # type: ignore[attr-defined]
        else:  # 非 Windows 平台降级处理
            subprocess.Popen(["xdg-open", os.path.dirname(path) if select else path])
        return True
    except OSError as exc:
        log.warning("打开资源管理器失败：%s", exc)
        return False


def copy_to_clipboard(text: str) -> None:
    """把文本写入剪贴板（无 Qt 时使用 Windows 原生命令）。"""
    try:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(text)
        return
    except Exception:
        pass
    if _IS_WINDOWS:
        try:
            subprocess.run(["clip"], input=text.encode("utf-16-le"),
                           shell=True, check=False)
        except OSError:  # pragma: no cover
            log.debug("剪贴板写入失败")


def in_trash(path: str) -> bool:
    """粗略判断路径是否位于回收站（用于提示）。"""
    return "$recycle.bin" in path.lower()

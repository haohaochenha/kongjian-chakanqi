"""应用数据目录与文件路径管理（跨机器可用，不写死盘符）。"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = "SpaceViewer"


def data_dir() -> Path:
    """返回存放配置/日志的目录，优先使用 Windows 的 %APPDATA%。"""
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Roaming")
    path = Path(base) / APP_DIR_NAME
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:  # 只读环境等极端情况，退回当前目录
        path = Path.cwd() / APP_DIR_NAME
        path.mkdir(parents=True, exist_ok=True)
    return path


def config_file() -> Path:
    """配置文件路径。"""
    return data_dir() / "settings.json"


def log_file() -> Path:
    """日志文件路径。"""
    return data_dir() / "spaceviewer.log"


def default_export_dir() -> Path:
    """默认导出目录（用户“文档”下，不存在则用数据目录）。"""
    home = Path.home()
    for candidate in (home / "Documents", home):
        if candidate.is_dir():
            return candidate
    return data_dir()

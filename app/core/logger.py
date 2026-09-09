"""日志工具：写入用户目录下的日志文件，方便排查崩溃问题。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional

from .paths import log_file

_CONFIGURED = False


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """获取（并惰性初始化）全局 logger。

    日志同时输出到文件（滚动，单文件 2MB，最多 3 个）和控制台。
    """
    global _CONFIGURED
    root = logging.getLogger("SpaceViewer" if name is None else f"SpaceViewer.{name}")
    if not _CONFIGURED:
        _CONFIGURED = True
        try:
            handler: logging.Handler = RotatingFileHandler(
                log_file(), maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
        except OSError:  # 无法写日志时退回内存输出，绝不能因此让程序崩溃
            handler = logging.NullHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        root.addHandler(handler)
        if len(root.handlers) == 1:
            stream = logging.StreamHandler(sys.stderr)
            stream.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
            root.addHandler(stream)
        root.setLevel(logging.INFO)
    return root


def install_excepthook() -> None:
    """安装全局异常钩子，把未捕获异常写入日志，避免静默退出。"""
    logger = get_logger("crash")
    original = sys.excepthook

    def hook(exc_type, exc, tb):  # pragma: no cover - 仅在真实崩溃时触发
        try:
            logger.critical("未捕获异常", exc_info=(exc_type, exc, tb))
        except Exception:
            pass
        original(exc_type, exc, tb)

    sys.excepthook = hook

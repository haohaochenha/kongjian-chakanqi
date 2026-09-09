"""空间查看器 —— 程序入口。

用法::

    python main.py                 # 正常启动
    python main.py "D:\\项目"      # 启动后立即扫描指定目录

高分屏适配：显式声明 DPI 缩放取整策略，在 125% / 150% 缩放的笔记本上
文字依然锐利，不会被系统二次拉伸模糊。
"""

from __future__ import annotations

import sys
from typing import List, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from app.core.config import ConfigManager
from app.core.logger import get_logger, install_excepthook
from app.ui.main_window import MainWindow
from app.ui.theme import FONT_FAMILY, FONT_FALLBACK, apply_theme

log = get_logger("main")


def _enable_high_dpi() -> None:
    """启用高分屏缩放（Qt6 默认已开启，这里做兼容性显式声明）。"""
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except AttributeError:  # 老版本 Qt 没有该枚举
        pass


def build_app(argv: List[str]) -> Tuple[QApplication, ConfigManager]:
    """创建 QApplication、应用主题与字体，并返回 (app, 配置对象)。"""
    _enable_high_dpi()
    app = QApplication(argv)
    app.setApplicationName("空间查看器")
    app.setOrganizationName("SpaceViewer")
    app.setStyle("Fusion")          # 跨平台基础风格，QSS 的表现更可控

    cfg = ConfigManager()
    scale = float(cfg.get("font_scale") or 1.0)
    apply_theme(app, cfg.theme, scale)

    font = QFont(FONT_FAMILY)
    if not font.exactMatch():
        font = QFont(FONT_FALLBACK)
    font.setPointSizeF(10.0 * scale)
    app.setFont(font)
    return app, cfg


def main(argv: List[str] | None = None) -> int:
    """程序主入口：启动界面并进入事件循环。"""
    argv = list(sys.argv if argv is None else argv)
    install_excepthook()
    app, _cfg = build_app(argv)

    window = MainWindow(_cfg)
    window.show()

    # 命令行里带了路径就自动开扫（singleShot 保证窗口先完成首帧布局）
    target = argv[1].strip() if len(argv) > 1 else ""
    if target:
        QTimer.singleShot(200, lambda: window.start_scan(target))

    log.info("界面启动完成")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

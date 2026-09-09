"""主题与样式（视觉参考 APIFox：低对比中性底色 + 蓝色主色 + 8px 圆角卡片）。

做法：
1. 用 ``Palette`` 数据类集中管理颜色令牌（token），自定义绘制控件（柱状图、
   占比条）也从这里取色，保证 QSS 与 QPainter 两套绘制体系的颜色完全一致；
2. QSS 通过 ``string.Template`` 注入令牌，避免在 CSS 文本里手写颜色值；
3. 提供 ``apply_theme()`` 一键切换，切换时无需重启程序。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from string import Template
from typing import Dict, List

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget

FONT_FAMILY = "Microsoft YaHei UI"
FONT_FALLBACK = "Microsoft YaHei"


@dataclass(slots=True)
class Palette:
    """一套配色方案。"""

    name: str
    window: str          # 窗口底色
    card: str            # 卡片背景
    card_alt: str        # 次级背景（表头、输入框）
    hover: str           # 悬停底色
    pressed: str         # 按下底色
    border: str          # 描边
    divider: str         # 分割线
    text: str            # 主文本
    text_secondary: str  # 次文本
    text_muted: str      # 弱化文本
    accent: str          # 主色
    accent_hover: str
    accent_pressed: str
    accent_soft: str     # 主色浅底（选中行、标签）
    success: str
    warning: str
    danger: str
    chart: List[str] = field(default_factory=list)  # 图表配色序列
    bar_track: str = "#00000000"
    shadow: str = "#26000000"

    def color(self, token: str) -> QColor:
        """按令牌名取 QColor（供 QPainter 使用）。"""
        value = getattr(self, token, None) or "#888888"
        return QColor(value)

    def tokens(self) -> Dict[str, str]:
        """转成 QSS 模板需要的字典。"""
        return {
            "window": self.window, "card": self.card, "card_alt": self.card_alt,
            "hover": self.hover, "pressed": self.pressed, "border": self.border,
            "divider": self.divider, "text": self.text,
            "text_secondary": self.text_secondary, "text_muted": self.text_muted,
            "accent": self.accent, "accent_hover": self.accent_hover,
            "accent_pressed": self.accent_pressed, "accent_soft": self.accent_soft,
            "success": self.success, "warning": self.warning, "danger": self.danger,
            "font": FONT_FAMILY, "shadow": self.shadow, "bar_track": self.bar_track,
        }


DARK = Palette(
    name="dark",
    window="#15171c",
    card="#1d2026",
    card_alt="#23272f",
    hover="#272c35",
    pressed="#2f3540",
    border="#2c313a",
    divider="#262a32",
    text="#e7e9ee",
    text_secondary="#a6adbb",
    text_muted="#6d7684",
    accent="#4d7cfe",
    accent_hover="#6b93ff",
    accent_pressed="#3b64d8",
    accent_soft="#22304d",
    success="#33c88b",
    warning="#f0a53c",
    danger="#f2606a",
    chart=["#4d7cfe", "#33c88b", "#f0a53c", "#a86bfa", "#e879f9", "#38bdf8",
           "#f472b6", "#a3e635", "#fb7185", "#818cf8"],
    bar_track="#2a2f38",
)

LIGHT = Palette(
    name="light",
    window="#f4f5f7",
    card="#ffffff",
    card_alt="#f7f8fa",
    hover="#eef1f6",
    pressed="#e3e8f0",
    border="#e3e6ec",
    divider="#eceef2",
    text="#1d2129",
    text_secondary="#4e5969",
    text_muted="#8a94a6",
    accent="#3370ff",
    accent_hover="#4e8bff",
    accent_pressed="#2457d1",
    accent_soft="#eaf1ff",
    success="#00b42a",
    warning="#ff9a2e",
    danger="#f5483b",
    chart=["#3370ff", "#00b42a", "#ff9a2e", "#722ed1", "#eb4d8b", "#0fc6c2",
           "#f77234", "#9fdb1d", "#6aa1ff", "#a65cf5"],
    bar_track="#eef0f4",
)

_QSS = Template("""
* {
    font-family: "$font", "Microsoft YaHei", "Segoe UI", sans-serif;
    outline: none;
}
QWidget {
    background: $window;
    color: $text;
    font-size: 13px;
}
QWidget#Root { background: $window; }
QLabel { background: transparent; border: none; color: $text; }
QLabel#Muted { color: $text_muted; }
QLabel#Secondary { color: $text_secondary; }
QLabel#H1 { font-size: 17px; font-weight: 700; }
QLabel#H2 { font-size: 14px; font-weight: 600; }
QLabel#Accent { color: $accent; font-weight: 600; }
QLabel#MetricValue { font-size: 19px; font-weight: 700; color: $text; }
QLabel#MetricName { font-size: 11px; color: $text_muted; }
QLabel#Badge {
    background: $accent_soft; color: $accent;
    border-radius: 9px; padding: 2px 9px; font-size: 11px; font-weight: 600;
}
QLabel#DangerBadge {
    background: $danger; color: #ffffff;
    border-radius: 9px; padding: 2px 9px; font-size: 11px; font-weight: 600;
}
QFrame#Card {
    background: $card; border: 1px solid $border; border-radius: 10px;
}
QFrame#Inner {
    background: $card_alt; border: 1px solid $border; border-radius: 8px;
}
QFrame#Divider { background: $divider; border: none; max-height: 1px; }

/* ---------------------------------------------------------------- 按钮 */
QPushButton {
    background: $card_alt; color: $text; border: 1px solid $border;
    border-radius: 7px; padding: 6px 14px; min-height: 20px; font-size: 13px;
}
QPushButton:hover { background: $hover; border-color: $text_muted; }
QPushButton:pressed { background: $pressed; }
QPushButton:disabled { color: $text_muted; background: $card_alt; border-color: $divider; }
QPushButton#Primary {
    background: $accent; color: #ffffff; border: 1px solid $accent; font-weight: 600;
}
QPushButton#Primary:hover { background: $accent_hover; border-color: $accent_hover; }
QPushButton#Primary:pressed { background: $accent_pressed; }
QPushButton#Danger { background: transparent; color: $danger; border: 1px solid $border; }
QPushButton#Danger:hover { background: $danger; color: #ffffff; border-color: $danger; }
QPushButton#Ghost { background: transparent; border: 1px solid transparent; color: $text_secondary; }
QPushButton#Ghost:hover { background: $hover; color: $text; }
QPushButton#Pill {
    background: transparent; border: 1px solid $border; border-radius: 13px;
    padding: 4px 12px; color: $text_secondary; font-size: 12px;
}
QPushButton#Pill:hover { color: $text; border-color: $accent; }
QPushButton#Pill:checked {
    background: $accent_soft; color: $accent; border-color: $accent; font-weight: 600;
}
QPushButton#Icon {
    background: transparent; border: none; border-radius: 6px;
    padding: 4px; color: $text_secondary; font-size: 15px;
}
QPushButton#Icon:hover { background: $hover; color: $text; }

/* ---------------------------------------------------------------- 输入 */
QLineEdit, QSpinBox, QComboBox {
    background: $card_alt; border: 1px solid $border; border-radius: 7px;
    padding: 6px 10px; color: $text; selection-background-color: $accent;
    selection-color: #ffffff;
}
QLineEdit:hover, QSpinBox:hover, QComboBox:hover { border-color: $text_muted; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: $accent; }
QLineEdit#PathInput { font-size: 13px; padding: 7px 12px; background: $card; }
QLineEdit[invalid="true"] { border: 1px solid $danger; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox::down-arrow {
    image: none; border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid $text_muted; margin-right: 8px;
}
QComboBox QAbstractItemView {
    background: $card; border: 1px solid $border; border-radius: 8px;
    padding: 4px; selection-background-color: $accent_soft; selection-color: $accent;
    outline: none;
}
QSpinBox::up-button, QSpinBox::down-button { width: 0; height: 0; border: none; }

/* ---------------------------------------------------------------- 菜单 */
QMenu {
    background: $card; border: 1px solid $border; border-radius: 9px;
    padding: 6px; color: $text;
}
QMenu::item { padding: 6px 26px 6px 14px; border-radius: 6px; }
QMenu::item:selected { background: $accent_soft; color: $accent; }
QMenu::item:disabled { color: $text_muted; }
QMenu::separator { height: 1px; background: $divider; margin: 5px 8px; }

/* ---------------------------------------------------------------- 进度条 */
QProgressBar {
    background: $bar_track; border: none; border-radius: 5px;
    height: 10px; text: none;
}
QProgressBar#Big { height: 14px; border-radius: 7px; }
QProgressBar::chunk {
    border-radius: 5px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 $accent, stop:1 $accent_hover);
}
QProgressBar#Big::chunk { border-radius: 7px; }

/* ---------------------------------------------------------------- 表格 */
QTableView {
    background: $card; alternate-background-color: $card_alt;
    border: 1px solid $border; border-radius: 10px;
    gridline-color: transparent; color: $text;
    selection-background-color: $accent_soft; selection-color: $accent;
    font-size: 13px;
}
QTableView::item { padding: 5px 8px; border: none; }
QTableView::item:hover { background: $hover; }
QTableView::item:selected { background: $accent_soft; color: $accent; }
QHeaderView { background: transparent; }
QHeaderView::section {
    background: $card_alt; color: $text_secondary; border: none;
    border-bottom: 1px solid $border; padding: 8px 10px; font-size: 12px; font-weight: 600;
}
QHeaderView::section:hover { color: $accent; }
QHeaderView::down-arrow, QHeaderView::up-arrow { width: 0; height: 0; }
QTableCornerButton::section { background: $card_alt; border: none; }

/* ---------------------------------------------------------------- 标签页 */
QTabWidget::pane { border: none; }
QTabBar { background: $card_alt; border: 1px solid $border; border-radius: 9px; }
QTabBar::tab {
    background: transparent; color: $text_secondary; border: none;
    padding: 6px 16px; margin: 3px; border-radius: 7px; font-size: 12.5px;
}
QTabBar::tab:hover { color: $text; }
QTabBar::tab:selected { background: $card; color: $accent; font-weight: 600; }

/* ---------------------------------------------------------------- 复选框 */
QCheckBox { background: transparent; color: $text_secondary; spacing: 7px; font-size: 12.5px; }
QCheckBox:hover { color: $text; }
QCheckBox::indicator {
    width: 15px; height: 15px; border-radius: 4px;
    border: 1px solid $text_muted; background: $card;
}
QCheckBox::indicator:hover { border-color: $accent; }
QCheckBox::indicator:checked { background: $accent; border-color: $accent; }

/* ------------------------------------------------------------ 分组框/滚动 */
QGroupBox {
    background: $card; border: 1px solid $border; border-radius: 10px;
    margin-top: 12px; padding: 12px; font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 12px; padding: 0 6px; color: $text_secondary;
}
QScrollArea, QAbstractScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QSplitter::handle { background: $divider; }
QSplitter::handle:horizontal { width: 6px; }
QSplitter#Sidebar::handle { background: transparent; }

/* ---------------------------------------------------------------- 滚动条 */
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical {
    background: $text_muted; border-radius: 4px; min-height: 32px;
}
QScrollBar::handle:vertical:hover { background: $accent; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal {
    background: $text_muted; border-radius: 4px; min-width: 32px;
}
QScrollBar::handle:horizontal:hover { background: $accent; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; background: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

/* ---------------------------------------------------------------- 其它 */
QStatusBar { background: $card; color: $text_muted; border-top: 1px solid $border; }
QToolTip {
    background: $card; color: $text; border: 1px solid $border;
    padding: 6px 9px; border-radius: 6px; font-size: 12px;
}
QMessageBox, QDialog { background: $card; }
QDialogButtonBox QPushButton { min-width: 74px; }
""")


def build_stylesheet(palette: Palette) -> str:
    """根据配色生成 QSS 文本。"""
    return _QSS.substitute(**palette.tokens())


def current_palette() -> Palette:
    """取当前生效的配色（依据 QApplication 上挂的 property）。"""
    app = QApplication.instance()
    name = app.property("svTheme") if app else None
    return LIGHT if name == "light" else DARK


def apply_theme(app: QApplication, theme: str, font_scale: float = 1.0) -> Palette:
    """应用主题（QSS + Qt 原生调色板），返回使用的配色。"""
    palette = LIGHT if theme == "light" else DARK
    app.setProperty("svTheme", palette.name)
    scale = min(1.3, max(0.9, float(font_scale or 1.0)))
    stylesheet = build_stylesheet(palette)
    if scale != 1.0:
        # 通过整体字号缩放实现“界面大小”调节（QSS 不支持相对单位，直接追加规则）
        stylesheet += f"\nQWidget {{ font-size: {round(13 * scale)}px; }}"
    app.setStyleSheet(stylesheet)
    _apply_qpalette(app, palette)
    return palette


def _apply_qpalette(app: QApplication, palette: Palette) -> None:
    """同步 Qt 原生调色板，让未走 QSS 的原生弹窗也保持主题一致。"""
    qp = QPalette()
    qp.setColor(QPalette.ColorRole.Window, QColor(palette.window))
    qp.setColor(QPalette.ColorRole.WindowText, QColor(palette.text))
    qp.setColor(QPalette.ColorRole.Base, QColor(palette.card))
    qp.setColor(QPalette.ColorRole.AlternateBase, QColor(palette.card_alt))
    qp.setColor(QPalette.ColorRole.Text, QColor(palette.text))
    qp.setColor(QPalette.ColorRole.Button, QColor(palette.card_alt))
    qp.setColor(QPalette.ColorRole.ButtonText, QColor(palette.text))
    qp.setColor(QPalette.ColorRole.Highlight, QColor(palette.accent))
    qp.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    qp.setColor(QPalette.ColorRole.ToolTipBase, QColor(palette.card))
    qp.setColor(QPalette.ColorRole.ToolTipText, QColor(palette.text))
    qp.setColor(QPalette.ColorRole.PlaceholderText, QColor(palette.text_muted))
    app.setPalette(qp)


def restpol_repolish(widget: QWidget) -> None:
    """主题切换后强制某个自绘控件重新计算样式。"""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()

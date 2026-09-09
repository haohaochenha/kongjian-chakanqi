"""自绘可视化控件：横向排行条形图 + 环形占比图。

为什么不使用 matplotlib 等第三方库：
* 避免额外依赖与字体缺失问题（中文标签在 matplotlib 里默认是方块）；
* QPainter 直接绘制更省内存、刷新更快，能跟随表格滚动实时联动；
* 配色与 QSS 主题共用同一套令牌，切换深浅色时自动同步。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QLinearGradient, QPainter,
                           QPainterPath, QPen)
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from .theme import current_palette


@dataclass(slots=True)
class ChartItem:
    """图表中的一条数据。"""

    label: str
    value: int
    ratio: float             # 0~1，相对总量的占比
    color: str = ""
    payload: object = None   # 携带目录 id 等上下文，点击时使用
    detail: str = ""         # 提示气泡里的完整文本


class HorizontalBarChart(QWidget):
    """横向条形排行图（适合展示名字很长的目录）。"""

    itemClicked = Signal(object)   # 发送 payload（目录 id）

    ROW_HEIGHT = 34
    BAR_HEIGHT = 14

    def __init__(self, parent: QWidget | None = None, max_rows: int = 10,
                 on_click: Optional[Callable[[object], None]] = None) -> None:
        super().__init__(parent)
        self._items: List[ChartItem] = []
        self._max_rows = max_rows
        self._hover = -1
        self._on_click = on_click
        self._name_width = 150
        # 高度按 max_rows 一次性固定：数据行数变化时布局不再抖动，
        # 这是“扫描中界面闪烁 / 显示框尺寸持续变化”的根因。
        self.setFixedHeight(self.ROW_HEIGHT * max_rows + 8)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # ------------------------------------------------------------------ 数据
    def set_items(self, items: List[ChartItem]) -> None:
        """更新数据（高度保持固定，不足的行留白）。"""
        self._items = items[: self._max_rows]
        self.update()

    def clear(self) -> None:
        """清空数据。"""
        self.set_items([])

    def sizeHint(self) -> QSize:  # noqa: D401 - Qt 重写
        return QSize(320, self.ROW_HEIGHT * self._max_rows + 8)

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        palette = current_palette()
        base_font = self.font()
        name_font = QFont(base_font)
        value_font = QFont(base_font)
        value_font.setPointSizeF(max(8.0, value_font.pointSizeF() * 0.92))
        metrics = QFontMetrics(value_font)

        if not self._items:
            painter.setPen(QColor(palette.text_muted))
            painter.setFont(base_font)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "暂无数据，请先扫描一个文件夹")
            return

        width = self.width()
        # 布局：名称 | 条形 | 数值，空间不足时压缩名称区
        value_width = min(110, metrics.horizontalAdvance("999.99 GB") + 12)
        name_width = max(80, min(self._name_width, width - value_width - 60))
        bar_left = name_width + 10
        bar_right = width - value_width - 8
        bar_span = max(20, bar_right - bar_left)

        for index, item in enumerate(self._items):
            top = index * self.ROW_HEIGHT + 4
            row_rect = QRectF(0, top, width, self.ROW_HEIGHT - 4)

            if index == self._hover:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(palette.hover))
                painter.drawRoundedRect(row_rect.adjusted(0, 0, -1, 0), 6, 6)

            # 名称
            painter.setFont(name_font)
            painter.setPen(QColor(palette.text if index == self._hover
                                  else palette.text_secondary))
            name = QFontMetrics(name_font).elidedText(
                item.label, Qt.TextElideMode.ElideMiddle, name_width - 4)
            painter.drawText(QRectF(2, top, name_width - 6, self.ROW_HEIGHT - 4),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             name)

            # 轨道 + 条
            track = QRectF(bar_left, top + (self.ROW_HEIGHT - 4 - self.BAR_HEIGHT) / 2,
                           bar_span, self.BAR_HEIGHT)
            painter.setPen(QPen(QColor(palette.border), 1))
            painter.setBrush(QColor(palette.bar_track))
            painter.drawRoundedRect(track, 4, 4)

            ratio = 0.0 if item.ratio < 0 else min(1.0, max(0.0, item.ratio))
            fill_width = max(6.0, track.width() * ratio) if ratio > 0 else 0.0
            if fill_width > 0:
                color = QColor(item.color or palette.chart[index % len(palette.chart)])
                gradient = QLinearGradient(track.topLeft(), track.bottomRight())
                gradient.setColorAt(0.0, color.lighter(118))
                gradient.setColorAt(1.0, color.darker(108))
                path = QPainterPath()
                path.addRoundedRect(QRectF(track.x(), track.y(), fill_width, track.height()),
                                    4, 4)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(gradient)
                painter.drawPath(path)

            # 数值
            painter.setFont(value_font)
            painter.setPen(QColor(palette.text))
            painter.drawText(QRectF(bar_right + 8, top, value_width - 6,
                                    self.ROW_HEIGHT - 4),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                             item.detail or _short(item.value))
        painter.end()

    # ------------------------------------------------------------------ 交互
    def _row_at(self, pos) -> int:
        if not self._items:
            return -1
        index = int(pos.y() // self.ROW_HEIGHT)
        return index if 0 <= index < len(self._items) else -1

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        index = self._row_at(event.position())
        if index != self._hover:
            self._hover = index
            self.update()
        if index >= 0:
            item = self._items[index]
            QToolTip.showText(event.globalPosition().toPoint(),
                              item.detail and f"{item.label}\n{item.detail}" or item.label,
                              self)
        else:
            QToolTip.hideText()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover != -1:
            self._hover = -1
            self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        index = self._row_at(event.position())
        if index >= 0:
            item = self._items[index]
            if self._on_click:
                self._on_click(item.payload)
            self.itemClicked.emit(item.payload)


def _short(num_bytes: int) -> str:
    """图表上用的紧凑大小文本。"""
    from ..core.formatter import format_size

    return format_size(num_bytes, decimals=1)


class DonutChart(QWidget):
    """环形占比图（用于文件类型分布）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: List[ChartItem] = []
        self._hover = -1
        self.setMouseTracking(True)
        # 高度固定 190：环直径恒为 ~182px。
        # 之前垂直方向是 Expanding，窗口高度/相邻卡片内容变化时环会被
        # 压缩或拉伸，视觉上就是“饼图位置与大小不稳定”。
        self.setMinimumWidth(168)
        self.setFixedHeight(190)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_items(self, items: List[ChartItem]) -> None:
        """更新数据（最多 8 段，其余合并显示为“其他”由调用方处理）。"""
        self._items = items[:8]
        self.update()

    def sizeHint(self) -> QSize:  # noqa: D401
        return QSize(190, 190)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = current_palette()
        side = min(self.width(), self.height()) - 8
        if side <= 10:
            return
        rect = QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)
        hole = side * 0.62
        total = sum(item.value for item in self._items) or 1

        painter.setPen(QPen(QColor(palette.card), 2))
        start = -90.0 * 16
        for index, item in enumerate(self._items):
            span = int(round(360.0 * 16 * item.value / total))
            if span == 0:
                continue
            color = QColor(item.color or palette.chart[index % len(palette.chart)])
            if index == self._hover:
                color = color.lighter(120)
            painter.setBrush(color)
            painter.drawPie(rect, int(start), span)
            start += span

        # 中间镂空 + 汇总文本
        hole_rect = QRectF((self.width() - hole) / 2, (self.height() - hole) / 2, hole, hole)
        painter.setBrush(QColor(palette.card))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(hole_rect)

        painter.setPen(QColor(palette.text))
        font = QFont(self.font())
        font.setPointSizeF(font.pointSizeF() * 1.12)
        font.setBold(True)
        painter.setFont(font)
        caption = self._items[self._hover].label if 0 <= self._hover < len(self._items) \
            else "总占用"
        value = (_short(self._items[self._hover].value)
                 if 0 <= self._hover < len(self._items) else _short(total))
        painter.drawText(hole_rect.adjusted(4, -6, -4, -6),
                         Qt.AlignmentFlag.AlignCenter, value)
        font.setBold(False)
        font.setPointSizeF(max(7.5, font.pointSizeF() * 0.78))
        painter.setFont(font)
        painter.setPen(QColor(palette.text_muted))
        painter.drawText(hole_rect.adjusted(4, 12, -4, 18),
                         Qt.AlignmentFlag.AlignCenter,
                         caption[:14])
        painter.end()

    def _index_at(self, pos: QPointF) -> int:
        """根据鼠标位置判断落在哪个扇区。"""
        if not self._items:
            return -1
        side = min(self.width(), self.height()) - 8
        center = QPointF(self.width() / 2.0, self.height() / 2.0)
        dx, dy = pos.x() - center.x(), pos.y() - center.y()
        if (dx * dx + dy * dy) > (side / 2.0) ** 2:
            return -1
        import math

        angle = (math.degrees(math.atan2(dy, dx)) + 90.0) % 360.0
        total = sum(item.value for item in self._items) or 1
        cursor = 0.0
        for index, item in enumerate(self._items):
            cursor += 360.0 * item.value / total
            if angle <= cursor:
                return index
        return -1

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        index = self._index_at(event.position())
        if index != self._hover:
            self._hover = index
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover != -1:
            self._hover = -1
            self.update()


class MiniBar(QWidget):
    """一个小占比条，用于“磁盘占用”这类行内展示。"""

    def __init__(self, parent: QWidget | None = None, height: int = 8) -> None:
        super().__init__(parent)
        self._ratio = 0.0
        self._color = ""
        self.setFixedHeight(height)
        self.setMinimumWidth(60)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, ratio: float, color: str = "") -> None:
        """设置 0~1 的比值与可选颜色。"""
        self._ratio = min(1.0, max(0.0, float(ratio)))
        self._color = color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = current_palette()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(palette.border), 1))
        painter.setBrush(QColor(palette.bar_track))
        painter.drawRoundedRect(rect, 3, 3)
        if self._ratio > 0:
            color = QColor(self._color or palette.accent)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(rect.x(), rect.y(),
                                           max(3.0, rect.width() * self._ratio),
                                           rect.height()), 3, 3)
        painter.end()

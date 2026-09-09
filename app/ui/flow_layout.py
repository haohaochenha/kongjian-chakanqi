"""自动换行布局（FlowLayout）。

用途：顶部工具条在窗口变窄时自动折行，实现“响应式布局”。
Qt 只在 C++ 示例里提供了 FlowLayout，这里给出 PySide6 版实现，
并正确支持 heightForWidth（否则父布局无法知道折叠后的高度）。
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import (QLayout, QLayoutItem, QSizePolicy, QSpacerItem,
                               QWidget, QWidgetItem)


class FlowLayout(QLayout):
    """水平排列、放不下时自动换行的布局。"""

    def __init__(self, parent: Optional[QWidget] = None, margin: int = 0,
                 h_spacing: int = 8, v_spacing: int = 8) -> None:
        super().__init__(parent)
        if margin >= 0:
            self.setContentsMargins(margin, margin, margin, margin)
        self._h_spacing = max(0, h_spacing)
        self._v_spacing = max(0, v_spacing)
        self._items: List[QLayoutItem] = []

    # ------------------------------------------------------------------ 条目管理
    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 - Qt 重写
        self._items.append(item)
        self.invalidate()

    def addSpacing(self, space: int) -> None:  # noqa: N802 - Qt 重写
        self._items.append(QSpacerItem(max(0, int(space)), 0,
                                       QSizePolicy.Policy.Fixed,
                                       QSizePolicy.Policy.Minimum))

    def addStretch(self, stretch: int = 0) -> None:  # noqa: N802 - Qt 重写
        """兼容 QBoxLayout 的占位弹簧。

        流式布局本身按“从左到右、放不下换行”排布，没有“把剩余空间撑开”的概念，
        因此这里只放入一个零尺寸的可扩展间隔项，保证调用方 API 一致而不报错。
        """
        self._items.append(QSpacerItem(0, 0, QSizePolicy.Policy.Expanding,
                                       QSizePolicy.Policy.Minimum))

    def add_widget(self, widget: QWidget) -> None:
        """追加控件（沿用基类实现，会自动 reparent 并回调 addItem）。"""
        self.addWidget(widget)

    def insert_widget(self, index: int, widget: QWidget) -> None:
        """在指定位置插入控件。"""
        parent = self.parentWidget()
        if parent is not None and widget.parent() is not parent:
            widget.setParent(parent)
        self._items.insert(max(0, min(index, len(self._items))), QWidgetItem(widget))
        self.invalidate()

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> Optional[QLayoutItem]:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> Optional[QLayoutItem]:  # noqa: N802
        """取出条目（基类在销毁布局时会调用）。"""
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802
        return Qt.Orientation(0)

    # ------------------------------------------------------------------ 尺寸计算
    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, max(0, width), 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(),
                      margins.top() + margins.bottom())
        return size

    # ------------------------------------------------------------------ 事件
    def eventFilter(self, watched: QWidget, event: QEvent) -> bool:  # noqa: N802
        """控件尺寸变化（如字体改变）时让布局重新计算。"""
        if event.type() == QEvent.Type.LayoutRequest and watched.isVisible() \
                and self.parentWidget() is watched:
            self.invalidate()
        return super().eventFilter(watched, event)

    # ------------------------------------------------------------------ 排布
    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        """执行换行排布。

        :param test_only: True 时只计算并返回总高度，不移动控件
        :return: 布局所需高度
        """
        margins = self.contentsMargins()
        effective = QRect(rect.x() + margins.left(), rect.y() + margins.top(),
                          rect.width() - margins.left() - margins.right(),
                          rect.height() - margins.top() - margins.bottom())
        x, y = effective.x(), effective.y()
        line_height = 0

        for item in self._items:
            widget = item.widget()
            if widget is not None and not widget.isVisible():
                continue
            hint = item.sizeHint()
            space_x = self._h_spacing
            next_x = x + hint.width() + space_x
            if line_height > 0 and next_x - space_x > effective.right():
                # 换行
                x = effective.x()
                y += line_height + self._v_spacing
                next_x = x + hint.width() + space_x
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), QSize(hint.width(), hint.height())))
            x = next_x
            line_height = max(line_height, hint.height())

        return y + line_height - rect.y() + margins.bottom()

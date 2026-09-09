"""文件夹排行的表格模型与自绘代理。

性能要点：
* 模型只保存 **目录 id 列表**（不是字典副本），排序也是针对 id 列表进行，
  几十万条记录时排序依然很快；
* 单元格文本在 ``data()`` 中按需格式化，不做预生成，避免为隐藏行浪费 CPU；
* 大小列由 :class:`RatioBarDelegate` 直接绘制占比条，
  比“每行塞一个进度条控件”省掉数量级级别的内存与重绘开销。
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, Qt, Signal)
from PySide6.QtGui import QColor, QIcon, QPainter
from PySide6.QtWidgets import QStyledItemDelegate, QStyle

from ..core.formatter import format_count, format_percent, format_size, format_timestamp, percent_value
from ..core.models import LOOSE_ID, DirRecord, ScanResult
from .theme import current_palette

# 自定义数据角色
ROLE_DIR_ID = Qt.ItemDataRole.UserRole + 1
ROLE_BYTES = Qt.ItemDataRole.UserRole + 2
ROLE_RATIO = Qt.ItemDataRole.UserRole + 3
ROLE_PATH = Qt.ItemDataRole.UserRole + 4
ROLE_SORT = Qt.ItemDataRole.UserRole + 5

COLUMNS: Sequence[tuple] = (
    ("名称", 340, Qt.AlignmentFlag.AlignLeft),
    ("大小", 120, Qt.AlignmentFlag.AlignRight),
    ("占比", 92, Qt.AlignmentFlag.AlignRight),
    ("文件数", 92, Qt.AlignmentFlag.AlignRight),
    ("子目录", 86, Qt.AlignmentFlag.AlignRight),
    ("修改时间", 138, Qt.AlignmentFlag.AlignLeft),
)

# 列 -> 排序键
COLUMN_SORT_KEY = {0: "name", 1: "size", 2: "percent", 3: "files", 4: "subdirs", 5: "modified"}


def folder_icon() -> QIcon:
    """系统文件夹图标（带缓存）。"""
    global _FOLDER_ICON
    try:
        return _FOLDER_ICON
    except NameError:
        pass
    from PySide6.QtWidgets import QFileIconProvider

    _FOLDER_ICON = QFileIconProvider().icon(QFileIconProvider.IconType.Folder)
    return _FOLDER_ICON


def file_icon() -> QIcon:
    """系统文件图标（带缓存，“散文件”虚拟行使用）。"""
    global _FILE_ICON
    try:
        return _FILE_ICON
    except NameError:
        pass
    from PySide6.QtWidgets import QFileIconProvider

    _FILE_ICON = QFileIconProvider().icon(QFileIconProvider.IconType.File)
    return _FILE_ICON


class FolderTableModel(QAbstractTableModel):
    """文件夹排行表模型。"""

    sortChangedByHeader = Signal(int, bool)  # 列号、是否降序

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._result: Optional[ScanResult] = None
        self._rows: List[DirRecord] = []          # 当前显示的记录（已排序、已限量）
        self._base_bytes: int = 0                 # 占比分母
        self._max_bytes: int = 0                  # 条形图基准（当前视图最大值）
        self._unit: str = "auto"
        self._decimals: int = 2
        self._filter: str = ""
        self._sort_key: str = "size"
        self._sort_desc: bool = True

    # ------------------------------------------------------------ 数据装配
    def set_data(self, records: Sequence[DirRecord], base_bytes: int,
                 result: ScanResult) -> None:
        """替换整表数据（不改变当前排序设置）。

        注意：不要用 begin/endResetModel——那会让视图整表重置：
        滚动位置归零、表头/选中被重建、全部行重绘，肉眼看到的就是
        “每次刷新整个界面都在晃”（闪烁的真正根因）。
        这里改为「尾部行增删 + dataChanged」的最小化更新。
        """
        self._result = result
        self._base_bytes = max(1, int(base_bytes))
        self._apply_sort(source=list(records))

    def set_display_options(self, unit: str = None, decimals: int = None) -> None:
        """更新单位/小数位设置并重绘（不重建行）。"""
        if unit is not None:
            self._unit = unit
        if decimals is not None:
            self._decimals = max(0, min(4, int(decimals)))
        if self._rows:
            top_left = self.index(0, 0)
            bottom_right = self.index(self.rowCount() - 1, self.columnCount() - 1)
            self.dataChanged.emit(top_left, bottom_right)

    def set_filter(self, text: str) -> None:
        """按名称包含关系过滤当前行集合（内容未变化时直接返回，不再重排）。"""
        new_filter = (text or "").strip().lower()
        if new_filter == self._filter:
            return
        self._filter = new_filter
        self._apply_sort()

    def set_sort(self, key: str, desc: bool) -> None:
        """设置排序方式（``size`` / ``name`` / ``files`` / ``subdirs`` / ``modified``）。"""
        if key in {"size", "name", "files", "subdirs", "modified", "percent"}:
            changed = (self._sort_key, self._sort_desc) != (key, bool(desc))
            self._sort_key, self._sort_desc = key, bool(desc)
            if changed:
                # 排序箭头画在表头文本里，模型不复位时需要手动通知表头刷新
                self.headerDataChanged.emit(Qt.Orientation.Horizontal,
                                            0, len(COLUMNS) - 1)
            self._apply_sort()

    def _apply_sort(self, source: Optional[List[DirRecord]] = None) -> None:
        """重新排序 + 过滤（记录数受 Top N 限制，代价很低）。

        :param source: 传入时表示“整表换数据”，否则对当前行集合排序/过滤
        """
        rows = list(source) if source is not None else list(self._rows)
        key = self._sort_key
        desc = self._sort_desc
        if key == "name":
            # 名称排序始终让 A-Z 与 Z-A 互换，不受“降序”语义影响
            rows.sort(key=lambda r: r.name.casefold(), reverse=not desc)
        elif key == "files":
            rows.sort(key=lambda r: (r.total_files, r.total_bytes), reverse=desc)
        elif key == "subdirs":
            rows.sort(key=lambda r: (r.total_subdirs, r.total_bytes), reverse=desc)
        elif key == "modified":
            rows.sort(key=lambda r: r.mtime, reverse=desc)
        else:  # size / percent 分母相同，直接按字节排
            rows.sort(key=lambda r: r.total_bytes, reverse=desc)
        if self._filter:
            rows = [r for r in rows if self._filter in r.name.casefold()]
        # “散文件”等虚拟聚合行（id=LOOSE_ID）不参与排行，固定排在列表末尾
        virtual = [r for r in rows if r.id == LOOSE_ID]
        if virtual and len(virtual) != len(rows):
            rows = [r for r in rows if r.id != LOOSE_ID] + virtual
        self._max_bytes = max((r.total_bytes for r in rows if r.id != LOOSE_ID),
                              default=0)
        self._replace_rows(rows)

    def _replace_rows(self, rows: List[DirRecord]) -> None:
        """把排序/过滤后的行写入模型，尽量少地惊动视图。

        行数变化只在尾部做 begin/endInsert/RemoveRows，
        内容变化用一次 dataChanged 覆盖——
        滚动位置、列宽、表头分区状态全部保留，视图不会整表重绘。
        """
        old_count = len(self._rows)
        new_count = len(rows)
        if new_count < old_count:
            self.beginRemoveRows(QModelIndex(), new_count, old_count - 1)
            self._rows = rows
            self.endRemoveRows()
        elif new_count > old_count:
            self.beginInsertRows(QModelIndex(), old_count, new_count - 1)
            self._rows = rows
            self.endInsertRows()
        else:
            self._rows = rows
        if new_count:
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(new_count - 1, self.columnCount() - 1))

    # ------------------------------------------------------------ Qt 接口
    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        base = super().flags(index)
        return base | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            title = COLUMNS[section][0]
            if 0 <= section < len(COLUMNS) and COLUMN_SORT_KEY.get(section) == self._sort_key:
                title += "  ↓" if self._sort_desc else "  ↑"
            return title
        if role == Qt.ItemDataRole.TextAlignmentRole and orientation == Qt.Orientation.Horizontal:
            return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        if role == Qt.ItemDataRole.ToolTipRole and orientation == Qt.Orientation.Horizontal:
            return "点击可切换排序"
        return None

    def _record(self, index: QModelIndex) -> Optional[DirRecord]:
        if not index.isValid():
            return None
        row = index.row()
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        record = self._record(index)
        if record is None or not index.isValid():
            return None
        column = index.column()
        result = self._result

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            if role == Qt.ItemDataRole.ToolTipRole:
                if record.id == LOOSE_ID:
                    # 虚拟“散文件”行：没有真实路径，说明它的含义与用法
                    return (f"{record.name}\n本目录直属文件的合计（不含子目录内容）\n"
                            "双击可查看这些文件的清单")
                path = result.full_path(record.id) if result else record.name
                return (f"{path}\n大小：{format_size(record.total_bytes, self._unit, self._decimals)}"
                        f"\n文件：{format_count(record.total_files)}   "
                        f"子目录：{format_count(record.total_subdirs)}"
                        + ("\n（存在无法访问的内容）" if record.error else "")
                        + ("\n（受层级/数量上限影响，未完全统计）" if record.truncated else ""))
            if column == 0:
                return record.name or (result.root_path if result else "")
            if column == 1:
                return format_size(record.total_bytes, self._unit, self._decimals)
            if column == 2:
                return format_percent(record.total_bytes, self._base_bytes)
            if column == 3:
                return format_count(record.total_files)
            if column == 4:
                return format_count(record.total_subdirs)
            if column == 5:
                return format_timestamp(record.mtime)
            return None

        if role == Qt.ItemDataRole.TextAlignmentRole:
            return COLUMNS[column][2] | Qt.AlignmentFlag.AlignVCenter
        if role == Qt.ItemDataRole.DecorationRole and column == 0:
            return folder_icon()
        if role == Qt.ItemDataRole.ForegroundRole:
            palette = current_palette()
            if column == 2:
                return QColor(palette.text_secondary)
            if record.error and column == 0:
                return QColor(palette.warning)
        if role == ROLE_DIR_ID:
            return record.id
        if role == ROLE_BYTES:
            return record.total_bytes
        if role == ROLE_RATIO:
            return percent_value(record.total_bytes, self._max_bytes or self._base_bytes)
        if role == ROLE_PATH:
            return result.full_path(record.id) if result else ""
        if role == ROLE_SORT:
            return record.name
        if role == Qt.ItemDataRole.SizeHintRole:
            return None
        return None

    # ------------------------------------------------------------ 便捷访问
    def dir_id(self, row: int) -> int:
        """某行的目录 id，越界返回 -1。"""
        if 0 <= row < len(self._rows):
            return self._rows[row].id
        return -1

    def record(self, row: int) -> Optional[DirRecord]:
        """某行的记录对象。"""
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def row_count(self) -> int:
        """当前行数。"""
        return len(self._rows)

    def all_rows(self) -> List[DirRecord]:
        """当前显示的全部行（导出时使用）。"""
        return list(self._rows)

    @property
    def sort_key(self) -> str:
        """当前排序字段。"""
        return self._sort_key

    @property
    def sort_desc(self) -> bool:
        """当前是否降序。"""
        return self._sort_desc


class RatioBarDelegate(QStyledItemDelegate):
    """在“大小”列背景绘制占比条，并绘制层级缩进的小圆点。"""

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        palette = current_palette()
        ratio = float(index.data(ROLE_RATIO) or 0.0)
        rect = option.rect
        if ratio > 0:
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            track = rect.adjusted(6, 6, -6, -6)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(palette.bar_track))
            painter.drawRoundedRect(track, 4, 4)
            width = max(4.0, track.width() * min(1.0, ratio))
            color = QColor(palette.accent)
            color.setAlphaF(0.32 if option.state & QStyle.StateFlag.State_Selected else 0.24)
            painter.setBrush(color)
            bar = track.adjusted(0, 0, -(track.width() - width), 0)
            painter.drawRoundedRect(bar, 4, 4)
            painter.restore()
        super().paint(painter, option, index)

    def sizeHint(self, option, index: QModelIndex):  # noqa: N802
        size = super().sizeHint(option, index)
        size.setHeight(max(28, size.height()))
        return size

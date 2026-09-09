"""“散文件”清单对话框：列出某个目录直属文件的明细。

背景：排行列表默认只显示子文件夹，目录下零散的文件被聚合成一行
“（散文件 N 个 · XX）”。当用户双击/查看该行时，由本对话框即时扫描
该目录的**第一层**文件（不递归、不改盘），按大小从大到小列出。

性能与健壮性说明：
* ``os.scandir`` 只遍历一层，几十万个条目也能在毫秒级完成；
* 表格最多展示 :data:`MAX_ROWS` 行（已按大小排序，即“最大的前 N 个”），
  避免极端目录把界面卡住；
* 单个文件 ``stat`` 失败只跳过该文件，不影响整体清单。
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout,
                               QHeaderView, QLabel, QPushButton, QTableWidget,
                               QTableWidgetItem, QWidget)

from ..core.formatter import format_count, format_size, format_timestamp
from ..core.logger import get_logger
from ..core.volumes import open_in_explorer

log = get_logger("loose_dialog")

MAX_ROWS = 500   # 最多展示的文件行数（超出时保留最大的，防止界面卡顿）

# 表格列：(标题, 对齐方式)
HEADERS = (
    ("文件名", Qt.AlignmentFlag.AlignLeft),
    ("大小", Qt.AlignmentFlag.AlignRight),
    ("修改时间", Qt.AlignmentFlag.AlignLeft),
)


def list_loose_files(directory: str) -> Tuple[List[Tuple[str, int, float]], int, int]:
    """扫描 directory 第一层的文件明细。

    :param directory: 要列出的目录路径
    :return: (文件列表, 文件总数, 字节合计)
             文件列表元素为 ``(名称, 字节数, 修改时间戳)``，已按大小降序排序；
             目录读取失败时返回 ``([], 0, 0)``，不抛异常（由界面层提示）。
    """
    entries: List[Tuple[str, int, float]] = []
    try:
        with os.scandir(directory) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        continue                     # 子目录不列入“散文件”
                    stat = entry.stat(follow_symlinks=False)
                    entries.append((entry.name, stat.st_size, stat.st_mtime))
                except OSError:                      # 单个文件失效不影响整体
                    continue
    except OSError as exc:
        log.warning("列出散文件失败：%s", exc)
        return [], 0, 0
    entries.sort(key=lambda item: item[1], reverse=True)
    return entries, len(entries), sum(size for _, size, _ in entries)


class LooseFilesDialog(QDialog):
    """展示某个目录直属文件清单的对话框。

    :param directory: 目录路径
    :param total_bytes: 扫描引擎统计的直属文件字节合计（标题栏展示，与排行行一致）
    :param total_files: 扫描引擎统计的直属文件个数
    :param unit: 大小显示单位（"auto" / "B" / "KB" / …）
    :param decimals: 大小显示小数位
    :param parent: 父窗口
    """

    def __init__(self, directory: str, total_bytes: int, total_files: int,
                 unit: str = "auto", decimals: int = 2,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.directory = directory
        # 注意：先算好目录名再进 f-string（Python 3.11 不允许表达式里出现反斜杠）
        dir_name = os.path.basename(directory.rstrip("\\/")) or directory
        self.setWindowTitle(f"散文件清单 · {dir_name}")
        self.resize(720, 480)

        # 即时扫描一层文件。标题数字以扫描引擎的统计为准（与排行行一致），
        # 表格内容为当前实际文件——两者若有差异会给出提示。
        files, file_count, _byte_sum = list_loose_files(directory)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        title = QLabel(f"共 {format_count(total_files)} 个直属文件 · "
                       f"{format_size(total_bytes, unit, decimals)}", self)
        layout.addWidget(title)

        notes: List[str] = []
        if file_count != total_files:
            notes.append("目录内容与扫描结果已不一致（文件可能被增删），以下为当前实际内容")
        if file_count > MAX_ROWS:
            notes.append(f"文件较多，仅列出最大的 {MAX_ROWS} 个")
        if not files and total_files > 0:
            notes.append("未能读取到文件（目录可能已被移动、删除或权限不足）")
        if notes:
            note = QLabel("；".join(notes) + "。", self)
            note.setObjectName("Muted")
            note.setWordWrap(True)
            side.addWidget(note)

        # 文件明细表：文件名 / 大小 / 修改时间
        shown = files[:MAX_ROWS]
        self.table = QTableWidget(len(shown), len(HEADERS), self)
        self.table.setHorizontalHeaderLabels([h for h, _ in HEADERS])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(False)

        for row, (name, size, mtime) in enumerate(shown):
            name_item = QTableWidgetItem(name)
            name_item.setToolTip(os.path.join(directory, name))
            # 完整路径存入 UserRole，双击时用于在资源管理器中定位
            name_item.setData(Qt.ItemDataRole.UserRole, os.path.join(directory, name))
            size_item = QTableWidgetItem(format_size(size, unit, decimals))
            size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                       | Qt.AlignmentFlag.AlignVCenter)
            time_item = QTableWidgetItem(format_timestamp(mtime))
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, size_item)
            self.table.setItem(row, 2, time_item)
        self.table.itemDoubleClicked.connect(self._open_selected)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close_btn = QPushButton("关闭", self)
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

    # ------------------------------------------------------------------ 交互
    def _open_selected(self, item: QTableWidgetItem) -> None:
        """双击文件行 → 在资源管理器中定位该文件（失败只记日志）。"""
        path = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not path:
            return
        try:
            open_in_explorer(path, select=True)
        except OSError as exc:
            log.warning("无法在资源管理器中定位文件：%s", exc)

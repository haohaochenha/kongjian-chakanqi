"""主窗口：把扫描引擎、表格模型、图表与偏好设置组装成完整应用。

布局结构（自上而下）::

    ┌ 标题栏（应用名 / 选项 / 主题 / 帮助）───────────────────────┐
    │ 路径栏 PathBar（输入框 + 快捷位置 + 磁盘 + 浏览）           │
    ├─ 左：结果区 ────────────────────────┬─ 右：侧栏（可滚动）──┤
    │ 面包屑 + 工具条（排序/单位/筛选/导出）│ 扫描进度 ScanPanel   │
    │ 排行表格 QTableView                  │ 扫描概览指标         │
    │ 可视化排行条形图                     │ 类型分布环形图       │
    │                                      │ 磁盘空间占用         │
    └──────────────────────────────────────┴──────────────────────┘

线程模型：扫描在 :class:`ScanThread` 中执行，界面只通过信号接收
``ScanProgress`` / ``ScanResult``，并用一个定时器节流刷新，保证 UI 不卡顿。
"""

from __future__ import annotations

import heapq
import os
import time
from typing import Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QFrame,
                               QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMenu, QMessageBox, QPushButton,
                               QScrollArea, QSplitter, QTableView,
                               QVBoxLayout,
                               QWidget, QWidgetAction)

from ..core import exporter, paths
from ..core.config import ConfigManager
from ..core.engine import ScanOptions
from ..core.formatter import (UNIT_CHOICES, format_count, format_duration,
                              format_size, format_speed)
from ..core.logger import get_logger
from ..core.models import LOOSE_ID, DirRecord, ScanResult, rows_for_export
from ..core.volumes import (copy_to_clipboard, in_trash, list_volumes,
                            open_in_explorer, pick_folder_dialog)
from .ai_dialog import AiAnalysisDialog
from .charts import ChartItem, DonutChart, HorizontalBarChart, MiniBar
from .flow_layout import FlowLayout
from .loose_files_dialog import LooseFilesDialog
from .scan_thread import ScanThread
from .table_model import (COLUMNS, COLUMN_SORT_KEY, ROLE_DIR_ID,
                          FolderTableModel, RatioBarDelegate)
from .theme import apply_theme, current_palette, restpol_repolish
from .widgets import (Breadcrumb, Card, MenuButton, MetricTile, PathBar,
                      PillButton, ScanPanel, build_help_text)

log = get_logger("ui")

APP_TITLE = "空间查看器"
APP_SUBTITLE = "文件夹大小分析与排行"

# 排序菜单：显示名 -> 模型排序键
SORT_LABELS: Tuple[Tuple[str, str], ...] = (
    ("按大小", "size"),
    ("按名称", "name"),
    ("按文件数", "files"),
    ("按子目录数", "subdirs"),
    ("按修改时间", "modified"),
)

TOP_N_CHOICES = (50, 100, 200, 500, 1000, 5000)


class MainWindow(QWidget):
    """应用主窗口（用 QWidget 而非 QMainWindow，避免多余的状态栏样式适配）。"""

    def __init__(self, config: Optional[ConfigManager] = None) -> None:
        super().__init__()
        self.setObjectName("Root")
        self.cfg = config or ConfigManager()

        # -------------------------------------------------------- 运行状态
        self._thread: Optional[ScanThread] = None
        self._result: Optional[ScanResult] = None
        self._current_id: int = -1          # 当前浏览的目录 id
        self._last_percent_shown = 0.0
        self._scan_started = 0.0
        self._dirty = False                 # 是否有待刷新的数据
        self._closing = False

        # -------------------------------------------------------- 构建界面
        self._build_ui()
        self._connect_signals()

        # 节流的视图刷新：扫描过程中最多每 N 毫秒重排一次表格
        # （必须先建好，_restore_preferences 里的按钮开关会用到它）
        self._timer = QTimer(self)
        self._timer.setInterval(600)
        self._timer.timeout.connect(self._on_tick)

        self._restore_preferences()
        self._refresh_volumes()
        self._timer.start()

    # ======================================================================
    # 界面搭建
    # ======================================================================
    def _build_ui(self) -> None:
        """创建全部控件与布局。"""
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        root.addWidget(self._build_header())

        self.path_bar = PathBar(self)
        root.addWidget(self.path_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setObjectName("Main")
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)
        splitter.addWidget(self._build_results_panel())
        splitter.addWidget(self._build_sidebar())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([960, 360])
        self.splitter = splitter
        root.addWidget(splitter, 1)

        root.addWidget(self._build_status_bar())

    # ------------------------------------------------------------------ 顶部
    def _build_header(self) -> QWidget:
        """应用标题 + 全局操作按钮。"""
        bar = QFrame(self)
        bar.setObjectName("Inner")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 8, 8, 8)
        layout.setSpacing(8)

        title = QLabel(APP_TITLE, bar)
        title.setObjectName("H1")
        subtitle = QLabel(APP_SUBTITLE, bar)
        subtitle.setObjectName("Muted")
        layout.addWidget(title)
        layout.addSpacing(8)
        layout.addWidget(subtitle)
        layout.addStretch(1)

        self.btn_options = MenuButton("扫描选项", bar)
        self._build_options_menu()
        layout.addWidget(self.btn_options)

        self.btn_theme = QPushButton("☾ 深色", bar)
        self.btn_theme.setObjectName("Ghost")
        self.btn_theme.setToolTip("切换深色/浅色主题（Ctrl+T）")
        self.btn_theme.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.btn_theme)

        self.btn_help = QPushButton("？", bar)
        self.btn_help.setObjectName("Icon")
        self.btn_help.setFixedWidth(32)
        self.btn_help.setToolTip("使用说明")
        layout.addWidget(self.btn_help)
        return bar

    def _build_options_menu(self) -> None:
        """“扫描选项”菜单：影响下一次扫描的参数。"""
        menu = QMenu(self.btn_options)
        self.opt_checks: Dict[str, QCheckBox] = {}
        for label, key, tip in (
            ("跳过隐藏目录", "skip_hidden", "跳过带“隐藏”属性的文件夹与文件"),
            ("跳过系统目录", "skip_system", "跳过带“系统”属性的文件夹"),
            ("跳过系统保留目录", "skip_reserved",
             "跳过 $Recycle.Bin、System Volume Information、Recovery 等"),
            ("跟随软链接/联接点", "follow_symlinks",
             "谨慎开启：可能造成重复统计或死循环"),
        ):
            holder = QMenu(label, menu)
            holder.setObjectName("OptionSub")
            check = QCheckBox("启用", holder)
            check.setToolTip(tip)
            check.setChecked(bool(self.cfg.get(key)))
            check.toggled.connect(lambda checked, k=key: self._set_option(k, checked))
            action = holder.addAction("")
            action.setVisible(False)
            wrap = QWidgetAction(holder)
            wrap.setDefaultWidget(check)
            holder.addAction(wrap)
            menu.addMenu(holder)
            self.opt_checks[key] = check

        menu.addSeparator()
        depth = QMenu("递归层级上限", menu)
        self._depth_actions: Dict[str, QAction] = {}
        for value, text in ((0, "不限制"), (1, "仅 1 层"), (2, "2 层"), (3, "3 层"),
                            (5, "5 层"), (8, "8 层")):
            act = depth.addAction(text)
            act.setCheckable(True)
            act.setData(value)
            act.triggered.connect(lambda checked=False, v=value: self._set_max_depth(v))
            self._depth_actions[str(value)] = act
        menu.addMenu(depth)

        records = QMenu("目录数量上限", menu)
        self._record_actions: List = []
        for value in (50_000, 100_000, 200_000, 400_000, 800_000):
            act = records.addAction(f"{value // 10000} 万个目录")
            act.setCheckable(True)
            act.setData(value)
            act.triggered.connect(lambda checked=False, v=value: self._set_max_records(v))
            self._record_actions.append(act)
        menu.addMenu(records)
        self.btn_options.set_menu(menu)

    # ------------------------------------------------------------------ 结果区
    def _build_results_panel(self) -> QWidget:
        """左侧：面包屑 + 工具条 + 表格 + 条形图。"""
        panel = QWidget(self)
        panel.setMinimumWidth(520)   # 防止左面板被侧栏/富余空间挤扁后列宽错乱
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # 面包屑 + 结果摘要
        nav = QFrame(panel)
        nav.setObjectName("Inner")
        nav_layout = QHBoxLayout(nav)
        nav_layout.setContentsMargins(10, 6, 10, 6)
        nav_layout.setSpacing(8)
        self.btn_up = QPushButton("← 上级", nav)
        self.btn_up.setObjectName("Ghost")
        self.btn_up.setToolTip("返回上一层（Alt+↑）")
        nav_layout.addWidget(self.btn_up)
        self.breadcrumb = Breadcrumb(nav)
        nav_layout.addWidget(self.breadcrumb, 1)
        self.lbl_summary = QLabel("尚未扫描", nav)
        self.lbl_summary.setObjectName("Muted")
        # 固定宽度 + 右对齐：摘要文本随统计数字变化时不再挤压面包屑
        # （否则面包屑槽宽每次刷新都在变，整行跟着左右晃）
        self.lbl_summary.setFixedWidth(300)
        self.lbl_summary.setAlignment(Qt.AlignmentFlag.AlignRight
                                      | Qt.AlignmentFlag.AlignVCenter)
        nav_layout.addWidget(self.lbl_summary)
        layout.addWidget(nav)

        # 工具条（自动换行 = 响应式）
        toolbar = self._build_toolbar()
        layout.addWidget(toolbar)

        # 表格卡片
        self.table_card = Card("文件夹排行", panel)
        self.model = FolderTableModel(self)
        self.view = QTableView(self)
        self.view.setModel(self.model)
        self.view.setItemDelegate(RatioBarDelegate(self.view))
        self.view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.view.setAlternatingRowColors(True)
        self.view.setShowGrid(False)
        self.view.setWordWrap(False)
        self.view.setSortingEnabled(False)   # 排序由模型负责，避免表头自动排序冲突
        self.view.setCornerButtonEnabled(False)
        self.view.verticalHeader().setVisible(False)
        self.view.verticalHeader().setDefaultSectionSize(30)
        header = self.view.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionsMovable(True)
        # 列宽全部交由显式设置（Interactive），与单元格内容宽度彻底解耦：
        # 数据文本长短变化不再推动列宽，末列吸收富余宽度，表格总宽恒等于视口宽。
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setMinimumSectionSize(56)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.setMinimumHeight(220)
        # 垂直滚动条常显：避免行数增减时滚动条出现/消失导致列宽左右移动
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.table_card.add(self.view, 1)
        layout.addWidget(self.table_card, 1)

        # 可视化卡片
        self.chart_card = Card("占用可视化", panel)
        self.bar_chart = HorizontalBarChart(self, max_rows=10)
        self.chart_card.add(self.bar_chart)
        layout.addWidget(self.chart_card)
        return panel

    def _build_toolbar(self) -> QWidget:
        """排序 / 显示 / 导出工具条。"""
        bar = Card(parent=self, object_name="Inner")
        flow = FlowLayout(margin=0, h_spacing=8, v_spacing=8)

        self.btn_level = PillButton("仅当前层", bar)
        self.btn_level.setToolTip("在“只看下一层子目录”和“所有层级一起排行”之间切换")
        flow.addWidget(self.btn_level)

        self.btn_sort = MenuButton("按大小", bar)
        sort_menu = QMenu(self.btn_sort)
        self._sort_actions: Dict[str, object] = {}
        for label, key in SORT_LABELS:
            act = sort_menu.addAction(label)
            act.setCheckable(True)
            act.triggered.connect(lambda checked=False, k=key: self._change_sort(k))
            self._sort_actions[key] = act
        self.btn_sort.set_menu(sort_menu)
        flow.addWidget(self.btn_sort)

        self.btn_order = PillButton("从大到小", bar)
        self.btn_order.setChecked(True)
        self.btn_order.setToolTip("切换升序 / 降序（Ctrl+R）")
        flow.addWidget(self.btn_order)

        self.btn_unit = MenuButton("自动单位", bar)
        unit_menu = QMenu(self.btn_unit)
        for unit in UNIT_CHOICES:
            act = unit_menu.addAction("自动（B/KB/MB/GB）" if unit == "auto" else unit)
            act.setCheckable(True)
            act.triggered.connect(lambda checked=False, u=unit: self._change_unit(u))
        self.btn_unit.set_menu(unit_menu)
        flow.addWidget(self.btn_unit)

        self.btn_topn = MenuButton("前 500 条", bar)
        top_menu = QMenu(self.btn_topn)
        for n in TOP_N_CHOICES:
            act = top_menu.addAction(f"前 {n:,} 条")
            act.setCheckable(True)
            act.triggered.connect(lambda checked=False, v=n: self._change_top_n(v))
        self.btn_topn.set_menu(top_menu)
        flow.addWidget(self.btn_topn)

        self.btn_chart = PillButton("图表", bar)
        self.btn_chart.setChecked(True)
        self.btn_chart.setToolTip("显示 / 隐藏可视化区域")
        flow.addWidget(self.btn_chart)

        self.edit_filter = QLineEdit(bar)
        self.edit_filter.setObjectName("PathInput")
        self.edit_filter.setPlaceholderText("筛选名称…")
        self.edit_filter.setClearButtonEnabled(True)
        self.edit_filter.setFixedWidth(150)
        flow.addWidget(self.edit_filter)

        self.btn_refresh = QPushButton("刷新", bar)
        self.btn_refresh.setObjectName("Ghost")
        self.btn_refresh.setToolTip("用当前设置重新扫描（F5）")
        flow.addWidget(self.btn_refresh)

        self.btn_export = MenuButton("导出", bar, pill=False)
        export_menu = QMenu(self.btn_export)
        export_menu.addAction("导出为 CSV…", lambda: self.export_results("csv"))
        export_menu.addAction("导出为 Excel…", lambda: self.export_results("xlsx"))
        export_menu.addSeparator()
        export_menu.addAction("导出文件类型分布…", lambda: self.export_results("ext"))
        self.btn_export.set_menu(export_menu)
        flow.addWidget(self.btn_export)

        # AI 分析：只在此按钮点击后才发起 API 请求（绝不自动执行）
        self.btn_ai = PillButton("AI 分析", bar)
        self.btn_ai.setToolTip("调用 AI 识别当前目录下所有文件的用途与软件归属（手动触发，Ctrl+I）")
        flow.addWidget(self.btn_ai)

        bar.add_layout(flow)
        return bar

    # ------------------------------------------------------------------- 侧栏
    def _build_sidebar(self) -> QWidget:
        """右侧：扫描进度、概览指标、类型分布、磁盘空间（放在滚动区域里）。"""
        holder = Card("扫描与统计", self)
        # 侧栏宽度锁死在 [300, 460]：无论窗口怎么缩放、内容怎么变化，
        # splitter 都不会推动侧栏——右侧所有卡片位置绝对稳定。
        holder.setMinimumWidth(300)
        holder.setMaximumWidth(460)

        self.scan_panel = ScanPanel(self)

        self.tile_grand = MetricTile("总占用", "--", self)
        self.tile_files = MetricTile("文件总数", "--", self)
        self.tile_dirs = MetricTile("目录总数", "--", self)
        self.tile_avg = MetricTile("平均目录大小", "--", self)
        summary = Card("扫描概览", self)
        # 固定 2×2 网格 + 固定列宽：四个指标块的位置绝对稳定，
        # 不随数值文本、扫描进度或侧栏内容量变化而换行/移动。
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        for index, tile in enumerate((self.tile_grand, self.tile_files,
                                      self.tile_dirs, self.tile_avg)):
            # 固定尺寸：数值文本变化时不再引起布局重排（防抖动）
            tile.setFixedSize(124, 72)
            grid.addWidget(tile, index // 2, index % 2)
        grid.setColumnStretch(2, 1)   # 富余宽度交给空白列，指标块不被拉伸
        summary.add_layout(grid)

        self.donut = DonutChart(self)
        self.ext_chart = HorizontalBarChart(self, max_rows=8)
        ext_card = Card("文件类型分布", self)
        ext_card.add(self.donut)
        ext_card.add(self.ext_chart)
        self.ext_card = ext_card

        self.volume_card = Card("磁盘空间", self)
        self.lbl_hint = QLabel("提示：双击进入子目录，右键打开位置或复制路径。", self)
        self.lbl_hint.setObjectName("Muted")
        self.lbl_hint.setWordWrap(True)

        inner = QVBoxLayout()
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(10)
        inner.addWidget(self.scan_panel)
        inner.addWidget(summary)
        inner.addWidget(ext_card)
        inner.addWidget(self.volume_card)
        inner.addWidget(self.lbl_hint)
        inner.addStretch(1)

        host = QWidget(self)
        host.setLayout(inner)
        host.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea(holder)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(host)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 垂直滚动条常显：内容高度变化时宽度保持稳定，避免右侧卡片左右移动
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        holder.add(scroll, 1)
        return holder

    def _build_status_bar(self) -> QWidget:
        """底部状态行。"""
        bar = QFrame(self)
        bar.setObjectName("Inner")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(12)
        self.status_left = QLabel("就绪", bar)
        self.status_left.setObjectName("Muted")
        self.status_right = QLabel("", bar)
        self.status_right.setObjectName("Muted")
        layout.addWidget(self.status_left, 1)
        layout.addWidget(self.status_right)
        return bar

    # ======================================================================
    # 信号连接
    # ======================================================================
    def _connect_signals(self) -> None:
        """集中接线，便于阅读与维护。"""
        self.path_bar.startRequested.connect(self.start_scan)
        self.path_bar.pathPicked.connect(self._on_path_picked)

        self.scan_panel.startClicked.connect(lambda: self.start_scan(self.path_bar.path()))
        self.scan_panel.pauseClicked.connect(self.pause_scan)
        self.scan_panel.resumeClicked.connect(self.resume_scan)
        self.scan_panel.stopClicked.connect(self.stop_scan)

        self.btn_up.clicked.connect(self.go_up)
        self.breadcrumb.navigate.connect(self.navigate_to)
        self.btn_level.toggled.connect(self._on_level_toggled)
        self.btn_order.toggled.connect(self._on_order_toggled)
        self.btn_chart.toggled.connect(self._on_chart_toggled)
        self.edit_filter.textChanged.connect(self._on_filter)
        self.btn_refresh.clicked.connect(self.rescan)
        self.btn_theme.clicked.connect(self.toggle_theme)
        self.btn_help.clicked.connect(self.show_help)
        self.btn_ai.clicked.connect(self.open_ai_analysis)

        self.view.clicked.connect(lambda _: self._update_selection_info())
        self.view.doubleClicked.connect(self._on_double_click)
        self.view.customContextMenuRequested.connect(self._show_context_menu)
        self.view.horizontalHeader().sectionClicked.connect(self._on_header_click)
        self.view.selectionModel().selectionChanged.connect(lambda _: self._update_selection_info())
        self.bar_chart.itemClicked.connect(self._on_chart_click)
        self.ext_chart.itemClicked.connect(lambda _p: None)

    # ======================================================================
    # 偏好读写
    # ======================================================================
    def _restore_preferences(self) -> None:
        """从配置恢复窗口状态与显示设置。"""
        cfg = self.cfg
        self.setWindowTitle(f"{APP_TITLE} · {APP_SUBTITLE}")
        size = cfg.get("geometry") or [1320, 820]
        try:
            self.resize(int(size[0]), int(size[1]))
        except (TypeError, ValueError, IndexError):
            self.resize(1320, 820)
        pos = cfg.get("window_pos")
        if pos and len(pos) == 2:
            self.move(int(pos[0]), int(pos[1]))
        if cfg.get("maximized"):
            self.showMaximized()

        sizes = cfg.get("splitter")
        if sizes and len(sizes) == 2:
            self.splitter.setSizes([int(sizes[0]), int(sizes[1])])

        last = cfg.get("last_path") or ""
        self.path_bar.set_path(last)
        self.path_bar.set_valid(bool(last) and os.path.isdir(last))

        # 显示相关
        self.model.set_display_options(cfg.get("size_unit"), int(cfg.get("decimals") or 2))
        self._sync_sort_ui()
        self._sync_unit_ui()
        self._sync_top_n_ui()
        self.btn_level.setChecked(bool(cfg.get("show_all_levels")))
        self.btn_chart.setChecked(bool(cfg.get("show_chart", True)))
        self._sync_options_ui()

        widths = cfg.get("column_widths") or {}
        for index, (title, default, _align) in enumerate(COLUMNS):
            value = widths.get(title)
            self.view.setColumnWidth(index, int(value) if value else default)

    def _save_preferences(self) -> None:
        """关闭窗口 / 关键操作后写回配置。"""
        try:
            widths = {
                title: self.view.columnWidth(index)
                for index, (title, _default, _align) in enumerate(COLUMNS)
            }
            self.cfg.update({
                "theme": self.cfg.theme,
                "font_scale": self.cfg.get("font_scale"),
                "last_path": self.path_bar.path(),
                "recent_paths": self.cfg.recent_paths,
                "max_depth": self.cfg.get("max_depth"),
                "max_records": self.cfg.get("max_records"),
                "follow_symlinks": self.cfg.get("follow_symlinks"),
                "skip_hidden": self.cfg.get("skip_hidden"),
                "skip_system": self.cfg.get("skip_system"),
                "skip_reserved": self.cfg.get("skip_reserved"),
                "sort_key": self.model.sort_key,
                "sort_desc": self.model.sort_desc,
                "size_unit": self.cfg.get("size_unit"),
                "decimals": self.cfg.get("decimals"),
                "top_n": self.cfg.get("top_n"),
                "show_all_levels": self.btn_level.isChecked(),
                "show_chart": self.btn_chart.isChecked(),
                "splitter": self.splitter.sizes(),
                "column_widths": widths,
            }, save=False)
            if not self.isMaximized():
                geo = self.size()
                self.cfg.set("geometry", [geo.width(), geo.height()])
                pos = self.pos()
                self.cfg.set("window_pos", [pos.x(), pos.y()])
            self.cfg.set("maximized", self.isMaximized())
            self.cfg.save()
        except OSError as exc:  # 配置写失败不应影响退出
            log.warning("保存偏好失败：%s", exc)

    # ======================================================================
    # 扫描控制
    # ======================================================================
    def start_scan(self, path: str) -> None:
        """开始一次扫描（若正在扫描则先请求停止）。"""
        path = (path or "").strip().strip('"').strip()
        if not path:
            self._warn("请先输入或选择一个文件夹路径。")
            return
        if not os.path.isdir(path):
            self.path_bar.set_valid(False)
            self._warn(f"路径不存在或不是文件夹：\n{path}")
            return
        self.path_bar.set_path(path)
        self.path_bar.set_valid(True)

        if self._thread is not None and self._thread.isRunning():
            if not self._confirm_stop_first():
                return
        else:
            # 上一次的线程已经结束：先回收对象，避免反复扫描残留 QThread
            self._retire_thread()

        options = ScanOptions(
            root=path,
            follow_symlinks=bool(self.cfg.get("follow_symlinks")),
            skip_hidden=bool(self.cfg.get("skip_hidden")),
            skip_system=bool(self.cfg.get("skip_system")),
            skip_reserved=bool(self.cfg.get("skip_reserved")),
            max_depth=int(self.cfg.get("max_depth") or 0),
            max_records=int(self.cfg.get("max_records") or 400000),
        )
        thread = ScanThread(options, self)
        thread.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        thread.partial_ready.connect(self._on_partial, Qt.ConnectionType.QueuedConnection)
        thread.completed.connect(self._on_completed, Qt.ConnectionType.QueuedConnection)
        thread.failed.connect(self._on_failed, Qt.ConnectionType.QueuedConnection)
        self._thread = thread

        self._result = None
        self._current_id = 0
        self._scan_started = time.time()
        self._last_percent_shown = 0.0
        self._dirty = False
        self.scan_panel.begin(path)
        self.breadcrumb.set_chain([(0, os.path.basename(path.rstrip("\\/")) or path)])
        self.model.set_data([], 1, ScanResult(root_path=path))
        self.bar_chart.clear()
        self.donut.set_items([])
        self.ext_chart.clear()
        self.status_left.setText(f"正在扫描 {path} …")
        self.cfg.push_recent(path)
        self.cfg.set("last_path", path)
        thread.start()
        log.info("开始扫描：%s", path)

    def _retire_thread(self) -> None:
        """回收上一轮已经结束的扫描线程对象。

        说明：``ScanThread`` 以主窗口为 parent（所有权在 C++ 侧），因此必须
        先清空 Python 引用再 ``deleteLater``，否则界面上仍可能持有指向已销毁
        C++ 对象的引用，继续调用 ``isRunning()`` 会导致崩溃。
        """
        thread = self._thread
        if thread is None:
            return
        try:
            if thread.isRunning():
                return                       # 仍在运行，交给 closeEvent 处理
        except RuntimeError:                 # C++ 对象已被销毁
            self._thread = None
            return
        self._thread = None
        thread.deleteLater()

    def pause_scan(self) -> None:
        """暂停扫描（数据保留，可继续）。"""
        if self._thread and self._thread.isRunning() and not self._thread.is_paused():
            self._thread.pause()
            self.scan_panel.set_running(True, True)
            self.status_left.setText("已暂停 —— 点击“继续”从中断处恢复")

    def resume_scan(self) -> None:
        """继续被暂停的扫描。"""
        if self._thread and self._thread.isRunning() and self._thread.is_paused():
            self._thread.resume()
            self.scan_panel.set_running(True, False)
            self.status_left.setText("已恢复扫描")

    def toggle_pause(self) -> None:
        """快捷键入口：在暂停 / 继续之间切换。"""
        if self._thread and self._thread.isRunning():
            if self._thread.is_paused():
                self.resume_scan()
            else:
                self.pause_scan()

    def stop_scan(self) -> None:
        """停止扫描并保留已完成的结果。"""
        if self._thread and self._thread.isRunning():
            self._thread.stop()
            self.status_left.setText("正在停止…")

    def rescan(self) -> None:
        """用当前路径与设置重新扫描。"""
        self.start_scan(self.path_bar.path())

    def browse(self) -> None:
        """打开系统文件夹选择对话框。"""
        chosen = pick_folder_dialog(self, self.path_bar.path())
        if chosen:
            self.path_bar.set_path(chosen)
            self.start_scan(chosen)

    # -------------------------------------------------------------- 线程回调
    def _on_progress(self, progress) -> None:
        """扫描线程的进度回调（已节流）。"""
        stats = progress.stats
        self.scan_panel.update_progress(
            percent=progress.percent,
            bytes_done=stats.bytes_done,
            files=stats.files_done,
            dirs=stats.dirs_done + stats.dirs_pending,
            speed=progress.bytes_per_sec,
            eta=progress.eta_seconds,
            current=stats.current_path,
            paused=progress.paused,
        )
        self._last_percent_shown = progress.percent
        extra = []
        if stats.errors:
            extra.append(f"{stats.errors} 处无法访问")
        if stats.skipped:
            extra.append(f"跳过 {format_count(stats.skipped)} 项")
        if extra:
            self.status_right.setText(" · ".join(extra))

    def _on_partial(self, dirs_done: int) -> None:
        """每完成若干目录：标记需要刷新（真正的重排交给定时器）。"""
        self._dirty = True
        self.status_left.setText(
            f"扫描中…  已处理 {format_count(dirs_done)} 个目录")

    def _on_completed(self, result: ScanResult) -> None:
        """扫描正常结束或被停止。"""
        self._result = result
        self._dirty = False
        self._current_id = 0
        elapsed = (result.finished_at - result.started_at) if result.started_at else 0.0
        if result.cancelled:
            message = f"已停止 —— 保留 {format_count(result.total_dirs)} 个目录的已完成数据"
        elif not result.complete:
            message = ("扫描完成（部分目录受层级/数量上限影响未完全统计）")
        else:
            message = "扫描完成"
        self.scan_panel.finish(message, elapsed)
        self.status_left.setText(f"{message} · {result.root_path}")
        if result.errors or result.skipped:
            parts = []
            if result.errors:
                parts.append(f"{len(result.errors)} 处无法访问")
            if result.skipped:
                parts.append(f"跳过 {format_count(result.skipped)} 项（软链接/被过滤）")
            self.status_right.setText(" · ".join(parts) + "，详见日志")
        self._update_summary(result, elapsed)
        self._refresh_view(force=True)
        self.breadcrumb.set_chain(self._chain_for(0))
        self._save_preferences()
        log.info("扫描完成：%s 个目录", result.total_dirs)

    def _on_failed(self, message: str) -> None:
        """扫描线程报错。"""
        self.scan_panel.fail(message)
        self.status_left.setText(message)
        self._warn(message)

    def _on_tick(self) -> None:
        """定时刷新：把“边扫边看”的开销控制在一个可接受频率。"""
        result = self._active_result()
        if result is None:
            return
        scanning = self._thread is not None and self._thread.isRunning()
        # 只有拿到新数据（partial 信号置位 _dirty）才重排视图；
        # 之前“没数据也低频刷一次”会让表格/图表每 600ms 无谓重绘，放大闪烁感。
        if scanning and self._dirty:
            self._dirty = False
            self._refresh_view()

    # ======================================================================
    # 视图刷新
    # ======================================================================
    def _active_result(self) -> Optional[ScanResult]:
        """当前应当展示的结果：扫描中取线程的实时结果，否则取最终结果。"""
        if self._thread is not None and self._thread.isRunning():
            live = self._thread.scanner.result
            if live is not None and live.records:
                return live
            return None
        return self._result

    def _collect_rows(self, result: ScanResult) -> Tuple[List[DirRecord], int, str]:
        """收集当前视图要显示的记录。

        “仅当前层”模式下，若容器目录有直属文件，会在末尾追加一行
        “（散文件 N 个 · XX）”虚拟聚合行（id=LOOSE_ID），用于汇总展示
        那些不构成文件夹的零散文件，双击可查看明细。

        :return: (记录列表, 占比分母, 容器名称)
        """
        container = result.get(self._current_id) or result.root
        if container is None:
            return [], 1, ""
        base = max(1, container.total_bytes)
        top_n = max(1, int(self.cfg.get("top_n") or 500))
        key = self.model.sort_key
        desc = self.model.sort_desc

        if self.btn_level.isChecked():       # 全部层级
            pool = [r for r in (result.get(i) for i in result.descendants(container.id))
                    if r is not None]
        else:                                # 仅当前层
            pool = [r for r in (result.get(i) for i in result.children(container.id))
                    if r is not None]

        if len(pool) > top_n:
            attr = {"files": "total_files", "subdirs": "total_subdirs",
                    "modified": "mtime"}.get(key, "total_bytes")
            picker = heapq.nlargest if desc or key == "name" else heapq.nsmallest
            pool = list(picker(top_n, pool, key=lambda r, a=attr: getattr(r, a)))

        # “散文件”聚合行：只在“仅当前层”模式且本目录有直属文件时追加在末尾。
        # 它是界面虚拟行，不属于扫描结果，导出/下钻/图表等交互均已防护。
        if not self.btn_level.isChecked() and container.direct_files > 0:
            unit = self.cfg.get("size_unit")
            decimals = int(self.cfg.get("decimals") or 2)
            pool.append(DirRecord(
                id=LOOSE_ID,
                parent_id=container.id,
                name=f"（散文件 {format_count(container.direct_files)} 个 · "
                     f"{format_size(container.direct_bytes, unit, decimals)}）",
                depth=container.depth + 1,
                direct_bytes=container.direct_bytes,
                direct_files=container.direct_files,
                total_bytes=container.direct_bytes,
                total_files=container.direct_files,
            ))
        return pool, base, container.name or result.root_path

    def _refresh_view(self, force: bool = False) -> None:
        """把当前结果写入模型，并同步图表与摘要。"""
        result = self._active_result()
        if result is None:
            if force:
                self.model.set_data([], 1, ScanResult(root_path=""))
                self.bar_chart.clear()
            return
        try:
            rows, base, caption = self._collect_rows(result)
        except (RuntimeError, OSError) as exc:   # 极端情况下的兜底
            log.warning("刷新视图失败：%s", exc)
            return
        self.model.set_data(rows, base, result)
        self.model.set_filter(self.edit_filter.text())
        # 扫描中实时刷新只更新表格；图表（含饼图）等最终结果再统一绘制，
        # 避免边扫边重绘造成饼图持续闪烁。扫描结束后 _result 已赋值，
        # 此条件自然变为 False，图表正常刷新。
        scanning_live = (self._thread is not None
                         and self._thread.isRunning()
                         and self._result is None)
        if not scanning_live:
            self._update_charts(rows, base)
        self._update_row_count_label(result, caption)

    def _update_charts(self, rows: Sequence[DirRecord], base: int) -> None:
        """更新横向条形图与环形图。"""
        palette = current_palette()
        unit = self.cfg.get("size_unit")
        decimals = int(self.cfg.get("decimals") or 2)
        # “散文件”虚拟行不进入图表（图表只排行真实目录，点击行为才一致）
        chart_rows = [r for r in rows if r.id != LOOSE_ID][:10]
        items = []
        for index, record in enumerate(chart_rows):
            ratio = (record.total_bytes / base) if base else 0.0
            items.append(ChartItem(
                label=record.name or "（根目录）",
                value=record.total_bytes,
                ratio=ratio,
                color=palette.chart[index % len(palette.chart)],
                payload=record.id,
                detail=f"{format_size(record.total_bytes, unit, decimals)}  ·  "
                       f"{ratio * 100:.1f}%  ·  {format_count(record.total_files)} 个文件",
            ))
        self.bar_chart.set_items(items)

        if not self._result or self._thread and self._thread.isRunning():
            source = self._active_result()
        else:
            source = self._result
        ext_items = self._extension_items(source)
        self.donut.set_items(ext_items[:8])
        self.ext_chart.set_items(ext_items[:8])

    def _extension_items(self, result: Optional[ScanResult]) -> List[ChartItem]:
        """文件类型分布（取占用最大的 8 种扩展名）。"""
        if result is None or not result.ext_bytes:
            return []
        total = sum(result.ext_bytes.values()) or 1
        palette = current_palette()
        top = heapq.nlargest(8, result.ext_bytes.items(), key=lambda kv: kv[1])
        return [
            ChartItem(
                label=ext,
                value=size,
                ratio=size / total,
                color=palette.chart[index % len(palette.chart)],
                detail=f"{format_size(size)}  ·  "
                       f"{format_count(result.ext_files.get(ext, 0))} 个文件",
            )
            for index, (ext, size) in enumerate(top)
        ]

    def _update_row_count_label(self, result: ScanResult, caption: str) -> None:
        """表格标题与摘要文本。"""
        shown = self.model.row_count()
        unit = self.cfg.get("size_unit")
        decimals = int(self.cfg.get("decimals") or 2)
        mode = "全部层级" if self.btn_level.isChecked() else "当前层"
        title = self.table_card.title()
        name = caption or (title.split("·", 1)[1].split("（")[0].strip()
                           if "·" in title else "（根目录）")
        self.table_card.set_title(f"文件夹排行 · {name or '（根目录）'}（{mode}）")
        summary_text = (f"显示 {format_count(shown)} 条 · "
                        f"{format_size(result.total_bytes, unit, decimals)} / "
                        f"{format_count(result.total_files)} 个文件")
        self.lbl_summary.setText(summary_text)
        # 固定宽度下超长文本会被裁剪，用 tooltip 保证信息不丢失
        self.lbl_summary.setToolTip(summary_text)

    def _update_summary(self, result: ScanResult, elapsed: float) -> None:
        """刷新右侧“扫描概览”。"""
        unit = self.cfg.get("size_unit")
        decimals = int(self.cfg.get("decimals") or 2)
        dirs = max(1, result.total_dirs)
        self.tile_grand.set_value(format_size(result.total_bytes, unit, decimals),
                                  f"{result.total_bytes:,} 字节")
        self.tile_files.set_value(format_count(result.total_files))
        self.tile_dirs.set_value(format_count(result.total_dirs))
        self.tile_avg.set_value(format_size(result.total_bytes // dirs, unit, decimals))
        self.status_right.setText(
            f"用时 {format_duration(elapsed)} · {format_speed(result.total_bytes / max(0.2, elapsed))}")
        self.ext_chart.set_items(self._extension_items(result))
        self.donut.set_items(self._extension_items(result)[:8])

    def _refresh_volumes(self) -> None:
        """重建“磁盘空间”卡片。"""
        self.volume_card.clear_body()
        palette = current_palette()
        for volume in list_volumes():
            row = QWidget(self.volume_card)
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(8)
            name = QLabel(volume.label, row)
            name.setMinimumWidth(44)
            bar = MiniBar(row)
            bar.set_value(volume.percent / 100.0)
            info = QLabel(f"{format_size(volume.free)} 可用 / {format_size(volume.total)}",
                          row)
            info.setObjectName("Muted")
            layout.addWidget(name)
            layout.addWidget(bar, 1)
            layout.addWidget(info)
            row.setToolTip(f"{volume.path} 已用 {volume.percent:.1f}%")
            self.volume_card.add(row)
            if volume.percent > 90:
                name.setStyleSheet(f"color: {palette.danger};")

    # ======================================================================
    # 导航
    # ======================================================================
    def navigate_to(self, dir_id: int) -> None:
        """跳转到某个目录（面包屑 / 图表点击）。"""
        result = self._active_result()
        if result is None or result.get(dir_id) is None:
            return
        self._current_id = dir_id
        self.breadcrumb.set_chain(self._chain_for(dir_id))
        self._refresh_view(force=True)

    def go_up(self) -> None:
        """返回上一层。"""
        result = self._active_result()
        if result is None:
            return
        record = result.get(self._current_id)
        if record is None or record.parent_id < 0:
            return
        self.navigate_to(record.parent_id)

    def enter_selected(self) -> None:
        """进入当前选中的目录。"""
        dir_id = self._selected_dir_id()
        if dir_id >= 0:
            self.navigate_to(dir_id)

    def _chain_for(self, dir_id: int) -> List[Tuple[int, str]]:
        """构造从根到 dir_id 的面包屑链条。"""
        result = self._active_result()
        chain: List[Tuple[int, str]] = []
        while dir_id >= 0 and result is not None:
            record = result.get(dir_id)
            if record is None:
                break
            chain.append((dir_id, record.name or result.root_path))
            dir_id = record.parent_id
        chain.reverse()
        return chain

    def _on_double_click(self, index) -> None:
        """双击进入子目录；双击“散文件”聚合行则查看直属文件清单。"""
        if self._thread and self._thread.isRunning():
            return
        dir_id = index.data(ROLE_DIR_ID)
        if not isinstance(dir_id, int):
            return
        if dir_id == LOOSE_ID:
            self.show_loose_files()
        elif dir_id >= 0:
            self.navigate_to(dir_id)

    def _on_chart_click(self, payload) -> None:
        """点击条形图某一条 → 进入对应目录（虚拟行不会进入图表）。"""
        if isinstance(payload, int) and payload >= 0:
            self.navigate_to(payload)

    def _on_header_click(self, section: int) -> None:
        """点击表头切换排序字段。"""
        key = COLUMN_SORT_KEY.get(section)
        if not key:
            return
        if key == self.model.sort_key:
            self.model.set_sort(key, not self.model.sort_desc)
        else:
            self.model.set_sort(key, key != "name")
        self._sync_sort_ui()
        self._refresh_view(force=True)
        self._save_preferences()

    # ======================================================================
    # 工具条交互
    # ======================================================================
    def _change_sort(self, key: str) -> None:
        """排序字段变更。"""
        self.model.set_sort(key, self.btn_order.isChecked())
        self._sync_sort_ui()
        self._refresh_view(force=True)
        self.cfg.set("sort_key", key)
        self._save_preferences()

    def _on_order_toggled(self, checked: bool) -> None:
        """升 / 降序切换。"""
        self.model.set_sort(self.model.sort_key, checked)
        self._refresh_view(force=True)
        self.cfg.set("sort_desc", checked)

    def _on_level_toggled(self, checked: bool) -> None:
        """“仅当前层 / 全部层级”切换。"""
        self.btn_level.setText("全部层级" if checked else "仅当前层")
        self.cfg.set("show_all_levels", checked)
        # 全部层级时数据量大，放宽刷新间隔
        self._timer.setInterval(1500 if checked else 600)
        self._refresh_view(force=True)

    def _on_chart_toggled(self, checked: bool) -> None:
        """显示 / 隐藏图表卡片。"""
        self.chart_card.setVisible(checked)
        self.ext_card.setVisible(checked)
        self.cfg.set("show_chart", checked)

    def _change_unit(self, unit: str) -> None:
        """大小单位切换。"""
        self.cfg.set("size_unit", unit)
        self.model.set_display_options(unit, None)
        self._sync_unit_ui()
        self._refresh_view(force=True)
        self._save_preferences()

    def _change_top_n(self, value: int) -> None:
        """列表显示条数切换。"""
        self.cfg.set("top_n", value)
        self._sync_top_n_ui()
        self._refresh_view(force=True)
        self._save_preferences()

    def _on_filter(self, text: str) -> None:
        """名称筛选。"""
        self.model.set_filter(text)
        self._update_row_count_label(self._active_result() or ScanResult(), "")
        self._update_charts(self.model.all_rows(), max(1, self._base_bytes()))

    def _base_bytes(self) -> int:
        """当前视图的占比分母。"""
        result = self._active_result()
        if result is None:
            return 1
        record = result.get(self._current_id) or result.root
        return max(1, record.total_bytes) if record else 1

    def _set_option(self, key: str, value: bool) -> None:
        """扫描选项开关（影响下一次扫描）。"""
        self.cfg.set(key, value, save=True)

    def _set_max_depth(self, value: int) -> None:
        """层级上限。"""
        self.cfg.set("max_depth", value, save=True)
        self._sync_options_ui()

    def _set_max_records(self, value: int) -> None:
        """目录数量上限。"""
        self.cfg.set("max_records", value, save=True)
        self._sync_options_ui()

    # ------------------------------------------------------------- UI 同步
    def _sync_sort_ui(self) -> None:
        """让菜单勾选状态与模型一致。"""
        key = self.model.sort_key
        for name, action in self._sort_actions.items():
            action.setChecked(name == key)
        label = dict(SORT_LABELS).get(key, "按大小")
        self.btn_sort.setText(label)
        desc = self.model.sort_desc
        self.btn_order.blockSignals(True)
        self.btn_order.setChecked(desc)
        self.btn_order.blockSignals(False)
        self.btn_order.setText("从大到小" if desc else "从小到大")

    def _sync_unit_ui(self) -> None:
        """单位菜单勾选态。"""
        unit = self.cfg.get("size_unit")
        menu = self.btn_unit.menu
        for index, choice in enumerate(UNIT_CHOICES):
            action = menu.actions()[index]
            action.blockSignals(True)
            action.setChecked(choice == unit)
            action.blockSignals(False)
        self.btn_unit.setText("自动单位" if unit == "auto" else f"{unit} 显示")

    def _sync_top_n_ui(self) -> None:
        """Top N 菜单勾选态。"""
        value = int(self.cfg.get("top_n") or 500)
        menu = self.btn_topn.menu
        for index, choice in enumerate(TOP_N_CHOICES):
            if index < len(menu.actions()):
                action = menu.actions()[index]
                action.blockSignals(True)
                action.setChecked(choice == value)
                action.blockSignals(False)
        self.btn_topn.setText(f"前 {value:,} 条")

    def _sync_options_ui(self) -> None:
        """扫描选项菜单勾选态。"""
        for key, check in self.opt_checks.items():
            check.blockSignals(True)
            check.setChecked(bool(self.cfg.get(key)))
            check.blockSignals(False)
        depth = int(self.cfg.get("max_depth") or 0)
        for value, action in self._depth_actions.items():
            action.blockSignals(True)
            action.setChecked(int(value) == depth)
            action.blockSignals(False)
        limit = int(self.cfg.get("max_records") or 400000)
        for action in self._record_actions:
            action.blockSignals(True)
            action.setChecked(int(action.data()) == limit)
            action.blockSignals(False)

    # ======================================================================
    # 右键菜单
    # ======================================================================
    def _show_context_menu(self, position) -> None:
        """表格右键菜单。"""
        index = self.view.indexAt(position)
        if index.isValid() and not self.view.selectionModel().isSelected(index):
            self.view.selectRow(index.row())
        menu = QMenu(self.view)
        path = self._selected_path()
        has_row = bool(path)

        act_enter = menu.addAction("进入该目录", self.enter_selected)
        act_open = menu.addAction("打开文件夹", lambda: open_in_explorer(path))
        act_locate = menu.addAction("打开所在位置",
                                    lambda: open_in_explorer(path, select=True))
        menu.addSeparator()
        act_copy_path = menu.addAction("复制完整路径",
                                       lambda: copy_to_clipboard(path))
        act_copy_name = menu.addAction("复制名称", lambda: copy_to_clipboard(
            os.path.basename(path.rstrip("\\/"))))
        act_copy_size = menu.addAction("复制大小文本", lambda: copy_to_clipboard(
            self._selected_size_text()))
        menu.addSeparator()
        act_root = menu.addAction("以此为根目录重新扫描",
                                  lambda: self.start_scan(path))
        menu.addSeparator()
        act_csv = menu.addAction("导出为 CSV…", lambda: self.export_results("csv"))
        act_xlsx = menu.addAction("导出为 Excel…", lambda: self.export_results("xlsx"))
        menu.addSeparator()
        act_detail = menu.addAction("查看详情", self.show_detail)

        for action in (act_enter, act_open, act_locate, act_copy_path, act_copy_name,
                       act_copy_size, act_root, act_detail):
            action.setEnabled(has_row)
        if self._selected_dir_id() < 0:
            act_enter.setEnabled(False)
        if self._selected_is_loose():
            # “散文件”虚拟行：目录类操作已被禁用，“查看详情”改为文件清单
            act_detail.setText("查看散文件清单")
            act_detail.setEnabled(True)
        else:
            act_detail.setText("查看详情")
        menu.exec(self.view.viewport().mapToGlobal(position))

    def _selected_index_row(self) -> int:
        """当前选中行，无选中返回 -1。"""
        selected = self.view.selectionModel().selectedRows()
        return selected[0].row() if selected else -1

    def _selected_dir_id(self) -> int:
        """选中行的目录 id。"""
        row = self._selected_index_row()
        return self.model.dir_id(row) if row >= 0 else -1

    def _selected_path(self) -> str:
        """选中行的完整路径。"""
        row = self._selected_index_row()
        if row < 0:
            return ""
        result = self._active_result()
        record = self.model.record(row)
        if result is None or record is None or record.id == LOOSE_ID:
            return ""
        return result.full_path(record.id)

    def _selected_is_loose(self) -> bool:
        """当前选中行是否为“散文件”虚拟聚合行（不对应真实目录）。"""
        row = self._selected_index_row()
        if row < 0:
            return False
        record = self.model.record(row)
        return record is not None and record.id == LOOSE_ID

    def show_loose_files(self) -> None:
        """弹出当前浏览目录的散文件（直属文件）清单。"""
        result = self._active_result()
        if result is None:
            self._warn("请先扫描一个文件夹。")
            return
        container = result.get(self._current_id) or result.root
        if container is None:
            return
        directory = result.full_path(container.id)
        if not directory or not os.path.isdir(directory):
            self._warn("目录已不存在，无法列出其中的文件。")
            return
        LooseFilesDialog(
            directory, container.direct_bytes, container.direct_files,
            self.cfg.get("size_unit"), int(self.cfg.get("decimals") or 2),
            self).exec()

    def _selected_size_text(self) -> str:
        """选中行的大小文本。"""
        row = self._selected_index_row()
        record = self.model.record(row) if row >= 0 else None
        if record is None:
            return ""
        return format_size(record.total_bytes, self.cfg.get("size_unit"),
                           int(self.cfg.get("decimals") or 2))

    def _update_selection_info(self) -> None:
        """选中行变化时更新状态栏。"""
        path = self._selected_path()
        if not path:
            self.status_left.setText("就绪" if not self._result else
                                     f"{self._result.root_path}")
            return
        self.status_left.setText(path)
        self.view.setToolTip("")

    def _on_path_picked(self, path: str) -> None:
        """路径选择后的即时反馈（校验 + 快速估算）。"""
        self.path_bar.set_path(path)
        valid = os.path.isdir(path)
        self.path_bar.set_valid(valid)
        if not valid:
            self.status_left.setText(f"路径无效：{path}")
            return
        if in_trash(path):
            self.status_left.setText("回收站内容受系统保护，建议改扫其他目录")

    # ======================================================================
    # 对话框 / 导出
    # ======================================================================
    def open_ai_analysis(self) -> None:
        """打开“AI 文件分析”对话框（分析目标 = 当前浏览的目录）。

        说明：对话框内部也只在用户点击“开始分析”后才请求 AI 接口，
        这里只负责目录校验与弹窗，不产生任何网络请求。
        """
        result = self._active_result()
        if result is None:
            self._warn("请先扫描一个文件夹，AI 分析需要以扫描结果中的目录为对象。")
            return
        directory = result.full_path(self._current_id)
        if not directory or not os.path.isdir(directory):
            self._warn("当前浏览的目录已不存在，请重新扫描后再使用 AI 分析。")
            return
        dialog = AiAnalysisDialog(self.cfg, directory, self)
        dialog.exec()

    def show_detail(self) -> None:
        """弹出所选目录的详细信息。"""
        row = self._selected_index_row()
        record = self.model.record(row) if row >= 0 else None
        result = self._active_result()
        if record is None or result is None:
            self._warn("请先在列表中选择一行。")
            return
        if record.id == LOOSE_ID:      # “散文件”虚拟行：详情即直属文件清单
            self.show_loose_files()
            return
        unit = self.cfg.get("size_unit")
        decimals = int(self.cfg.get("decimals") or 2)
        top = result.top_descendants(record.id, 5)
        lines = [
            f"名称：{record.name or result.root_path}",
            f"路径：{result.full_path(record.id)}",
            f"总大小：{format_size(record.total_bytes, unit, decimals)}"
            f"（{record.total_bytes:,} 字节）",
            f"直属文件：{format_count(record.direct_files)} 个 / "
            f"{format_size(record.direct_bytes, unit, decimals)}",
            f"递归文件：{format_count(record.total_files)} 个",
            f"递归子目录：{format_count(record.total_subdirs)} 个",
            f"最大单个文件：{format_size(record.largest_file, unit, decimals)}",
            f"隐藏/系统文件：{format_count(record.total_hidden_files)} 个 / "
            f"{format_size(record.total_hidden_bytes, unit, decimals)}"
            f"（资源管理器默认不显示，可能是手工加总与扫描结果不一致的原因）",
            f"层级：{record.depth}",
        ]
        if top:
            lines.append("—— 其中最大的 5 个子目录 ——")
            lines.extend(f"{format_size(r.total_bytes, unit, decimals):>12}  {r.name}"
                         for r in top)
        QMessageBox.information(self, "目录详情", "\n".join(lines))

    def show_help(self) -> None:
        """使用说明。"""
        box = QMessageBox(self)
        box.setWindowTitle("使用说明")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(build_help_text())
        box.exec()

    def export_results(self, kind: str) -> None:
        """导出当前视图（CSV / XLSX / 文件类型分布）。

        :param kind: ``csv`` / ``xlsx`` / ``ext``
        """
        result = self._active_result()
        if result is None:
            self._warn("还没有可导出的结果，请先扫描一个文件夹。")
            return
        stamp = time.strftime("%Y%m%d_%H%M%S")
        try:
            if kind == "ext":
                if not result.ext_bytes:
                    self._warn("没有文件类型数据可导出。")
                    return
                path, _ = QFileDialog.getSaveFileName(
                    self, "导出文件类型分布",
                    str(paths.default_export_dir() / f"文件类型分布_{stamp}.csv"),
                    "CSV 文件 (*.csv);;Excel 工作簿 (*.xlsx)")
                if not path:
                    return
                written = exporter.export_extension_report(
                    path, dict(result.ext_bytes), dict(result.ext_files),
                    result.total_bytes)
            else:
                # “散文件”虚拟行不是真实目录，导出前过滤掉（row_to_dict 会返回空行）
                rows = rows_for_export(
                    result, [r for r in self.model.all_rows() if r.id != LOOSE_ID],
                    self._base_bytes())
                rows = exporter.rows_from_records(rows)
                if not rows:
                    self._warn("当前列表为空，没有可导出的行。")
                    return
                meta = exporter.build_meta(result.root_path, result.total_bytes,
                                           result.total_files, result.total_dirs,
                                           result.finished_at - result.started_at,
                                           "folders", len(rows))
                suffix = "xlsx" if kind == "xlsx" else "csv"
                filters = ("Excel 工作簿 (*.xlsx)" if kind == "xlsx"
                           else "CSV 文件 (*.csv)")
                path, _ = QFileDialog.getSaveFileName(
                    self, "导出排行结果",
                    str(paths.default_export_dir() / f"文件夹排行_{stamp}.{suffix}"),
                    filters)
                if not path:
                    return
                written = (exporter.export_xlsx(path, rows, meta) if kind == "xlsx"
                           else exporter.export_csv(path, rows, meta))
        except RuntimeError as exc:      # 缺 openpyxl 之类“需要用户行动”的错误
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "导出失败", f"写入文件时出错：\n{exc}")
            return
        QMessageBox.information(self, "导出成功",
                                f"已写入：\n{written}\n\n共 {os.path.getsize(written):,} 字节")
        open_in_explorer(written, select=True)

    # ======================================================================
    # 主题 / 快捷键 / 关闭
    # ======================================================================
    def toggle_theme(self) -> None:
        """深色 / 浅色切换（Ctrl+T）。"""
        theme = self.cfg.toggle_theme()
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, theme, float(self.cfg.get("font_scale") or 1.0))
        for widget in (self.bar_chart, self.donut, self.ext_chart, self.view):
            restpol_repolish(widget)
        self._apply_theme_button()
        self._refresh_volumes()
        self._refresh_view(force=True)
        self.cfg.save()

    def _apply_theme_button(self) -> None:
        """同步主题按钮文案。"""
        dark = self.cfg.theme == "dark"
        self.btn_theme.setText("☾ 深色" if dark else "☀ 浅色")

    def _confirm_stop_first(self) -> bool:
        """正在扫描时又点了开始：询问是否停止上一次。"""
        box = QMessageBox(self)
        box.setWindowTitle("正在扫描")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("当前有扫描任务正在进行，要先停止它吗？")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.Yes)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _warn(self, text: str) -> None:
        """轻量提示（不阻塞操作）。"""
        box = QMessageBox(self)
        box.setWindowTitle(APP_TITLE)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(text)
        box.exec()

    # -------------------------------------------------------------- 事件重写
    def keyPressEvent(self, event) -> None:  # noqa: N802
        """全局快捷键（避免依赖 QShortcut 的焦点问题）。"""
        key = event.key()
        mods = event.modifiers()
        ctrl = mods & Qt.KeyboardModifier.ControlModifier
        alt = mods & Qt.KeyboardModifier.AltModifier
        if key == Qt.Key.Key_Escape:
            self.stop_scan()
        elif ctrl and key == Qt.Key.Key_S:
            self.start_scan(self.path_bar.path())
        elif ctrl and key == Qt.Key.Key_O:
            self.browse()
        elif ctrl and key == Qt.Key.Key_E:
            self.export_results("csv")
        elif ctrl and key == Qt.Key.Key_I:
            self.open_ai_analysis()
        elif ctrl and key == Qt.Key.Key_P:
            self.toggle_pause()
        elif ctrl and key == Qt.Key.Key_T:
            self.toggle_theme()
        elif ctrl and key == Qt.Key.Key_R:
            self.btn_order.toggle()
        elif ctrl and key == Qt.Key.Key_D:
            self.btn_level.toggle()
        elif alt and key == Qt.Key.Key_Up:
            self.go_up()
        elif alt and key == Qt.Key.Key_Right:
            self.enter_selected()
        elif key == Qt.Key.Key_F5:
            self.rescan()
        elif key == Qt.Key.Key_F6:
            self.btn_sort.menu().exec(
                self.btn_sort.mapToGlobal(QPoint(0, self.btn_sort.height())))
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def closeEvent(self, event) -> None:  # noqa: N802
        """退出前停止扫描线程并保存偏好（防止线程泄漏）。"""
        self._closing = True
        if self._thread is not None and self._thread.isRunning():
            self._thread.stop()
            if not self._thread.wait_safely(4000):
                log.warning("扫描线程未在 4 秒内退出")
        self._timer.stop()
        self._save_preferences()
        event.accept()

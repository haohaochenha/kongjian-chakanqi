"""AI 文件分析界面：设置对话框 + 分析主对话框。

交互原则（与需求一致）：
* **只手动触发**——用户点击“开始分析”才发起 API 请求，程序绝不会自动调用；
* 分析在后台线程进行，界面不卡顿，可随时“停止”；
* 结果表格的列宽锁定（延续主窗口防抖动的设计），内容变化不会引起布局晃动。
"""

from __future__ import annotations

import os
import time
from typing import Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog,
                               QFormLayout, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QProgressBar,
                               QPushButton, QSpinBox, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..core import exporter, paths
from ..core.ai_client import (AiAnalysisError, AiClient, FileEntry,
                              FileInsight, entry_key)
from ..core.config import ConfigManager
from ..core.formatter import format_count, format_size
from ..core.logger import get_logger
from ..core.volumes import open_in_explorer
from .ai_thread import AiAnalysisThread, CollectFilesThread
from .widgets import Card

log = get_logger("ai_dialog")

# 国内常用的 OpenAI 兼容服务预设（新手可以直接选择，无需查文档）
PROVIDER_PRESETS = (
    ("OpenAI", "https://api.openai.com/v1"),
    ("DeepSeek", "https://api.deepseek.com/v1"),
    ("Kimi / 月之暗面", "https://api.moonshot.cn/v1"),
    ("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4"),
    ("阿里通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    ("自定义…", ""),
)

# 结果表格列：(标题, 默认列宽, 是否可拉伸)
TABLE_COLUMNS = (
    ("文件名", 180, False),
    ("所在目录", 160, False),
    ("大小", 90, False),
    ("软件归属", 160, False),
    ("类型", 70, False),
    ("用途说明", 220, True),
)


def _fetch(cfg: ConfigManager, key: str) -> str:
    """读取字符串配置（统一 strip，避免空白字符引发请求失败）。"""
    return str(cfg.get(key) or "").strip()


# ======================================================================
# AI 设置对话框
# ======================================================================
class AiSettingsDialog(QDialog):
    """编辑 OpenAI 兼容接口的连接参数（保存到本机配置文件）。"""

    def __init__(self, cfg: ConfigManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("AI 设置 · OpenAI 兼容接口")
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        # 服务商预设：选择后自动填入接口地址
        self.combo_provider = QComboBox(self)
        for name, _url in PROVIDER_PRESETS:
            self.combo_provider.addItem(name)
        self.combo_provider.addItem("自定义…")
        self.combo_provider.activated.connect(self._on_provider_activated)
        form.addRow("服务商预设", self.combo_provider)

        self.edit_base_url = QLineEdit(_fetch(cfg, "ai_base_url"), self)
        self.edit_base_url.setPlaceholderText("https://api.openai.com/v1")
        self.edit_base_url.setToolTip("OpenAI 兼容接口地址，需包含版本号（一般以 /v1 结尾）")
        form.addRow("接口地址", self.edit_base_url)

        key_row = QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        self.edit_api_key = QLineEdit(_fetch(cfg, "ai_api_key"), self)
        self.edit_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_api_key.setPlaceholderText("sk-…")
        self.check_show_key = QCheckBox("显示", self)
        self.check_show_key.toggled.connect(self._on_show_key_toggled)
        key_row.addWidget(self.edit_api_key, 1)
        key_row.addWidget(self.check_show_key)
        form.addRow("API Key", key_row)

        self.edit_model = QLineEdit(_fetch(cfg, "ai_model"), self)
        self.edit_model.setPlaceholderText("gpt-4o-mini / deepseek-chat …")
        self.edit_model.setToolTip("模型名称由服务商决定，填错会返回 404")
        form.addRow("模型名称", self.edit_model)

        self.spin_batch = QSpinBox(self)
        self.spin_batch.setRange(1, 100)
        self.spin_batch.setValue(int(cfg.get("ai_batch_size") or 30))
        self.spin_batch.setToolTip("每次请求发送给 AI 的文件个数；越大请求越少，但单次回复更长")
        form.addRow("每批文件数", self.spin_batch)

        self.spin_max_files = QSpinBox(self)
        self.spin_max_files.setRange(1, 2000)
        self.spin_max_files.setValue(int(cfg.get("ai_max_files") or 200))
        self.spin_max_files.setToolTip("目录文件很多时，只分析占用最大的前 N 个文件")
        form.addRow("最多分析文件数", self.spin_max_files)

        layout.addLayout(form)

        hint = QLabel("说明：API Key 以明文保存在本机配置文件中，请勿在公用电脑上保存；\n"
                      "分析时会把“文件名 / 大小 / 修改时间”发送给所配置的 AI 服务。",
                      self)
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        btn_cancel = QPushButton("取消", self)
        btn_cancel.clicked.connect(self.reject)
        btn_ok = QPushButton("保存", self)
        btn_ok.setObjectName("Primary")
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(self.accept)
        buttons.addWidget(btn_cancel)
        buttons.addWidget(btn_ok)
        layout.addLayout(buttons)

    # -------------------------------------------------------------- 槽函数
    def _on_provider_activated(self, index: int) -> None:
        """选择服务商预设后自动填接口地址（“自定义…”不清空用户输入）。"""
        presets = list(PROVIDER_PRESETS) + [("自定义…", "")]
        name, url = presets[index]
        if url:
            self.edit_base_url.setText(url)

    def _on_show_key_toggled(self, checked: bool) -> None:
        """切换 API Key 的显示/隐藏。"""
        self.edit_api_key.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password)

    # -------------------------------------------------------------- 保存
    def accept(self) -> None:  # noqa: N802
        """校验后写回配置并关闭。"""
        base_url = self.edit_base_url.text().strip()
        model = self.edit_model.text().strip()
        if base_url and not base_url.lower().startswith(("http://", "https://")):
            QMessageBox.warning(self, "AI 设置", "接口地址需要以 http:// 或 https:// 开头。")
            return
        self.cfg.update({
            "ai_base_url": base_url.rstrip("/"),   # 归一化：去掉尾部斜杠
            "ai_api_key": self.edit_api_key.text().strip(),
            "ai_model": model,
            "ai_batch_size": self.spin_batch.value(),
            "ai_max_files": self.spin_max_files.value(),
        }, save=True)
        super().accept()


# ======================================================================
# AI 分析主对话框
# ======================================================================
class AiAnalysisDialog(QDialog):
    """当前目录文件的 AI 用途识别与软件归属分析（手动触发）。"""

    def __init__(self, cfg: ConfigManager, directory: str,
                 parent: Optional[QWidget] = None) -> None:
        """
        :param cfg: 配置管理器（读写 AI 连接参数）
        :param directory: 要分析的目录（主窗口当前浏览的目录）
        """
        super().__init__(parent)
        self.cfg = cfg
        self.directory = directory
        self.setWindowTitle("AI 文件分析")
        self.resize(860, 560)
        self.setMinimumSize(640, 420)

        self._entries: List[FileEntry] = []          # 待分析文件清单
        self._insights: List[FileInsight] = []       # 已收到的结果（按批次顺序）
        self._entries_by_key: Dict[str, FileEntry] = {}   # entry_key -> 条目（递归后唯一）
        self._thread: Optional[AiAnalysisThread] = None
        self._collector: Optional[CollectFilesThread] = None
        self._started_at = 0.0

        self._build_ui()
        self._load_entries()

    # -------------------------------------------------------------- 界面
    def _build_ui(self) -> None:
        """创建全部控件。"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        dir_name = os.path.basename(self.directory.rstrip("\\/")) or self.directory
        card = Card(f"分析目录 · {dir_name}", self)

        # 目录路径（超长省略，完整路径放 tooltip）
        self.lbl_dir = QLabel(self.directory, self)
        self.lbl_dir.setObjectName("Muted")
        self.lbl_dir.setToolTip(self.directory)
        card.add(self.lbl_dir)

        # 操作按钮行
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.btn_start = QPushButton("开始分析", self)
        self.btn_start.setObjectName("Primary")
        self.btn_start.setDefault(True)
        self.btn_start.setToolTip("把当前目录的文件清单交给 AI 识别（手动触发）")
        self.btn_stop = QPushButton("停止", self)
        self.btn_stop.setEnabled(False)
        self.btn_settings = QPushButton("AI 设置…", self)
        self.btn_settings.setObjectName("Ghost")
        self.btn_export_csv = QPushButton("导出 CSV…", self)
        self.btn_export_csv.setEnabled(False)
        self.btn_export_xlsx = QPushButton("导出 Excel…", self)
        self.btn_export_xlsx.setEnabled(False)
        for widget in (self.btn_start, self.btn_stop, self.btn_settings,
                       self.btn_export_csv, self.btn_export_xlsx):
            buttons.addWidget(widget)
        buttons.addStretch(1)
        card.add_layout(buttons)

        # 进度条 + 状态
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        card.add(self.progress)

        self.lbl_status = QLabel("就绪 —— 点击“开始分析”才会调用 AI 接口", self)
        self.lbl_status.setObjectName("Muted")
        self.lbl_status.setWordWrap(True)
        card.add(self.lbl_status)

        # 结果表格：列宽锁定 + 末列吸收富余（防内容变化引起晃动）
        self.table = QTableWidget(0, len(TABLE_COLUMNS), self)
        self.table.setHorizontalHeaderLabels([t for t, *_ in TABLE_COLUMNS])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setMinimumSectionSize(60)
        for index, (_title, width, _stretch) in enumerate(TABLE_COLUMNS):
            self.table.setColumnWidth(index, width)
        # 滚动条常显：结果行数变化时不引起横向宽度跳变
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setMinimumHeight(240)
        card.add(self.table, 1)

        layout.addWidget(card, 1)

        # 底部提示
        self.lbl_hint = QLabel("归属与说明由 AI 推断生成，仅供参考；"
                               "信息不足的条目会标注“（推测）”。", self)
        self.lbl_hint.setObjectName("Muted")
        self.lbl_hint.setWordWrap(True)
        layout.addWidget(self.lbl_hint)

        # 信号
        self.btn_start.clicked.connect(self.start_analysis)
        self.btn_stop.clicked.connect(self._stop_analysis)
        self.btn_settings.clicked.connect(self.open_settings)
        self.btn_export_csv.clicked.connect(lambda: self.export_results("csv"))
        self.btn_export_xlsx.clicked.connect(lambda: self.export_results("xlsx"))

    def _load_entries(self) -> None:
        """后台递归收集文件清单（只是读目录，不请求任何接口）。

        大目录（如 D 盘）可能有几十万文件，同步遍历会卡住界面，
        所以收集放到 :class:`CollectFilesThread`，完成后再刷新状态。
        """
        self._shutdown_collector()
        self._entries = []
        self._entries_by_key = {}
        self.btn_start.setEnabled(False)
        self.btn_settings.setEnabled(False)
        self.lbl_status.setText("正在收集文件清单…（大目录可能较慢）")
        max_files = int(self.cfg.get("ai_max_files") or 200)
        self._collector = CollectFilesThread(
            self.directory, max_files=max_files, parent=self)
        self._collector.collected.connect(self._on_collected,
                                          Qt.ConnectionType.QueuedConnection)
        self._collector.failed.connect(self._on_collect_failed,
                                       Qt.ConnectionType.QueuedConnection)
        self._collector.start()

    def _on_collected(self, entries: list) -> None:
        """收集完成：建立唯一键索引并刷新状态。"""
        self._entries = list(entries)
        self._entries_by_key = {
            entry_key(e.rel_dir, e.name): e for e in self._entries}
        max_files = int(self.cfg.get("ai_max_files") or 200)
        self.btn_settings.setEnabled(True)
        if not self._entries:
            self.lbl_status.setText("当前目录下没有可分析的文件。")
            self.btn_start.setEnabled(False)
            return
        if len(self._entries) < max_files:
            self.lbl_status.setText(
                f"共 {format_count(len(self._entries))} 个文件待分析 —— "
                "点击“开始分析”开始（不会自动执行）")
        else:
            self.lbl_status.setText(
                f"目录中文件较多，已取占用最大的前 {format_count(len(self._entries))} 个"
                "（可在 AI 设置中调整上限）")
        self.btn_start.setEnabled(True)

    def _on_collect_failed(self, message: str) -> None:
        """收集失败：提示并禁用开始按钮。"""
        self._entries = []
        self._entries_by_key = {}
        self.btn_settings.setEnabled(True)
        self.btn_start.setEnabled(False)
        self.lbl_status.setText(str(message))

    def _shutdown_collector(self) -> None:
        """回收收集线程（重新收集/关闭对话框时调用）。"""
        collector = self._collector
        if collector is None:
            return
        try:
            if collector.isRunning():
                collector.wait_safely(5000)
        except RuntimeError:            # C++ 对象已销毁
            pass
        self._collector = None

    # -------------------------------------------------------------- 手动触发
    def start_analysis(self) -> None:
        """点击“开始分析”后才真正请求 AI（唯一的触发入口）。"""
        if self._thread is not None and self._thread.isRunning():
            return                      # 正在分析中，忽略重复点击
        if not self._entries:
            self.lbl_status.setText("没有可分析的文件。")
            return
        client = AiClient(_fetch(self.cfg, "ai_base_url"),
                          _fetch(self.cfg, "ai_api_key"),
                          _fetch(self.cfg, "ai_model"))
        try:
            client._validate()          # 配置缺失时立刻提示，不进入线程
        except AiAnalysisError as exc:
            if self._ask_open_settings(str(exc)):
                self.open_settings()
            return

        self.table.setRowCount(0)       # 清空上一次结果
        self._insights.clear()
        batch = int(self.cfg.get("ai_batch_size") or 30)
        thread = AiAnalysisThread(client, self._entries, self.directory,
                                  batch_size=batch, parent=self)
        thread.insights_ready.connect(self._on_insights, Qt.ConnectionType.QueuedConnection)
        thread.batch_done.connect(self._on_batch_done, Qt.ConnectionType.QueuedConnection)
        thread.failed.connect(self._on_failed, Qt.ConnectionType.QueuedConnection)
        thread.finished_all.connect(self._on_finished_all, Qt.ConnectionType.QueuedConnection)
        self._thread = thread
        self._started_at = time.time()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_settings.setEnabled(False)
        self.btn_export_csv.setEnabled(False)
        self.btn_export_xlsx.setEnabled(False)
        self.progress.setRange(0, len(self._entries))
        self.progress.setValue(0)
        self.lbl_status.setText(f"正在分析 0 / {len(self._entries)} 个文件…")
        thread.start()
        log.info("AI 分析已手动触发：%s", self.directory)

    def _stop_analysis(self) -> None:
        """停止当前分析（当前批次完成后退出）。"""
        if self._thread is not None and self._thread.isRunning():
            self._thread.cancel()
            self.btn_stop.setEnabled(False)
            self.lbl_status.setText("正在停止（等待当前批次返回）…")

    def _shutdown_thread(self) -> None:
        """回收后台线程（对话框以任何方式关闭时都会调用）。"""
        thread = self._thread
        if thread is None:
            return
        try:
            if thread.isRunning():
                thread.cancel()
                thread.wait_safely(5000)
        except RuntimeError:            # C++ 对象已销毁
            pass
        self._thread = None

    def done(self, result: int) -> None:  # noqa: N802
        """关闭对话框前先安全回收线程（accept/reject/关闭窗口都会经过这里）。"""
        self._shutdown_thread()
        self._shutdown_collector()
        super().done(result)

    # -------------------------------------------------------------- 线程回调
    def _on_insights(self, insights: list) -> None:
        """收到一批结果：追加进表格。"""
        batch: List[FileInsight] = list(insights)
        self._insights.extend(batch)
        start_row = self.table.rowCount()
        self.table.setRowCount(start_row + len(batch))
        for offset, insight in enumerate(batch):
            entry = self._entries_by_key.get(
                entry_key(insight.rel_dir, insight.name))
            size_text = format_size(entry.size) if entry else ""
            purpose = insight.purpose
            rel_dir_text = insight.rel_dir or "（根目录）"
            values = (insight.name, rel_dir_text, size_text, insight.owner,
                      insight.category, purpose)
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column in (2, 4):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
                if column == 5:
                    item.setToolTip(purpose)   # 超长说明悬浮可见
                self.table.setItem(start_row + offset, column, item)

    def _on_batch_done(self, done: int, total: int) -> None:
        """一批完成：更新进度与状态文本。"""
        self.progress.setValue(done)
        self.lbl_status.setText(f"正在分析 {done} / {total} 个文件…")

    def _on_failed(self, message: str) -> None:
        """分析出错：提示并恢复按钮。"""
        self.lbl_status.setText(f"分析失败：{message}")
        self._set_idle_buttons()
        QMessageBox.warning(self, "AI 分析失败", message)

    def _on_finished_all(self) -> None:
        """全部批次结束（含取消）。"""
        elapsed = time.time() - self._started_at if self._started_at else 0.0
        count = len(self._insights)
        if count:
            self.progress.setRange(0, 1)
            self.progress.setValue(1)
            self.lbl_status.setText(
                f"分析完成 · 共 {format_count(count)} 条结果 · 用时 {elapsed:.1f} 秒")
            self.btn_export_csv.setEnabled(True)
            self.btn_export_xlsx.setEnabled(True)
        else:
            self.lbl_status.setText("分析已停止，没有结果。")
        self._set_idle_buttons()

    def _set_idle_buttons(self) -> None:
        """恢复“空闲”状态的按钮可用性。"""
        self.btn_start.setEnabled(bool(self._entries))
        self.btn_stop.setEnabled(False)
        self.btn_settings.setEnabled(True)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)

    # -------------------------------------------------------------- 设置/导出
    def _ask_open_settings(self, message: str) -> bool:
        """配置不完整时询问是否立即打开设置。"""
        box = QMessageBox(self)
        box.setWindowTitle("AI 设置不完整")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(message)
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.Yes)
        return box.exec() == QMessageBox.StandardButton.Yes

    def open_settings(self) -> None:
        """打开 AI 设置对话框；保存后刷新文件清单（上限可能变化）。"""
        dialog = AiSettingsDialog(self.cfg, self)
        if dialog.exec():
            self.table.setRowCount(0)
            self._insights.clear()
            self._load_entries()   # 异步重新收集，完成后自动恢复按钮状态

    def export_results(self, kind: str) -> None:
        """导出分析结果（CSV / XLSX）。"""
        if not self._insights:
            QMessageBox.information(self, "AI 文件分析", "还没有可导出的结果。")
            return
        stamp = time.strftime("%Y%m%d_%H%M%S")
        suffix = "xlsx" if kind == "xlsx" else "csv"
        default = str(paths.default_export_dir() / f"AI文件分析_{stamp}.{suffix}")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 AI 分析结果", default,
            "Excel 工作簿 (*.xlsx)" if kind == "xlsx" else "CSV 文件 (*.csv)")
        if not path:
            return
        meta = {
            "标题": "AI 文件分析报告",
            "分析目录": self.directory,
            "模型": _fetch(self.cfg, "ai_model"),
            "导出行数": f"{len(self._insights):,}",
            "导出时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        rows = []
        for insight in self._insights:
            entry = self._entries_by_key.get(
                entry_key(insight.rel_dir, insight.name))
            rows.append({
                "name": insight.name,
                "rel_dir": insight.rel_dir or "（根目录）",
                "owner": insight.owner,
                "category": insight.category,
                "size_text": format_size(entry.size) if entry else "",
                "purpose": insight.purpose,
            })
        try:
            written = exporter.export_ai_report(path, rows, meta)
        except RuntimeError as exc:      # 缺 openpyxl 等需用户行动的错误
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "导出失败", f"写入文件时出错：\n{exc}")
            return
        QMessageBox.information(self, "导出成功", f"已写入：\n{written}")
        open_in_explorer(written, select=True)

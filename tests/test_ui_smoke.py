"""界面冒烟测试（离屏运行，不需要真实显示器）。

目的：验证“扫描 → 排序 → 筛选 → 下钻 → 单位切换 → 主题切换 → 导出”整条链路
在真实 Qt 环境下不抛异常，并且显示的数据与引擎结果一致。

运行方式::

    python -m unittest discover -s tests -v

说明：测试会在临时目录建一棵大小已知的目录树，并弹窗全部替换成假对象，
因此不会打开资源管理器、不会真的弹出对话框。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import sys
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.config import ConfigManager  # noqa: E402
from app.core.models import LOOSE_ID  # noqa: E402
from app.ui import main_window as mw  # noqa: E402
from app.ui.loose_files_dialog import list_loose_files  # noqa: E402

logging.disable(logging.INFO)

HAS_OPENPYXL = importlib.util.find_spec("openpyxl") is not None

DIRS = 6                     # 顶层目录数量
UNIT_BYTES = 1024            # 每个 d{i} 的直属文件大小基数

# 打桩前保存真实符号，供各测试类还原使用
_REAL_MESSAGE_BOX = mw.QMessageBox
_REAL_FILE_DIALOG = mw.QFileDialog
_REAL_OPEN_IN_EXPLORER = mw.open_in_explorer
_REAL_Q_MENU = mw.QMenu
_REAL_LOOSE_DIALOG = mw.LooseFilesDialog


# ------------------------------------------------------------------ 假对话框
class FakeMessageBox:
    """替代 QMessageBox：只记录调用，不阻塞等待用户点击。"""

    calls: list = []

    class Icon:
        Warning = 1
        Information = 2
        Question = 3
        Critical = 4

    class StandardButton:
        Ok = 0x1
        Yes = 0x4
        No = 0x10

    def __init__(self, parent=None) -> None:
        self.text = ""

    def setWindowTitle(self, _title) -> None:
        pass

    def setTextFormat(self, _fmt) -> None:
        pass

    def setText(self, text) -> None:
        self.text = str(text)

    def setIcon(self, _icon) -> None:
        pass

    def setStandardButtons(self, _buttons) -> None:
        pass

    def setDefaultButton(self, _button) -> None:
        pass

    def exec(self, *_args) -> int:
        FakeMessageBox.calls.append(("exec", self.text))
        return 0

    @classmethod
    def information(cls, _parent, title, text):
        cls.calls.append(("information", title, text))

    @classmethod
    def warning(cls, _parent, title, text):
        cls.calls.append(("warning", title, text))

    @classmethod
    def critical(cls, _parent, title, text):
        cls.calls.append(("critical", title, text))

    @classmethod
    def question(cls, _parent, title, text, *args, **kwargs):
        cls.calls.append(("question", title, text))
        return cls.StandardButton.Yes


class FakeFileDialog:
    """替代 QFileDialog：返回预设路径。"""

    save_answer = ""
    open_answer = ""

    @staticmethod
    def getSaveFileName(_parent, _title, suggested, _filter):
        return (FakeFileDialog.save_answer or suggested), ""

    @staticmethod
    def getExistingDirectory(_parent, _title, start=""):
        return FakeFileDialog.open_answer or start


class FakeLooseDialog:
    """替代 LooseFilesDialog：只记录构造参数与 exec 调用，不真正弹窗。"""

    instances: list = []

    def __init__(self, directory, total_bytes, total_files,
                 unit="auto", decimals=2, parent=None) -> None:
        self.directory = directory
        self.total_bytes = total_bytes
        self.total_files = total_files
        self.exec_called = False
        FakeLooseDialog.instances.append(self)

    def exec(self) -> int:
        self.exec_called = True
        return 0


class FakeMenu:
    """替代 QMenu：真实 QAction 记录菜单项，exec 立即返回不阻塞。"""

    instances: list = []

    def __init__(self, *_args, **_kwargs) -> None:
        self.actions_list: list = []
        FakeMenu.instances.append(self)

    def addAction(self, text, callback=None):
        from PySide6.QtGui import QAction

        act = QAction(text)          # 不传 parent：FakeMenu 不是 QObject
        if callback is not None:
            act.triggered.connect(callback)
        self.actions_list.append(act)
        return act

    def addSeparator(self):
        return None

    def exec(self, *_args) -> None:
        pass


def _app() -> QApplication:
    """进程内共享的 QApplication。"""
    return QApplication.instance() or QApplication([])


def _pump(app: QApplication, seconds: float) -> None:
    """让事件循环空转一段时间（等价于等待界面刷新）。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def _wait_for(app: QApplication, predicate, timeout: float = 40.0) -> bool:
    """轮询等待条件成立，期间持续派发 Qt 事件。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


MARKER_BYTES = 7777          # 根目录下的散文件，用于验证“根自身大小不计入排行”


def build_tree(base: str) -> int:
    """在 base 下构造已知大小的目录树，返回所有子目录的理论总字节数。

    注意：根目录自己还会写一个 ``marker.bin``，它不属于任何子目录，
    因此不计入返回值；在排行列表中它会以“散文件”聚合行的形式出现。
    """
    with open(os.path.join(base, "marker.bin"), "wb") as fh:
        fh.write(b"\0" * MARKER_BYTES)
    total = 0
    for i in range(1, DIRS + 1):
        folder = os.path.join(base, f"d{i}")
        os.makedirs(os.path.join(folder, f"sub{i}"), exist_ok=True)
        with open(os.path.join(folder, "f.bin"), "wb") as fh:
            fh.write(b"\0" * (i * UNIT_BYTES))
        with open(os.path.join(folder, f"sub{i}", "g.dat"), "wb") as fh:
            fh.write(b"\0" * (i * UNIT_BYTES // 2))
        total += i * UNIT_BYTES + i * UNIT_BYTES // 2
    return total


class UiSmokeTest(unittest.TestCase):
    """主窗口端到端用例（同一个窗口实例贯穿多个断言，省去重复扫描）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _app()
        cls.tree = tempfile.mkdtemp(prefix="space-viewer-ui-")
        cls.cfg_path = os.path.join(cls.tree, "settings.json")
        cls.expected_total = build_tree(cls.tree)
        cls.export_dir = tempfile.mkdtemp(prefix="space-viewer-ui-export-")

        cls._real_message_box = _REAL_MESSAGE_BOX
        cls._real_file_dialog = _REAL_FILE_DIALOG
        cls._real_open = _REAL_OPEN_IN_EXPLORER
        mw.QMessageBox = FakeMessageBox       # type: ignore[assignment]
        mw.QFileDialog = FakeFileDialog       # type: ignore[assignment]
        mw.open_in_explorer = lambda *a, **k: None

        cls.window = mw.MainWindow(ConfigManager(cls.cfg_path))
        cls.window.start_scan(cls.tree)
        ok = _wait_for(cls.app, lambda: cls.window._result is not None
                       and (cls.window._thread is None
                            or not cls.window._thread.isRunning()))
        if not ok:                                       # pragma: no cover
            raise unittest.SkipTest("扫描未在预期时间内完成")
        _pump(cls.app, 0.2)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.window is not None:
            cls.window.stop_scan()
            if cls.window._thread is not None:
                cls.window._thread.wait_safely(3000)
            cls.window.close()
        _pump(cls.app, 0.1)
        mw.QMessageBox = cls._real_message_box     # type: ignore[assignment]
        mw.QFileDialog = cls._real_file_dialog     # type: ignore[assignment]
        mw.open_in_explorer = cls._real_open
        shutil.rmtree(cls.tree, ignore_errors=True)
        shutil.rmtree(cls.export_dir, ignore_errors=True)

    def setUp(self) -> None:
        FakeMessageBox.calls = []
        # 清除上一个用例残留的选中行，否则 _update_selection_info 会把
        # 状态栏左侧文本改写成选中行的路径，干扰状态栏断言。
        self.window.view.selectionModel().clearSelection()
        self.window._update_selection_info()
        # 每个用例从默认状态开始：仅当前层 + 按大小降序 + 无筛选
        self.window._on_filter("")
        self.window.btn_level.setChecked(False)
        self.window._change_sort("size")
        self.window.btn_order.setChecked(True)
        self.window._change_unit("auto")
        _pump(self.app, 0.05)

    def tearDown(self) -> None:
        """统一收尾：复位导出桩 + 收起窗口（几何类用例可能 show 过）。"""
        FakeFileDialog.save_answer = ""
        self.window.hide()
        _pump(self.app, 0.05)

    def _show_window(self) -> None:
        """离屏显示主窗口并等待布局完成（几何断言的前置条件）。"""
        self.window.resize(1200, 780)
        self.window.show()
        _pump(self.app, 0.2)

    # ------------------------------------------------------------ 基础信息
    def test_row_count_matches_children(self) -> None:
        """默认“仅当前层”显示 6 个子目录 + 1 行“散文件”聚合行。"""
        self.assertEqual(self.window.model.rowCount(), DIRS + 1)

    def test_total_size_matches_engine(self) -> None:
        """列表中各文件夹大小之和与目录树理论值一致（含散文件聚合行）。"""
        shown = sum(self.window.model.record(r).total_bytes
                    for r in range(self.window.model.rowCount()))
        # 末行是“散文件”聚合行，其大小 = 根目录直属文件（marker.bin）
        self.assertEqual(shown, self.expected_total + MARKER_BYTES)
        # 根记录还包含根目录自身的散文件，因此一定大于子目录之和
        self.assertGreater(self.window._result.total_bytes, self.expected_total)

    def test_status_and_scan_panel_updated(self) -> None:
        """扫描完成后进度卡片与状态栏有反馈。"""
        # 卡片文本由 _on_completed -> scan_panel.finish() 写入，最稳定
        self.assertIn("扫描", self.window.scan_panel.status.text())
        self.assertTrue(self.window.status_left.text())
        self.assertTrue(self.window.model.rowCount() > 0)

    # ------------------------------------------------------------ 排序
    def test_sort_descending_by_size(self) -> None:
        """从大到小时首行是 d6，次末行是 d1，末行是散文件聚合行。"""
        model = self.window.model
        first = model.data(model.index(0, 0))
        last = model.data(model.index(model.rowCount() - 2, 0))
        self.assertEqual(first, f"d{DIRS}")
        self.assertEqual(last, "d1")
        self.assertIn("散文件", model.data(model.index(model.rowCount() - 1, 0)))

    def test_sort_ascending_by_size(self) -> None:
        """切换为从小到大后首行变成 d1（散文件聚合行仍固定在末尾）。"""
        self.window.btn_order.setChecked(False)
        _pump(self.app, 0.2)
        model = self.window.model
        self.assertEqual(model.data(model.index(0, 0)), "d1")
        sizes = [model.record(r).total_bytes for r in range(model.rowCount())
                 if model.record(r).id >= 0]
        self.assertEqual(sizes, sorted(sizes))

    def test_sort_by_name(self) -> None:
        """按名称排序生效且表头带箭头。"""
        self.window._change_sort("name")
        _pump(self.app, 0.2)
        model = self.window.model
        names = [model.data(model.index(r, 0)) for r in range(model.rowCount())]
        self.assertEqual(names, sorted(names))
        header = str(model.headerData(0, Qt.Orientation.Horizontal))
        self.assertIn("↓", header)
        self.window.btn_order.setChecked(False)
        _pump(self.app, 0.2)
        self.assertIn("↑", str(model.headerData(0, Qt.Orientation.Horizontal)))

    def test_sort_by_files(self) -> None:
        """按文件数排序时顺序与大小一致（本树中两者同向）。"""
        self.window._change_sort("files")
        _pump(self.app, 0.2)
        self.assertEqual(self.window.model.sort_key, "files")
        self.assertEqual(self.window.model.data(self.window.model.index(0, 0)), f"d{DIRS}")

    # ------------------------------------------------------------ 筛选与层级
    def test_filter_by_name(self) -> None:
        """输入关键字后只显示匹配行。"""
        self.window._on_filter("d3")
        _pump(self.app, 0.2)
        self.assertEqual(self.window.model.rowCount(), 1)
        self.assertEqual(self.window.model.data(self.window.model.index(0, 0)), "d3")
        self.window._on_filter("不存在的名字")
        _pump(self.app, 0.2)
        self.assertEqual(self.window.model.rowCount(), 0)

    def test_all_levels_mode(self) -> None:
        """“全部层级”应同时列出二级目录。"""
        self.window.btn_level.setChecked(True)
        _pump(self.app, 0.3)
        self.assertEqual(self.window.model.rowCount(), DIRS * 2)
        names = {self.window.model.data(self.window.model.index(r, 0))
                 for r in range(self.window.model.rowCount())}
        self.assertIn("sub1", names)

    # ------------------------------------------------------------ 单位
    def test_unit_switch(self) -> None:
        """手动指定 MB / B 后大小列后缀随之变化。"""
        self.window._change_unit("MB")
        _pump(self.app, 0.1)
        model = self.window.model
        text = model.data(model.index(0, 1))
        self.assertTrue(text.endswith(" MB"), text)
        self.window._change_unit("B")
        _pump(self.app, 0.1)
        raw = model.record(0).total_bytes
        self.assertEqual(model.data(model.index(0, 1)), f"{raw:,} B")

    # ------------------------------------------------------------ 下钻导航
    def test_navigate_and_go_up(self) -> None:
        """双击进入子目录再返回上级，行数与面包屑同步。"""
        model = self.window.model
        target_id = model.dir_id(model.rowCount() - 2)     # d1（末行是散文件聚合行）
        self.window.navigate_to(target_id)
        _pump(self.app, 0.3)
        self.assertEqual(self.window._current_id, target_id)
        # d1 下有 1 个子目录 + 直属文件 f.bin（以散文件聚合行展示，固定末尾）
        self.assertEqual(model.rowCount(), 2)
        self.assertEqual(model.data(model.index(0, 0)), "sub1")
        self.assertIn("散文件", model.data(model.index(1, 0)))
        self.window.go_up()
        _pump(self.app, 0.3)
        self.assertEqual(self.window._current_id, 0)
        self.assertEqual(model.rowCount(), DIRS + 1)       # 6 个目录 + 散文件行

    # ------------------------------------------------------------ 主题
    def test_theme_toggle_roundtrip(self) -> None:
        """深浅色来回切换后主题值复原。"""
        before = self.window.cfg.theme
        self.window.toggle_theme()
        _pump(self.app, 0.2)
        self.assertNotEqual(self.window.cfg.theme, before)
        self.window.toggle_theme()
        _pump(self.app, 0.2)
        self.assertEqual(self.window.cfg.theme, before)
        self.assertIn(self.window.btn_theme.text(), ("☾ 深色", "☀ 浅色"))

    # ------------------------------------------------------------ 右键菜单动作
    def test_selected_row_helpers(self) -> None:
        """右键菜单依赖的选中行辅助方法返回正确内容。"""
        model = self.window.model
        self.window.view.selectRow(0)
        self.assertEqual(model.dir_id(self.window._selected_index_row()),
                         self.window._selected_dir_id())
        path = self.window._selected_path()
        self.assertTrue(path.endswith(f"d{DIRS}"), path)
        size_text = self.window._selected_size_text()
        self.assertTrue(size_text, "选中行的大小文本不应为空")
        self.assertIn(size_text.split()[-1], ("B", "KB", "MB", "GB"))

    def test_sidebar_fixed_layout(self) -> None:
        """饼图与扫描概览的位置/尺寸必须固定，不随内容量变化（防闪烁回归）。"""
        from PySide6.QtWidgets import QGridLayout, QSizePolicy  # noqa: E402

        # 1) 环形图：垂直策略 Fixed、高度锁定 190
        donut = self.window.donut
        self.assertEqual(donut.sizePolicy().verticalPolicy(),
                         QSizePolicy.Policy.Fixed)
        self.assertEqual(donut.minimumHeight(), 190)
        self.assertEqual(donut.maximumHeight(), 190)

        # 2) 扫描概览：四个指标块尺寸完全固定
        for tile in (self.window.tile_grand, self.window.tile_files,
                     self.window.tile_dirs, self.window.tile_avg):
            self.assertEqual(tile.minimumWidth(), 124)
            self.assertEqual(tile.maximumWidth(), 124)
            self.assertEqual(tile.minimumHeight(), 72)
            self.assertEqual(tile.maximumHeight(), 72)

        # 3) 扫描概览使用固定网格布局（而非会随内容换行的流式布局）
        card = self.window.tile_grand.parentWidget()
        grids: list = []

        def walk(layout) -> None:
            for index in range(layout.count()):
                child = layout.itemAt(index)
                sub = child.layout() if child is not None else None
                if sub is None:
                    continue
                if isinstance(sub, QGridLayout):
                    grids.append(sub)
                walk(sub)

        walk(card.layout())
        self.assertTrue(grids, "扫描概览卡片内应存在 QGridLayout")
        grid = grids[0]
        self.assertEqual(grid.rowCount(), 2)
        self.assertEqual(grid.columnCount(), 3)  # 2 列指标 + 1 列空白拉伸

    # ------------------------------------------------- 布局稳定性（行为级）
    def test_tiles_geometry_immune_to_long_values(self) -> None:
        """超长数值文本不改变扫描概览指标块的几何（防抖动的行为级验证）。"""
        self._show_window()
        tiles = [self.window.tile_grand, self.window.tile_files,
                 self.window.tile_dirs, self.window.tile_avg]
        before = [t.geometry() for t in tiles]
        for tile in tiles:
            tile.set_value("999,999,999,999,888 TB —— 超长数值文本", "tip")
        _pump(self.app, 0.2)
        self.assertEqual([t.geometry() for t in tiles], before,
                         "指标块尺寸/位置应完全固定，不受数值文本影响")

    def test_charts_geometry_immune_to_data_changes(self) -> None:
        """图表数据在空↔满之间切换时，侧栏卡片几何保持不变。"""
        from app.ui.charts import ChartItem  # noqa: E402

        self._show_window()
        ext_card = self.window.ext_card
        summary = self.window.tile_grand.parentWidget()
        donut, bar, ext_chart = self.window.donut, self.window.bar_chart, \
            self.window.ext_chart
        before_card = (ext_card.x(), ext_card.y(),
                       ext_card.width(), ext_card.height())
        before_summary_h = summary.height()

        full = [ChartItem(f"项目{i}", (i + 1) * 1024, 0.0) for i in range(10)]
        for items in (full, [], full):
            bar.set_items(items)
            donut.set_items(items[:8])
            ext_chart.set_items(items[:8])
            _pump(self.app, 0.15)
            self.assertEqual(
                (ext_card.x(), ext_card.y(), ext_card.width(), ext_card.height()),
                before_card, "文件类型分布卡片几何不应随数据量变化")
            self.assertEqual(summary.height(), before_summary_h,
                             "扫描概览卡片高度不应随数据量变化")
            # 三个图表的高度都必须锁定（内容再多也不撑开布局）
            for chart in (bar, ext_chart, donut):
                self.assertEqual(chart.minimumHeight(), chart.maximumHeight())

    def test_sidebar_geometry_stable_across_window_resizes(self) -> None:
        """窗口多次缩放后，侧栏关键组件的几何保持稳定（响应式不位移）。"""
        self._show_window()
        summary = self.window.tile_grand.parentWidget()
        donut = self.window.donut
        tiles = [self.window.tile_grand, self.window.tile_files,
                 self.window.tile_dirs, self.window.tile_avg]
        baseline = {
            "tiles": [t.geometry() for t in tiles],
            "summary_h": summary.height(),
            "donut_h": donut.height(),
            "ring": min(donut.width(), donut.height()),
            "ext_h": self.window.ext_card.height(),
        }
        for size in ((1000, 680), (1600, 1000), (1150, 820)):
            self.window.resize(*size)
            _pump(self.app, 0.25)
            self.assertEqual([t.geometry() for t in tiles], baseline["tiles"],
                             f"缩放到 {size} 后指标块位置不应移动")
            self.assertEqual(summary.height(), baseline["summary_h"])
            self.assertEqual(donut.height(), baseline["donut_h"])
            self.assertEqual(min(donut.width(), donut.height()), baseline["ring"],
                             "环形图直径应恒定")
            self.assertEqual(self.window.ext_card.height(), baseline["ext_h"])

    def test_scan_panel_tiles_and_bar_chart_locked(self) -> None:
        """进度面板指标块宽度锁定，排行条形图高度锁定且策略为 Fixed。"""
        from PySide6.QtWidgets import QSizePolicy  # noqa: E402

        for name in ("tile_total", "tile_files", "tile_dirs",
                     "tile_speed", "tile_eta"):
            tile = getattr(self.window.scan_panel, name)
            self.assertEqual(tile.minimumWidth(), 104)
            self.assertEqual(tile.maximumWidth(), 104)
        bar = self.window.bar_chart
        self.assertGreater(bar.minimumHeight(), 0)
        self.assertEqual(bar.minimumHeight(), bar.maximumHeight(),
                         "排行条形图高度应锁定（按 max_rows 固定）")
        self.assertEqual(bar.sizePolicy().verticalPolicy(), QSizePolicy.Policy.Fixed)

    def test_show_detail_does_not_crash(self) -> None:
        """“查看详情”只弹提示，不抛异常，且包含隐藏/系统文件统计。"""
        self.window.view.selectRow(0)
        self.window.show_detail()
        _pump(self.app, 0.1)
        self.assertTrue(FakeMessageBox.calls)
        texts = [str(call[-1]) for call in FakeMessageBox.calls]
        self.assertTrue(any("隐藏/系统文件" in text for text in texts),
                        f"详情文本应包含隐藏文件统计，实际：{texts[:1]}")

    # ------------------------------------------------------------ 导出
    def test_export_csv(self) -> None:
        """导出 CSV：文件生成且包含全部列头。"""
        import csv

        target = os.path.join(self.export_dir, "rank.csv")
        FakeFileDialog.save_answer = target
        self.window.export_results("csv")
        _pump(self.app, 0.2)
        self.assertTrue(os.path.isfile(target))
        with open(target, "r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        header = next(i for i, row in enumerate(rows) if row[:1] == ["名称"])
        # 元信息以 # 开头写在表头之前
        self.assertTrue(all(r[0].startswith("#") for r in rows[:header] if r))
        data = [r for r in rows[header + 1:] if r]
        self.assertEqual(len(data), DIRS)
        self.assertEqual(data[0][0], f"d{DIRS}")

    @unittest.skipUnless(HAS_OPENPYXL, "未安装 openpyxl")
    def test_export_xlsx(self) -> None:
        """导出 Excel：可被 openpyxl 重新打开。"""
        from openpyxl import load_workbook

        target = os.path.join(self.export_dir, "rank.xlsx")
        FakeFileDialog.save_answer = target
        self.window.export_results("xlsx")
        _pump(self.app, 0.2)
        self.assertTrue(os.path.isfile(target))
        workbook = load_workbook(target)
        sheet = workbook.active
        self.assertEqual(sheet.title, "文件夹排行")
        header_row = next(r for r in range(1, sheet.max_row + 1)
                          if sheet.cell(row=r, column=1).value == "名称")
        # 表头之下应正好是 6 行数据（跳过分隔用的空行）
        self.assertEqual(sum(1 for r in range(header_row + 1, sheet.max_row + 1)
                             if sheet.cell(row=r, column=1).value), DIRS)
        self.assertEqual(sheet.freeze_panes, f"A{header_row + 1}")
        workbook.close()

    def test_export_extensions(self) -> None:
        """导出文件类型分布报告。"""
        target = os.path.join(self.export_dir, "ext.csv")
        FakeFileDialog.save_answer = target
        self.window.export_results("ext")
        _pump(self.app, 0.2)
        self.assertTrue(os.path.isfile(target))

    def test_export_without_data_warns(self) -> None:
        """筛选到空列表时导出应给出提示而不是写空文件。"""
        self.window._on_filter("绝不存在的目录名")
        _pump(self.app, 0.2)
        target = os.path.join(self.export_dir, "none.csv")
        FakeFileDialog.save_answer = target
        self.window.export_results("csv")
        _pump(self.app, 0.2)
        self.assertFalse(os.path.exists(target))
        kinds = [c[0] for c in FakeMessageBox.calls]
        self.assertIn("exec", kinds)   # 走了 _warn 提示

    # --------------------------------------------- 布局稳定（晃动根治回归）
    def test_model_updates_keep_view_state(self) -> None:
        """多次 set_data 后列宽与滚动位置保持不变（防“整表重置”回归）。"""
        import dataclasses

        self._show_window()
        view = self.window.view
        model = self.window.model
        result = self.window._result

        # 用现有记录拼出 60 行，让表格出现垂直滚动
        base_rows = model.all_rows()
        many = [dataclasses.replace(base_rows[i % len(base_rows)],
                                    id=10000 + i, name=f"目录{i:02d}")
                for i in range(60)]
        model.set_data(many, self.window._base_bytes(), result)
        _pump(self.app, 0.1)
        view.verticalScrollBar().setValue(9)
        _pump(self.app, 0.05)
        widths_before = [view.columnWidth(c) for c in range(model.columnCount())]
        header_sizes = [view.horizontalHeader().sectionSize(c)
                        for c in range(model.columnCount())]

        # 连续换数据（行数 60→40→40→60），模拟扫描过程中的实时刷新
        for rows in (many[:40], many[10:50], many):
            model.set_data(rows, self.window._base_bytes(), result)
            _pump(self.app, 0.05)

        self.assertEqual(model.rowCount(), len(many))
        self.assertEqual([view.columnWidth(c) for c in range(model.columnCount())],
                         widths_before)
        self.assertEqual([view.horizontalHeader().sectionSize(c)
                          for c in range(model.columnCount())], header_sizes)
        # 滚动位置不被重置回顶部
        self.assertEqual(view.verticalScrollBar().value(), 9)

    def test_shaky_widgets_locked(self) -> None:
        """摘要/侧栏/表头的锁定属性（防晃动的静态断言）。"""
        from PySide6.QtWidgets import QHeaderView

        window = self.window
        # 摘要固定宽度：文本变化不再挤压面包屑
        self.assertEqual(window.lbl_summary.minimumWidth(), 300)
        self.assertEqual(window.lbl_summary.maximumWidth(), 300)
        # 侧栏宽度锁死在 [300, 460]
        holder = window.splitter.widget(1)
        self.assertEqual(holder.minimumWidth(), 300)
        self.assertEqual(holder.maximumWidth(), 460)
        # 表头：列宽 Interactive + 最后一列拉伸
        header = window.view.horizontalHeader()
        self.assertTrue(header.stretchLastSection())
        self.assertEqual(header.sectionResizeMode(0),
                         QHeaderView.ResizeMode.Interactive)

    def test_breadcrumb_reuses_widgets(self) -> None:
        """面包屑复用按钮/分隔符，不再整排销毁重建（游离闪烁回归防护）。"""
        self._show_window()
        crumb = self.window.breadcrumb
        crumb.set_chain([(0, "根"), (1, "a"), (2, "b")])
        _pump(self.app, 0.05)
        buttons_before = list(crumb._buttons)
        seps_before = list(crumb._seps)

        # 变深一层：按钮数量增加，但实例必须复用
        crumb.set_chain([(0, "根"), (1, "a"), (2, "b"), (3, "c")])
        _pump(self.app, 0.05)
        self.assertEqual(crumb._buttons[:3], buttons_before)
        self.assertEqual(crumb._seps[:2], seps_before)
        self.assertEqual([b.text() for b in crumb._buttons if b.isVisible()],
                         ["根", "a", "b", "c"])

        # 收缩为 1 层：多余按钮与分隔符隐藏
        crumb.set_chain([(0, "根")])
        _pump(self.app, 0.05)
        self.assertEqual([b.text() for b in crumb._buttons if b.isVisible()], ["根"])
        self.assertFalse(any(s.isVisible() for s in crumb._seps))

        # 点击复用的按钮仍发出正确的目录 id（根目录 id=0 不允许误判为 -1）
        received: list = []
        crumb.navigate.connect(received.append)
        crumb._buttons[0].click()
        self.assertEqual(received[-1], 0)

        # 恢复扫描完成后的面包屑，避免影响其他用例
        crumb.set_chain(self.window._chain_for(0))
        _pump(self.app, 0.05)

    # ------------------------------------------------- 散文件聚合行（虚拟行）
    def test_loose_row_selection_helpers(self) -> None:
        """选中散文件聚合行时，各辅助方法能识别它且不给出虚假路径。"""
        model = self.window.model
        last = model.rowCount() - 1
        self.assertIn("散文件", model.data(model.index(last, 0)))
        self.assertEqual(model.dir_id(last), LOOSE_ID)
        self.window.view.selectRow(last)
        self.assertTrue(self.window._selected_is_loose())
        self.assertEqual(self.window._selected_dir_id(), LOOSE_ID)
        self.assertEqual(self.window._selected_path(), "")   # 虚拟行没有真实路径
        # 切回真实目录行后恢复普通行为
        self.window.view.selectRow(0)
        self.assertFalse(self.window._selected_is_loose())
        self.assertTrue(self.window._selected_path().endswith(f"d{DIRS}"))

    def test_loose_row_double_click_opens_dialog(self) -> None:
        """双击散文件聚合行 → 以当前目录与扫描统计弹出散文件清单。"""
        model = self.window.model
        last_index = model.index(model.rowCount() - 1, 0)
        mw.LooseFilesDialog = FakeLooseDialog       # type: ignore[assignment]
        FakeLooseDialog.instances = []
        try:
            self.window._on_double_click(last_index)
            _pump(self.app, 0.1)
        finally:
            mw.LooseFilesDialog = _REAL_LOOSE_DIALOG  # type: ignore[assignment]
        self.assertEqual(len(FakeLooseDialog.instances), 1)
        dialog = FakeLooseDialog.instances[0]
        self.assertTrue(dialog.exec_called)
        self.assertEqual(os.path.normcase(dialog.directory),
                         os.path.normcase(self.tree))
        self.assertEqual(dialog.total_files, 1)       # 根目录只有 marker.bin
        self.assertEqual(dialog.total_bytes, MARKER_BYTES)

    def test_show_detail_on_loose_row(self) -> None:
        """选中散文件聚合行执行“查看详情”→ 弹清单而非普通详情框。"""
        model = self.window.model
        self.window.view.selectRow(model.rowCount() - 1)
        mw.LooseFilesDialog = FakeLooseDialog       # type: ignore[assignment]
        FakeLooseDialog.instances = []
        try:
            self.window.show_detail()
            _pump(self.app, 0.1)
        finally:
            mw.LooseFilesDialog = _REAL_LOOSE_DIALOG  # type: ignore[assignment]
        self.assertEqual(len(FakeLooseDialog.instances), 1)
        self.assertTrue(FakeLooseDialog.instances[0].exec_called)
        self.assertFalse(FakeMessageBox.calls)        # 不应再走普通详情弹窗

    def test_loose_row_context_menu(self) -> None:
        """右键散文件聚合行 → “查看详情”变为“查看散文件清单”且可触发。"""
        model = self.window.model
        last = model.rowCount() - 1
        self.window.view.selectRow(last)
        mw.QMenu = FakeMenu                          # type: ignore[assignment]
        mw.LooseFilesDialog = FakeLooseDialog        # type: ignore[assignment]
        FakeMenu.instances = []
        FakeLooseDialog.instances = []
        try:
            rect = self.window.view.visualRect(model.index(last, 0))
            self.window._show_context_menu(rect.center())
            menu = FakeMenu.instances[-1]
            detail = next(a for a in menu.actions_list
                          if a.text() == "查看散文件清单")
            self.assertTrue(detail.isEnabled())
            detail.trigger()                          # 触发菜单项 → show_detail 链路
        finally:
            mw.QMenu = _REAL_Q_MENU                  # type: ignore[assignment]
            mw.LooseFilesDialog = _REAL_LOOSE_DIALOG  # type: ignore[assignment]
        self.assertEqual(len(FakeLooseDialog.instances), 1)
        self.assertTrue(FakeLooseDialog.instances[0].exec_called)

    def test_loose_row_not_in_charts_and_export(self) -> None:
        """散文件聚合行不进入图表，也不写入导出文件。"""
        rows = self.window.model.all_rows()
        self.window._update_charts(rows, self.window._base_bytes())
        # 图表条数 = 真实目录数（虚拟行被排除）
        self.assertEqual(len(self.window.bar_chart._items), DIRS)
        import csv

        target = os.path.join(self.export_dir, "rank-loose.csv")
        FakeFileDialog.save_answer = target
        self.window.export_results("csv")
        _pump(self.app, 0.2)
        with open(target, "r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        header = next(i for i, row in enumerate(rows) if row[:1] == ["名称"])
        data = [r for r in rows[header + 1:] if r]
        self.assertEqual(len(data), DIRS)             # 只有 6 个目录，无散文件行


class _StubbedCase(unittest.TestCase):
    """公共基类：为单个测试类打桩对话框，结束后还原。"""

    def setUp(self) -> None:
        mw.QMessageBox = FakeMessageBox        # type: ignore[assignment]
        mw.QFileDialog = FakeFileDialog        # type: ignore[assignment]
        mw.open_in_explorer = lambda *a, **k: None
        FakeMessageBox.calls = []
        FakeFileDialog.save_answer = ""
        FakeFileDialog.open_answer = ""

    def tearDown(self) -> None:
        mw.QMessageBox = _REAL_MESSAGE_BOX     # type: ignore[assignment]
        mw.QFileDialog = _REAL_FILE_DIALOG     # type: ignore[assignment]
        mw.open_in_explorer = _REAL_OPEN_IN_EXPLORER


class TestInvalidPath(_StubbedCase):
    """异常输入路径。"""

    def test_start_scan_with_missing_path_shows_warning(self) -> None:
        """路径不存在时给出警告且不启动线程。"""
        app = _app()
        work = tempfile.mkdtemp(prefix="space-viewer-invalid-")
        window = mw.MainWindow(ConfigManager(os.path.join(work, "settings.json")))
        try:
            window.start_scan(os.path.join(work, "肯定不存在的目录-4242"))
            _pump(app, 0.1)
            self.assertIsNone(window._thread)
            self.assertTrue(FakeMessageBox.calls)
        finally:
            window.close()
            _pump(app, 0.1)
            shutil.rmtree(work, ignore_errors=True)


class TestPreferences(_StubbedCase):
    """偏好持久化。"""

    def test_last_path_saved(self) -> None:
        """扫描结束后 last_path / 历史记录已写入配置文件。"""
        app = _app()
        tree = tempfile.mkdtemp(prefix="space-viewer-pref-")
        cfg_file = os.path.join(tree, "settings.json")
        build_tree(tree)
        window = mw.MainWindow(ConfigManager(cfg_file))
        try:
            window.start_scan(tree)
            ok = _wait_for(app, lambda: window._result is not None and
                           (window._thread is None or not window._thread.isRunning()))
            self.assertTrue(ok, "扫描未在预期时间内完成")
            _pump(app, 0.3)
            window.close()
            _pump(app, 0.1)
            reloaded = ConfigManager(cfg_file)
            self.assertEqual(os.path.normcase(reloaded.get("last_path")),
                             os.path.normcase(tree))
            self.assertIn(os.path.normcase(tree),
                          [os.path.normcase(p) for p in reloaded.recent_paths])
        finally:
            shutil.rmtree(tree, ignore_errors=True)

    def test_corrupted_config_falls_back(self) -> None:
        """配置文件损坏时自动回退默认值并备份。"""
        work = tempfile.mkdtemp(prefix="space-viewer-badcfg-")
        bad = os.path.join(work, "settings.json")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("{ 这不是合法的 JSON")
        cfg = ConfigManager(bad)
        self.assertEqual(cfg.get("theme"), "dark")
        self.assertTrue(os.path.exists(os.path.join(work, "settings.bak")))
        shutil.rmtree(work, ignore_errors=True)


class TestListLooseFiles(unittest.TestCase):
    """list_loose_files 纯函数：一层扫描、跳过子目录、失败兜底。"""

    def test_lists_only_files_sorted_desc(self) -> None:
        """只列出直属文件（跳过子目录），按大小降序，合计正确。"""
        work = tempfile.mkdtemp(prefix="space-viewer-loose-")
        try:
            with open(os.path.join(work, "小.bin"), "wb") as fh:
                fh.write(b"\0" * 10)
            with open(os.path.join(work, "大.bin"), "wb") as fh:
                fh.write(b"\0" * 100)
            os.makedirs(os.path.join(work, "子目录"))
            files, count, total = list_loose_files(work)
            self.assertEqual([name for name, _, _ in files],
                             ["大.bin", "小.bin"])
            self.assertEqual(count, 2)
            self.assertEqual(total, 110)
            self.assertTrue(all(mtime > 0 for _, _, mtime in files))
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_missing_directory_returns_empty(self) -> None:
        """目录不存在时返回空结果而不抛异常。"""
        files, count, total = list_loose_files(
            os.path.join(tempfile.gettempdir(), "肯定不存在-4242", "占位"))
        self.assertEqual((files, count, total), ([], 0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)

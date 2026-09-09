"""扫描引擎测试：在临时目录里构造一棵“已知大小”的目录树，验证统计准确性。

覆盖需求中的“扫描结果准确无误”“支持中断与恢复”“层级/数量上限”等核心逻辑。

运行方式::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.engine import FolderScanner, ScanOptions, _normalize_root, quick_estimate  # noqa: E402
from app.core.models import ScanResult, row_to_dict  # noqa: E402

# 扫描引擎的 INFO 日志会让测试输出很吵，测试期间只保留 WARNING 以上
logging.disable(logging.INFO)

# 目录树布局（字节数是断言依据，改动会连带影响所有期望值）
FILE_ROOT = 100          # <root>/x.txt
FILE_A = 1024            # <root>/a/1.bin
FILE_AB = 2048           # <root>/a/b/2.bin
FILE_RESERVED = 4096     # <root>/WindowsApps/big.bin
TOTAL_ALL = FILE_ROOT + FILE_A + FILE_AB + FILE_RESERVED   # 7268
TOTAL_NORMAL = TOTAL_ALL - FILE_RESERVED                  # 3172


def _write(path: str, size: int) -> None:
    """写入指定大小的文件（自动建父目录）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)


class EngineTestBase(unittest.TestCase):
    """公共脚手架：建临时目录树，测试结束清理。"""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="space-viewer-test-")
        _write(os.path.join(self.root, "x.txt"), FILE_ROOT)
        _write(os.path.join(self.root, "a", "1.bin"), FILE_A)
        _write(os.path.join(self.root, "a", "b", "2.bin"), FILE_AB)
        _write(os.path.join(self.root, "empty", "readme"), 0)
        _write(os.path.join(self.root, "WindowsApps", "big.bin"), FILE_RESERVED)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def by_name(self, result: ScanResult, name: str):
        """按目录名查记录（测试专用，名字在本树内唯一）。"""
        for record in result.records:
            if record.name == name:
                return record
        return None

    def scan(self, **kwargs) -> ScanResult:
        """跑一次扫描并返回结果。"""
        options = ScanOptions(root=self.root, **kwargs)
        return FolderScanner(options=options).run()


class TestScanAccuracy(EngineTestBase):
    """大小/数量统计的准确性。"""

    def test_total_bytes_recursive(self) -> None:
        """根目录递归大小 = 所有文件之和（排除保留目录后）。"""
        result = self.scan(skip_reserved=True)
        self.assertEqual(result.total_bytes, TOTAL_NORMAL)
        # 4 个文件：x.txt / a/1.bin / a/b/2.bin / empty/readme（0 字节也计数）
        self.assertEqual(result.total_files, 4)

    def test_parent_child_totals(self) -> None:
        """每个父目录的 total_bytes 等于其子孙 direct_bytes 之和。"""
        result = self.scan(skip_reserved=True)
        a = self.by_name(result, "a")
        b = self.by_name(result, "b")
        root = result.root
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertEqual(b.total_bytes, FILE_AB)
        self.assertEqual(b.total_files, 1)
        self.assertEqual(a.total_bytes, FILE_A + FILE_AB)
        self.assertEqual(a.total_files, 2)
        self.assertEqual(root.total_bytes, TOTAL_NORMAL)
        # direct_bytes 只含直属文件，不含子目录
        self.assertEqual(a.direct_bytes, FILE_A)
        self.assertEqual(root.direct_bytes, FILE_ROOT)

    def test_total_subdirs_after_finalize(self) -> None:
        """finalize() 之后递归子目录数正确。"""
        result = self.scan(skip_reserved=True)
        root = result.root
        a = self.by_name(result, "a")
        self.assertEqual(a.subdirs, 1)
        self.assertEqual(a.total_subdirs, 1)
        # root 下子孙：a / a/b / empty（WindowsApps 已被排除）
        self.assertEqual(root.total_subdirs, 3)

    def test_largest_file_propagated(self) -> None:
        """最大单个文件会向上传播。"""
        result = self.scan(skip_reserved=True)
        self.assertEqual(self.by_name(result, "a").largest_file, FILE_AB)
        self.assertEqual(result.root.largest_file, FILE_AB)

    def test_reserved_dir_included_by_default(self) -> None:
        """默认不排除系统保留目录时，其大小应计入。"""
        result = self.scan()
        self.assertEqual(result.total_bytes, TOTAL_ALL)

    def test_extension_stats(self) -> None:
        """扩展名归类统计正确（0 字节文件不计入）。"""
        result = self.scan(skip_reserved=True)
        self.assertEqual(result.ext_bytes[".bin"], FILE_A + FILE_AB)
        self.assertEqual(result.ext_bytes[".txt"], FILE_ROOT)
        self.assertEqual(result.ext_files[".txt"], 1)
        self.assertNotIn("(无扩展名)", result.ext_bytes)  # readme 大小为 0

    def test_no_progress_leak_between_runs(self) -> None:
        """total_bytes 不应被重复累加（propagate 只跑一次的回归用例）。"""
        scanner = FolderScanner(options=ScanOptions(root=self.root, skip_reserved=True))
        first = scanner.run()
        self.assertEqual(first.total_bytes, TOTAL_NORMAL)
        second = scanner.run()          # 复用 scanner 再扫一次
        self.assertEqual(second.total_bytes, TOTAL_NORMAL)

    @unittest.skipUnless(sys.platform.startswith("win"), "attrib 仅 Windows 可用")
    def test_hidden_files_counted_and_tracked(self) -> None:
        """隐藏/系统文件计入总大小，并单独记录 hidden 统计。

        对应真实场景：资源管理器默认不显示 pagefile.sys 等文件，
        手工加总比扫描结果小 —— 引擎必须把这类文件统计进去并能单独核对。
        """
        hidden_path = os.path.join(self.root, "a", "secret.bin")
        _write(hidden_path, 4096)
        subprocess.run(["attrib", "+h", "+s", os.path.abspath(hidden_path)],
                       check=True, capture_output=True)
        result = self.scan(skip_reserved=True)
        a = self.by_name(result, "a")
        self.assertIsNotNone(a)
        # 总大小包含隐藏文件
        self.assertEqual(a.total_bytes, FILE_A + FILE_AB + 4096)
        self.assertEqual(a.total_files, 3)
        # 直属与递归的 hidden 统计正确
        self.assertEqual(a.hidden_files, 1)
        self.assertEqual(a.hidden_bytes, 4096)
        self.assertEqual(a.total_hidden_files, 1)
        self.assertEqual(a.total_hidden_bytes, 4096)
        # 向上累加到根目录
        self.assertEqual(result.root.total_hidden_files, 1)
        self.assertEqual(result.root.total_hidden_bytes, 4096)

    @unittest.skipUnless(sys.platform.startswith("win"), "attrib 仅 Windows 可用")
    def test_skip_hidden_excludes_hidden_files(self) -> None:
        """勾选“跳过隐藏目录”后，隐藏文件同样不被统计且计入 skipped。"""
        hidden_path = os.path.join(self.root, "a", "secret.bin")
        _write(hidden_path, 4096)
        subprocess.run(["attrib", "+h", os.path.abspath(hidden_path)],
                       check=True, capture_output=True)
        result = self.scan(skip_hidden=True, skip_reserved=True)
        a = self.by_name(result, "a")
        self.assertEqual(a.total_bytes, FILE_A + FILE_AB)
        self.assertEqual(a.hidden_files, 0)
        self.assertGreaterEqual(result.skipped, 1)


class TestLimitsAndFlags(EngineTestBase):
    """层级上限、记录上限、保留目录与取消/暂停。"""

    def test_max_depth_truncates(self) -> None:
        """max_depth=1 时只展开一层，并标记结果不完整。"""
        result = self.scan(skip_reserved=True, max_depth=1)
        self.assertIsNotNone(self.by_name(result, "a"))
        self.assertIsNone(self.by_name(result, "b"))
        self.assertFalse(result.complete)
        self.assertTrue(self.by_name(result, "a").truncated)
        # 第二层未扫描，因此只统计到根目录直属文件
        self.assertEqual(result.total_bytes, FILE_ROOT)

    def test_max_records_limit(self) -> None:
        """记录数达到上限后停止新增目录。"""
        result = self.scan(skip_reserved=True, max_records=2)
        self.assertEqual(len(result.records), 2)
        self.assertFalse(result.complete)

    def test_skip_reserved_dir(self) -> None:
        """勾选排除保留目录后 WindowsApps 不被展开。"""
        result = self.scan(skip_reserved=True)
        self.assertIsNone(self.by_name(result, "WindowsApps"))

    def test_cancel_keeps_partial_result(self) -> None:
        """预先请求停止：立即返回且结果标记为已取消。"""
        scanner = FolderScanner(options=ScanOptions(root=self.root))
        scanner.request_cancel()
        result = scanner.run()
        self.assertTrue(result.cancelled)
        self.assertFalse(result.complete)
        self.assertEqual(len(result.records), 1)  # 只有根记录

    def test_pause_then_resume(self) -> None:
        """暂停时线程阻塞不产出结果，恢复后能扫描完整。"""
        scanner = FolderScanner(options=ScanOptions(root=self.root, skip_reserved=True))
        scanner.request_pause()
        worker = threading.Thread(target=scanner.run, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 5)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(scanner.result.records) > 1:
            time.sleep(0.02)
        self.assertTrue(worker.is_alive(), "暂停状态下扫描线程不应结束")
        self.assertTrue(scanner.is_paused)

        scanner.request_resume()
        worker.join(15)
        self.assertFalse(worker.is_alive(), "恢复后扫描线程应及时结束")
        self.assertEqual(scanner.result.total_bytes, TOTAL_NORMAL)
        self.assertTrue(scanner.result.complete)

    def test_progress_callback_and_final_percent(self) -> None:
        """进度回调可拿到快照，最终一次为 100%。"""
        received = []
        scanner = FolderScanner(
            options=ScanOptions(root=self.root, skip_reserved=True, progress_interval=0.0),
            on_progress=received.append,
        )
        scanner.run()
        self.assertTrue(received)
        self.assertEqual(received[-1].percent, 100.0)
        self.assertGreaterEqual(received[-1].stats.dirs_done, 1)
        self.assertIsInstance(received[-1].stats.current_path, str)

    def test_empty_root_raises(self) -> None:
        """空路径直接报错，避免误扫当前目录。"""
        with self.assertRaises(ValueError):
            _normalize_root("   ")
        with self.assertRaises(ValueError):
            FolderScanner(options=ScanOptions(root="")).run()


class TestResultQueries(EngineTestBase):
    """结果对象的查询接口。"""

    def setUp(self) -> None:
        super().setUp()
        self.result = self.scan(skip_reserved=True)

    def test_full_path_and_children(self) -> None:
        """完整路径回溯与子链遍历正确。"""
        a = self.by_name(self.result, "a")
        self.assertEqual(self.result.full_path(a.id), os.path.join(self.root, "a"))
        children = [self.result.records[i].name for i in self.result.children(a.id)]
        self.assertEqual(children, ["b"])

    def test_top_descendants_sorted(self) -> None:
        """top_descendants 按大小取前 N。"""
        top = self.result.top_descendants(0, 1, desc=True)
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0].name, "a")

    def test_get_out_of_range(self) -> None:
        """越界 id 返回 None（线程并发读取的容错）。"""
        self.assertIsNone(self.result.get(999999))
        self.assertEqual(self.result.full_path(999999), "")

    def test_row_to_dict_fields(self) -> None:
        """导出行的字段与 exporter.COLUMNS 对齐。"""
        a = self.by_name(self.result, "a")
        row = row_to_dict(self.result, a.id, self.result.total_bytes)
        self.assertEqual(row["name"], "a")
        self.assertEqual(row["size"], FILE_A + FILE_AB)
        self.assertAlmostEqual(row["percent"], (FILE_A + FILE_AB) / TOTAL_NORMAL, places=6)
        self.assertEqual(row["files"], 2)
        for key in ("path", "subdirs", "direct_files", "largest", "depth", "modified"):
            self.assertIn(key, row)

    def test_error_text_empty_when_no_error(self) -> None:
        """无错误时提示文本为空串。"""
        self.assertEqual(self.result.error_text(), "")


class TestRobustness(unittest.TestCase):
    """异常输入不应抛栈。"""

    def test_missing_directory_records_error(self) -> None:
        """根目录不存在：返回错误记录而不是崩溃。"""
        missing = os.path.join(tempfile.gettempdir(), "space-viewer-not-exists-1234567")
        shutil.rmtree(missing, ignore_errors=True)
        result = FolderScanner(options=ScanOptions(root=missing)).run()
        self.assertTrue(result.root.error)
        self.assertTrue(result.errors)

    def test_quoted_path_normalized(self) -> None:
        """带引号/结尾分隔符的路径能被正确规范化。"""
        base = tempfile.mkdtemp(prefix="space-viewer-norm-")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.assertEqual(_normalize_root(f'"{base}"'), os.path.abspath(base))
        self.assertEqual(_normalize_root(base + os.sep), os.path.abspath(base))

    def test_quick_estimate(self) -> None:
        """快速估算与精确扫描结果一致（小树场景）。"""
        base = tempfile.mkdtemp(prefix="space-viewer-quick-")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        _write(os.path.join(base, "f1.bin"), 500)
        _write(os.path.join(base, "sub", "f2.bin"), 250)
        total, count = quick_estimate(base)
        self.assertEqual(total, 750)
        self.assertGreaterEqual(count, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)

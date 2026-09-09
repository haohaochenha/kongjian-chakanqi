"""格式化工具的单元测试：单位换算、占比、耗时、文本解析。"""

from __future__ import annotations

import unittest

from app.core.formatter import (format_count, format_duration, format_percent,
                                format_size, format_speed, format_timestamp,
                                parse_size_text, percent_value, size_to_unit)


class TestSizeFormat(unittest.TestCase):
    """大小格式化。"""

    def test_auto_units(self) -> None:
        """自动模式：B / KB / MB / GB 逐级切换。"""
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(512), "512 B")
        self.assertEqual(format_size(1536), "1.50 KB")
        self.assertEqual(format_size(1024 * 1024), "1.00 MB")
        self.assertEqual(format_size(3 * 1024 ** 3), "3.00 GB")

    def test_fixed_unit(self) -> None:
        """手动指定单位时不做自动降级。"""
        self.assertEqual(format_size(1536, "KB"), "1.50 KB")
        self.assertEqual(format_size(1536, "MB"), "0.001 MB")
        self.assertEqual(format_size(1536, "B"), "1,536 B")

    def test_bad_input_never_raises(self) -> None:
        """异常输入（None、负数、字符串）都不能抛错。"""
        for bad in (None, -100, "abc", float("nan")):
            self.assertIsInstance(format_size(bad), str)
        self.assertEqual(format_size(-5), "0 B")

    def test_size_to_unit_pair(self) -> None:
        """数值与单位成对返回。"""
        value, unit = size_to_unit(1024 ** 2 * 5)
        self.assertEqual((value, unit), (5.0, "MB"))

    def test_thousands_separator(self) -> None:
        self.assertEqual(format_count(1234567), "1,234,567")
        self.assertEqual(format_count(None), "0")


class TestPercent(unittest.TestCase):
    """占比计算。"""

    def test_basic(self) -> None:
        self.assertEqual(format_percent(1, 4), "25.0%")
        self.assertEqual(format_percent(0, 0), "0.0%")
        self.assertEqual(format_percent(1, 0), "0.0%")

    def test_small_but_nonzero(self) -> None:
        """非零极小值显示成 <0.1%，避免误导。"""
        self.assertEqual(format_percent(1, 10 ** 6), "<0.1%")

    def test_ratio_clamped(self) -> None:
        self.assertEqual(percent_value(5, 2), 1.0)
        self.assertEqual(percent_value(-1, 2), 0.0)
        self.assertAlmostEqual(percent_value(1, 4), 0.25)


class TestTimeAndSpeed(unittest.TestCase):
    """耗时、速度与时间戳。"""

    def test_duration(self) -> None:
        self.assertEqual(format_duration(45), "45 秒")
        self.assertEqual(format_duration(125), "2 分 5 秒")
        self.assertIn("小时", format_duration(3700))
        self.assertEqual(format_duration(None), "--")
        self.assertEqual(format_duration(-1), "--")

    def test_speed(self) -> None:
        self.assertEqual(format_speed(1024), "1.00 KB/s")
        self.assertEqual(format_speed(0), "--")
        self.assertEqual(format_speed(None), "--")

    def test_timestamp(self) -> None:
        self.assertEqual(format_timestamp(0), "--")
        self.assertEqual(format_timestamp("bad"), "--")
        self.assertRegex(format_timestamp(1_700_000_000), r"^\d{4}-\d{2}-\d{2} ")


class TestParseSize(unittest.TestCase):
    """容量文本解析。"""

    def test_units(self) -> None:
        self.assertEqual(parse_size_text("1024"), 1024)
        self.assertEqual(parse_size_text("1KB"), 1024)
        self.assertEqual(parse_size_text("1.5 GB"), int(1.5 * 1024 ** 3))
        self.assertEqual(parse_size_text("junk"), 0)
        self.assertEqual(parse_size_text(None), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

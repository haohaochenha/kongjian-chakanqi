"""大小、数量、时间等数据的显示格式化工具。

所有函数均为纯函数，方便单元测试，也保证界面层与逻辑层不会互相污染。
"""

from __future__ import annotations

import datetime as _dt
from typing import Tuple

# 1 KB = 1024 B（Windows 资源管理器同样使用 1024 进制，保持与用户直觉一致）
_STEP = 1024.0
_UNITS: Tuple[str, ...] = ("B", "KB", "MB", "GB", "TB", "PB")

# 允许用户手动指定的单位（界面下拉框直接使用该常量）
UNIT_CHOICES: Tuple[str, ...] = ("auto", "B", "KB", "MB", "GB")


def _validate_bytes(num_bytes: object) -> int:
    """把任意输入安全地转换成非负整数字节数。

    :param num_bytes: 输入值（可能是 None / float / 负数）
    :return: 非负整数字节数
    """
    try:
        value = int(num_bytes)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def size_to_unit(num_bytes: object, unit: str = "auto") -> Tuple[float, str]:
    """把字节数换算成指定单位。

    :param num_bytes: 字节数
    :param unit: "auto" 自动选择合适单位，或 "B"/"KB"/"MB"/"GB"/"TB"/"PB"
    :return: (数值, 单位字符串)
    """
    value = float(_validate_bytes(num_bytes))
    if unit and unit != "auto":
        try:
            index = _UNITS.index(unit.upper())
        except ValueError:
            index = 2  # 非法单位时退回 MB，避免界面崩溃
        return value / (_STEP ** index), _UNITS[index]

    index = 0
    # 自动模式：选择让数值 >= 1 的最大单位，最大到 PB
    while value >= _STEP and index < len(_UNITS) - 1:
        value /= _STEP
        index += 1
    return value, _UNITS[index]


def format_size(num_bytes: object, unit: str = "auto", decimals: int = 2) -> str:
    """格式化文件大小，例如 ``1536 -> "1.50 KB"``。

    :param num_bytes: 字节数
    :param unit: 单位，"auto" 表示自动
    :param decimals: 小数位数（B 单位固定 0 位）
    :return: 带单位的可读字符串
    """
    value, suffix = size_to_unit(num_bytes, unit)
    if suffix == "B":
        # 字节数没有小数，但同样加千分位分隔符，与资源管理器显示习惯一致
        return f"{int(value):,} B"
    try:
        digits = max(0, min(6, int(decimals)))
    except (TypeError, ValueError):
        digits = 2
    # 数值很小时（如 0.001 GB）至少保留 3 位有效小数，避免出现 "0.00 MB"
    if value < 1 and digits < 3:
        digits = 3
    return f"{value:,.{digits}f} {suffix}"


def format_count(number: object) -> str:
    """格式化整数计数（千分位），例如 ``12345 -> "12,345"``。"""
    try:
        return f"{int(number):,}"
    except (TypeError, ValueError):
        return "0"


def format_percent(part: object, whole: object, decimals: int = 1) -> str:
    """计算占比并格式化，分母为 0 时返回 "0.0%"。"""
    try:
        total = float(whole)  # type: ignore[arg-type]
        if total <= 0:
            return "0.0%"
        ratio = float(part) / total * 100.0  # type: ignore[arg-type]
    except (TypeError, ValueError, ZeroDivisionError):
        return "0.0%"
    # 非零但极小的占比，避免显示 0.0% 让用户误以为没有占用
    if 0 < ratio < 10 ** (-decimals):
        return f"<{1 / 10 ** decimals:.{decimals}f}%"
    return f"{ratio:.{decimals}f}%"


def percent_value(part: object, whole: object) -> float:
    """返回 0~1 之间的比值，用于绘制占比条。"""
    try:
        total = float(whole)  # type: ignore[arg-type]
        if total <= 0:
            return 0.0
        return max(0.0, min(1.0, float(part) / total))  # type: ignore[arg-type]
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def format_duration(seconds: object) -> str:
    """把秒数格式化为 ``1小时2分3秒`` / ``45秒`` 这样的可读文本。

    传入 ``None`` 或负数表示“无法估算”，返回 ``--``。
    """
    if seconds is None:
        return "--"
    try:
        total = int(round(float(seconds)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "--"
    if total < 0:
        return "--"
    if total < 60:
        return f"{total} 秒"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分 {secs} 秒"
    return f"{minutes} 分 {secs} 秒"


def format_speed(bytes_per_second: object) -> str:
    """格式化扫描速度，例如 ``1.2 MB/s``。"""
    value = bytes_per_second
    try:
        if value is None or float(value) <= 0:  # type: ignore[arg-type]
            return "--"
    except (TypeError, ValueError):
        return "--"
    return f"{format_size(value)}/s"


def format_rate(count: object, seconds: object) -> str:
    """格式化吞吐率（个/秒），例如目录数、文件数的处理速度。"""
    try:
        elapsed = float(seconds)  # type: ignore[arg-type]
        if elapsed <= 0:
            return "--"
        rate = float(count) / elapsed  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "--"
    if rate >= 1000:
        return f"{rate / 1000:,.1f} 千/秒"
    return f"{rate:,.1f} 个/秒"


def format_timestamp(timestamp: object) -> str:
    """把 Unix 时间戳格式化为 ``2026-01-02 03:04``；无效值返回 ``--``。"""
    try:
        ts = float(timestamp)  # type: ignore[arg-type]
        if ts <= 0:
            return "--"
        return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "--"


def parse_size_text(text: object) -> int:
    """解析用户输入的容量文本（如 ``10MB``、``1.5 GB``）为字节数。

    :return: 字节数；解析失败返回 0
    """
    if text is None:
        return 0
    raw = str(text).strip().upper().replace(",", "")
    if not raw:
        return 0
    suffix = "B"
    for unit in ("KB", "MB", "GB", "TB", "PB", "B", "K", "M", "G", "T"):
        if raw.endswith(unit):
            suffix, raw = unit, raw[: -len(unit)].strip()
            break
    try:
        value = float(raw)
    except ValueError:
        return 0
    table = {"B": 0, "K": 1, "KB": 1, "M": 2, "MB": 2, "G": 3, "GB": 3,
             "T": 4, "TB": 4, "P": 5, "PB": 5}
    return int(value * (_STEP ** table.get(suffix, 0)))

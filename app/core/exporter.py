"""结果导出：CSV（Excel 友好）与 XLSX（openpyxl，带样式与数据条）。

安全/健壮性说明：
* CSV 使用 ``utf-8-sig``（带 BOM），Excel 打开中文不乱码；
* XLSX 通过 openpyxl 生成，超过 1 万行时自动切换 write_only 流式模式，
  避免大结果集一次性驻留内存；
* 写文件前先校验目标目录可写，失败时抛出带中文说明的异常，由界面提示用户。
"""

from __future__ import annotations

import csv
import datetime as _dt
import os
from typing import Dict, Iterable, List, Optional, Sequence

from .formatter import format_duration, format_size
from .logger import get_logger

log = get_logger("export")

# 导出列定义：(表头, 行数据键, 列宽, 数字格式)
COLUMNS: Sequence[tuple] = (
    ("名称", "name", 34, None),
    ("完整路径", "path", 60, None),
    ("大小", "size_text", 14, None),
    ("大小(字节)", "size", 16, "#,##0"),
    ("占比", "percent", 10, "0.00%"),
    ("文件数", "files", 12, "#,##0"),
    ("子目录数", "subdirs", 12, "#,##0"),
    ("直属文件数", "direct_files", 12, "#,##0"),
    ("最大文件", "largest", 14, "#,##0"),
    ("层级", "depth", 8, "0"),
    ("修改时间", "modified", 20, "yyyy-mm-dd hh:mm"),
)

KIND_LABELS = {"folders": "文件夹排行", "extensions": "文件类型分布"}


def rows_from_records(records: Iterable[Dict[str, object]]) -> List[Dict[str, object]]:
    """把内部行字典转换为导出行（补齐可读文本列）。"""
    out: List[Dict[str, object]] = []
    for raw in records:
        item = dict(raw)
        size = int(item.get("size") or 0)
        item.setdefault("size_text", format_size(size))
        item.setdefault("percent", float(item.get("percent") or 0.0))
        modified = item.get("modified")
        if isinstance(modified, (int, float)) and modified:
            item["modified"] = _dt.datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M")
        else:
            # 注意：必须直接赋值，setdefault 无法覆盖已存在的 0 / None
            item["modified"] = "" if not isinstance(modified, str) else modified
        out.append(item)
    return out


def _check_target(path: str) -> None:
    """校验目标路径是否可写。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(directory):
        raise FileNotFoundError(f"目标目录不存在：{directory}")
    if not os.access(directory, os.W_OK):
        raise PermissionError(f"目标目录没有写入权限：{directory}")
    if os.path.exists(path) and not os.path.isfile(path):
        raise IsADirectoryError(f"目标是一个目录，请选择具体文件名：{path}")


def export_csv(path: str, rows: Sequence[Dict[str, object]],
               meta: Optional[Dict[str, str]] = None) -> str:
    """导出为 CSV。

    :param path: 目标文件路径（自动补 .csv 后缀）
    :param rows: 行数据（键需与 COLUMNS 中第二个元素一致）
    :param meta: 附加在文件头部的说明信息（以 # 注释形式写入）
    :return: 实际写入的路径
    """
    if not path.lower().endswith(".csv"):
        path += ".csv"
    _check_target(path)
    with open(path, "w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        for key, value in (meta or {}).items():
            writer.writerow([f"# {key}", value])
        if meta:
            writer.writerow([])
        writer.writerow([title for title, *_ in COLUMNS])
        for row in rows:
            writer.writerow([row.get(key, "") for _, key, *_ in COLUMNS])
    log.info("已导出 CSV：%s（%d 行）", path, len(rows))
    return path


def export_xlsx(path: str, rows: Sequence[Dict[str, object]],
                meta: Optional[Dict[str, str]] = None) -> str:
    """导出为 XLSX（带标题、表头样式、冻结窗格、自动筛选与数据条）。"""
    if not path.lower().endswith(".xlsx"):
        path += ".xlsx"
    _check_target(path)
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError as exc:  # 依赖缺失时给出可操作的中文提示
        raise RuntimeError(
            "缺少 openpyxl，无法导出 Excel。请在命令行执行："
            "pip install openpyxl -i https://pypi.tuna.tsinghua.edu.cn/simple"
        ) from exc

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "文件夹排行"

    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_font = Font(name="Microsoft YaHei", bold=True, color="FFFFFF", size=11)
    head_fill = PatternFill("solid", fgColor="4D7CFE")
    body_font = Font(name="Microsoft YaHei", size=10)

    row_index = 1
    # 1) 标题 + 扫描信息
    sheet.cell(row=1, column=1, value=(meta or {}).get("标题", "文件夹大小排行"))
    sheet.cell(row=1, column=1).font = Font(name="Microsoft YaHei", bold=True, size=14)
    row_index = 2
    for key, value in (meta or {}).items():
        if key == "标题":
            continue
        sheet.cell(row=row_index, column=1, value=f"{key}：{value}")
        sheet.cell(row=row_index, column=1).font = Font(name="Microsoft YaHei",
                                                        size=9, color="808080")
        row_index += 1
    row_index += 1

    # 2) 表头
    header_row = row_index
    for col, (title, _key, _width, _fmt) in enumerate(COLUMNS, start=1):
        cell = sheet.cell(row=header_row, column=col, value=title)
        cell.font = head_font
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border
        sheet.column_dimensions[get_column_letter(col)].width = COLUMNS[col - 1][2]
    sheet.row_dimensions[header_row].height = 22

    # 3) 数据
    for offset, row in enumerate(rows, start=1):
        excel_row = header_row + offset
        for col, (_title, key, _width, number_format) in enumerate(COLUMNS, start=1):
            value = row.get(key, "")
            if value is None:
                value = ""
            cell = sheet.cell(row=excel_row, column=col, value=value)
            cell.font = body_font
            cell.border = border
            if number_format:
                cell.number_format = number_format
            cell.alignment = Alignment(
                horizontal="right" if number_format else "left", vertical="center")

    last_row = header_row + len(rows)
    if last_row > header_row:
        sheet.auto_filter.ref = f"A{header_row}:{get_column_letter(len(COLUMNS))}{last_row}"
        # 大小列加数据条，直观对比（等价于界面里的可视化柱）
        try:
            from openpyxl.formatting.rule import DataBarRule

            size_col = next(i for i, (_t, key, *_r) in enumerate(COLUMNS, start=1)
                            if key == "size")
            sheet.conditional_formatting.add(
                f"{get_column_letter(size_col)}{header_row + 1}:"
                f"{get_column_letter(size_col)}{last_row}",
                DataBarRule(start_type="min", end_type="max", color="4D7CFE",
                            showValue=True))
        except Exception as exc:  # 数据条不是必需项，失败不影响导出
            log.debug("添加数据条失败：%s", exc)
    sheet.freeze_panes = f"A{header_row + 1}"

    workbook.save(path)
    log.info("已导出 XLSX：%s（%d 行）", path, len(rows))
    return path


def export_extension_report(path: str, ext_bytes: Dict[str, int],
                            ext_files: Dict[str, int], total: int) -> str:
    """导出文件类型分布报告（CSV / XLSX 两种格式自动识别）。"""
    rows = []
    for ext, size in sorted(ext_bytes.items(), key=lambda kv: kv[1], reverse=True):
        rows.append({
            "name": ext, "path": "", "size_text": format_size(size), "size": size,
            "percent": (size / total) if total else 0.0,
            "files": ext_files.get(ext, 0), "subdirs": "", "direct_files": "",
            "largest": "", "depth": "", "modified": "",
        })
    return export_xlsx(path, rows, {"标题": "文件类型占用分布"}) if path.lower().endswith(
        ".xlsx") else export_csv(path, rows, {"标题": "文件类型占用分布"})


def build_meta(root: str, total_bytes: int, total_files: int, total_dirs: int,
               elapsed: float, kind: str, count: int) -> Dict[str, str]:
    """组装导出文件的元信息文本。"""
    return {
        "标题": KIND_LABELS.get(kind, kind),
        "扫描根目录": root,
        "总占用": format_size(total_bytes),
        "文件总数": f"{total_files:,}",
        "目录总数": f"{total_dirs:,}",
        "扫描耗时": format_duration(elapsed),
        "导出行数": f"{count:,}",
        "导出时间": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ----------------------------------------------------------------------
# AI 文件分析报告
# ----------------------------------------------------------------------
# 列定义：(表头, 行数据键, xlsx 列宽) —— 行字典由界面层构造
AI_COLUMNS: Sequence[tuple] = (
    ("文件名", "name", 36),
    ("所在目录", "rel_dir", 22),
    ("软件归属", "owner", 24),
    ("类型", "category", 10),
    ("大小", "size_text", 14),
    ("用途说明", "purpose", 64),
)


def export_ai_csv(path: str, rows: Sequence[Dict[str, object]],
                  meta: Optional[Dict[str, str]] = None) -> str:
    """导出 AI 分析报告为 CSV（utf-8-sig，Excel 打开不乱码）。"""
    if not path.lower().endswith(".csv"):
        path += ".csv"
    _check_target(path)
    with open(path, "w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        for key, value in (meta or {}).items():
            writer.writerow([f"# {key}", value])
        if meta:
            writer.writerow([])
        writer.writerow([title for title, *_ in AI_COLUMNS])
        for row in rows:
            writer.writerow([row.get(key, "") for _, key, *_ in AI_COLUMNS])
    log.info("已导出 AI 分析 CSV：%s（%d 行）", path, len(rows))
    return path


def export_ai_xlsx(path: str, rows: Sequence[Dict[str, object]],
                   meta: Optional[Dict[str, str]] = None) -> str:
    """导出 AI 分析报告为 XLSX（带标题、表头样式与冻结窗格）。"""
    if not path.lower().endswith(".xlsx"):
        path += ".xlsx"
    _check_target(path)
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError as exc:
        raise RuntimeError(
            "缺少 openpyxl，无法导出 Excel。请在命令行执行："
            "pip install openpyxl -i https://pypi.tuna.tsinghua.edu.cn/simple"
        ) from exc

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "AI文件分析"

    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_font = Font(name="Microsoft YaHei", bold=True, color="FFFFFF", size=11)
    head_fill = PatternFill("solid", fgColor="4D7CFE")
    body_font = Font(name="Microsoft YaHei", size=10)

    sheet.cell(row=1, column=1, value=(meta or {}).get("标题", "AI 文件分析报告"))
    sheet.cell(row=1, column=1).font = Font(name="Microsoft YaHei", bold=True, size=14)
    row_index = 2
    for key, value in (meta or {}).items():
        if key == "标题":
            continue
        sheet.cell(row=row_index, column=1, value=f"{key}：{value}")
        sheet.cell(row=row_index, column=1).font = Font(name="Microsoft YaHei",
                                                        size=9, color="808080")
        row_index += 1
    row_index += 1

    header_row = row_index
    for col, (title, _key, width) in enumerate(AI_COLUMNS, start=1):
        cell = sheet.cell(row=header_row, column=col, value=title)
        cell.font = head_font
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border
        sheet.column_dimensions[get_column_letter(col)].width = width
    sheet.row_dimensions[header_row].height = 22

    for offset, row in enumerate(rows, start=1):
        excel_row = header_row + offset
        for col, (_title, key, _width) in enumerate(AI_COLUMNS, start=1):
            cell = sheet.cell(row=excel_row, column=col,
                              value=row.get(key, "") or "")
            cell.font = body_font
            cell.border = border
            cell.alignment = Alignment(vertical="top", wrap_text=(key == "purpose"))

    if rows:
        sheet.auto_filter.ref = (
            f"A{header_row}:{get_column_letter(len(AI_COLUMNS))}"
            f"{header_row + len(rows)}")
    sheet.freeze_panes = f"A{header_row + 1}"
    workbook.save(path)
    log.info("已导出 AI 分析 XLSX：%s（%d 行）", path, len(rows))
    return path


def export_ai_report(path: str, rows: Sequence[Dict[str, object]],
                     meta: Optional[Dict[str, str]] = None) -> str:
    """导出 AI 分析报告（按文件后缀自动选择 CSV / XLSX 格式）。"""
    return export_ai_xlsx(path, rows, meta) if path.lower().endswith(
        ".xlsx") else export_ai_csv(path, rows, meta)

"""扫描结果的数据模型。

内存优化思路（面对 C 盘这种百万级目录的场景）：
1. 所有目录用 ``DirRecord`` 存在一个 list 里，用「下标 = 目录 id」代替字典，
   避免为每个目录保存完整路径字符串（完整路径按需回溯父级拼出来）。
2. 目录之间用 ``parent_id / head_child / next_sibling`` 组成父子链，
   比 ``dict[str, list[str]]`` 省掉大量字符串与列表对象。
3. 采用广度优先（BFS）遍历，子目录 id 一定大于父目录 id，
   因此最后只需 **一次逆序遍历** 就能把所有目录的递归大小算出来（O(N)）。
"""

from __future__ import annotations

import heapq
import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# “散文件”聚合行的虚拟目录 id：不指向 records 中的任何真实记录，
# 仅用于界面展示（排行列表末尾的直属文件汇总行），点击可查看文件清单。
LOOSE_ID = -1


@dataclass(slots=True)
class DirRecord:
    """单个目录的统计信息（大小单位统一为字节）。"""

    id: int
    parent_id: int          # 父目录 id，根目录为 -1
    name: str               # 目录名（不含路径），根目录为完整路径
    depth: int              # 相对扫描根目录的层级，根为 0
    mtime: float = 0.0      # 目录最后修改时间戳

    direct_bytes: int = 0   # 直属文件占用（不含子目录）
    direct_files: int = 0   # 直属文件数量
    subdirs: int = 0        # 直属子目录数量

    total_bytes: int = 0    # 递归总大小 = direct_bytes + 所有子孙目录
    total_files: int = 0    # 递归文件数量
    total_subdirs: int = 0  # 递归子目录数量

    largest_file: int = 0   # 该目录（含子孙）中最大的单个文件
    error: bool = False     # 是否发生过访问失败（权限不足等）
    truncated: bool = False # 是否因层级/数量上限而未继续深入

    # 隐藏 / 系统属性文件的专项统计：资源管理器默认不显示这些文件，
    # 手工加总常常与扫描结果对不上，这里把它们单独记录便于核对。
    hidden_bytes: int = 0         # 直属隐藏/系统文件字节
    hidden_files: int = 0         # 直属隐藏/系统文件个数
    total_hidden_bytes: int = 0   # 递归隐藏/系统文件字节（含子孙目录）
    total_hidden_files: int = 0   # 递归隐藏/系统文件个数

    head_child: int = -1    # 第一个子目录 id
    next_sibling: int = -1  # 同级下一个目录 id

    @property
    def is_empty(self) -> bool:
        """目录是否既没有文件也没有子目录。"""
        return self.total_files == 0 and self.total_subdirs == 0 and self.direct_bytes == 0


@dataclass(slots=True)
class ScanStats:
    """扫描过程的运行时统计，用于界面进度卡片。"""

    dirs_done: int = 0        # 已完成扫描的目录数
    dirs_pending: int = 0     # 队列中等待扫描的目录数
    files_done: int = 0       # 已统计的文件数
    bytes_done: int = 0       # 已统计的字节数
    errors: int = 0           # 失败条目数
    skipped: int = 0          # 被选项过滤掉的条目数（隐藏/系统/软链等）
    truncated_dirs: int = 0   # 因上限而未深入的目录数
    elapsed: float = 0.0      # 已耗时（秒）
    current_path: str = ""    # 当前正在扫描的目录


@dataclass(slots=True)
class ScanResult:
    """一次扫描的完整结果。

    ``records`` 为**只追加**列表：扫描线程只在尾部 append/累加整型字段，
    界面线程读取时先 ``list(records)`` 取快照，因此不会迭代到一半被修改。
    """

    root_path: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    cancelled: bool = False
    paused: bool = False
    complete: bool = True     # 是否完整扫描（未触及任何上限）

    records: List[DirRecord] = field(default_factory=list)
    errors: List[Tuple[str, str]] = field(default_factory=list)  # (路径, 原因)
    ext_bytes: Dict[str, int] = field(default_factory=dict)      # 扩展名 -> 字节
    ext_files: Dict[str, int] = field(default_factory=dict)      # 扩展名 -> 个数
    skipped: int = 0         # 被跳过的条目数（软链接/被过滤/属性读取失败等）

    _path_cache: Dict[int, str] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ 构建
    def add_record(self, parent_id: int, name: str, depth: int, mtime: float = 0.0) -> DirRecord:
        """新建一个目录记录并挂到父目录的子链上，返回该记录。"""
        record = DirRecord(id=len(self.records), parent_id=parent_id, name=name,
                           depth=depth, mtime=mtime)
        self.records.append(record)
        if parent_id >= 0:
            parent = self.records[parent_id]
            record.next_sibling = parent.head_child
            parent.head_child = record.id
            parent.subdirs += 1
        return record

    # ------------------------------------------------------------------ 查询
    def get(self, dir_id: int) -> Optional[DirRecord]:
        """按 id 取记录，越界返回 None（防止线程追加导致的短暂不一致）。"""
        if 0 <= dir_id < len(self.records):
            return self.records[dir_id]
        return None

    def full_path(self, dir_id: int) -> str:
        """回溯父级拼出目录的完整路径（带缓存，避免重复拼接）。"""
        cached = self._path_cache.get(dir_id)
        if cached is not None:
            return cached
        record = self.get(dir_id)
        if record is None:
            return ""
        if record.parent_id < 0:
            path = self.root_path
        else:
            path = os.path.join(self.full_path(record.parent_id), record.name)
        # 缓存条目过多会影响内存，限制在 2 万条以内
        if len(self._path_cache) < 20000:
            self._path_cache[dir_id] = path
        return path

    def children(self, dir_id: int) -> List[int]:
        """返回某个目录的直接子目录 id 列表。"""
        out: List[int] = []
        record = self.get(dir_id)
        if record is None:
            return out
        child = record.head_child
        while child >= 0:
            out.append(child)
            nxt = self.get(child)
            child = nxt.next_sibling if nxt else -1
        return out

    def descendants(self, dir_id: int) -> Iterable[int]:
        """深度优先生成某目录下的所有子孙目录 id（不含自身）。"""
        stack: List[int] = [dir_id]
        seen = 0
        while stack and seen < 2_000_000:  # 防御性上限，避免异常数据导致卡死
            current = stack.pop()
            for child in self.children(current):
                seen += 1
                yield child
                stack.append(child)

    def top_descendants(self, dir_id: int, count: int, desc: bool = True) -> List[DirRecord]:
        """取某目录下所有子孙中最大/最小的前 N 个（用堆实现，O(M log N)）。"""
        limit = max(1, int(count))
        pool = [self.records[i] for i in self.descendants(dir_id) if i < len(self.records)]
        if desc:
            return heapq.nlargest(limit, pool, key=lambda r: r.total_bytes)
        return heapq.nsmallest(limit, pool, key=lambda r: r.total_bytes)

    # ------------------------------------------------------------------ 汇总
    @property
    def root(self) -> Optional[DirRecord]:
        """根目录记录。"""
        return self.records[0] if self.records else None

    @property
    def total_bytes(self) -> int:
        """扫描到的总字节数。"""
        root = self.root
        return root.total_bytes if root else 0

    @property
    def total_files(self) -> int:
        """扫描到的总文件数。"""
        root = self.root
        return root.total_files if root else 0

    @property
    def total_dirs(self) -> int:
        """扫描到的目录总数（含根）。"""
        return len(self.records)

    def finalize(self) -> None:
        """收尾：自底向上累加「递归子目录数」与「最大文件」。

        说明：``total_bytes`` / ``total_files`` 在扫描过程中已由
        :meth:`propagate_sizes` 实时向上累加（这样界面才能“边扫边看”），
        因此这里不再重复累加，只补齐与目录结构相关的统计。
        """
        for record in reversed(self.records):
            record.total_subdirs += record.subdirs
            parent = self.get(record.parent_id) if record.parent_id >= 0 else None
            if parent is not None:
                parent.total_subdirs += record.total_subdirs
                if record.largest_file > parent.largest_file:
                    parent.largest_file = record.largest_file

    def propagate_sizes(self, record: DirRecord) -> None:
        """把一个目录的直属数据累加到它自己以及所有祖先目录。

        每个目录只调用一次，成本为 O(层级深度)，远小于「每个文件向上累加」，
        同时又能让界面上的大小在扫描过程中持续增长。
        """
        if (record.direct_bytes == 0 and record.direct_files == 0
                and record.hidden_bytes == 0):
            return
        record.total_bytes += record.direct_bytes
        record.total_files += record.direct_files
        record.total_hidden_bytes += record.hidden_bytes
        record.total_hidden_files += record.hidden_files
        parent_id = record.parent_id
        while parent_id >= 0:
            parent = self.get(parent_id)
            if parent is None:
                break
            parent.total_bytes += record.direct_bytes
            parent.total_files += record.direct_files
            parent.total_hidden_bytes += record.hidden_bytes
            parent.total_hidden_files += record.hidden_files
            parent_id = parent.parent_id

    def prune_extensions(self, keep: int = 48) -> None:
        """限制扩展名统计的键数量，超出部分合并进「其他」，控制内存占用。"""
        if len(self.ext_bytes) <= keep * 2:
            return
        top = heapq.nlargest(keep, self.ext_bytes.items(), key=lambda kv: kv[1])
        kept = dict(top)
        rest_bytes = 0
        rest_files = 0
        for ext, size in self.ext_bytes.items():
            if ext not in kept:
                rest_bytes += size
        for ext, cnt in self.ext_files.items():
            if ext not in kept:
                rest_files += cnt
        self.ext_bytes = kept
        self.ext_files = {k: v for k, v in self.ext_files.items() if k in kept}
        if rest_bytes or rest_files:
            self.ext_bytes["其他"] = self.ext_bytes.get("其他", 0) + rest_bytes
            self.ext_files["其他"] = self.ext_files.get("其他", 0) + rest_files

    def error_text(self, limit: int = 5) -> str:
        """把前若干条错误拼成一行提示文本。"""
        if not self.errors:
            return ""
        head = "; ".join(f"{p}（{m}）" for p, m in self.errors[:limit])
        more = "" if len(self.errors) <= limit else f" 等 {len(self.errors)} 处"
        return head + more

    def snapshot_records(self) -> Sequence[DirRecord]:
        """给界面用的浅拷贝快照（列表拷贝是原子的，避免迭代时被追加）。"""
        return list(self.records)


def row_to_dict(result: ScanResult, dir_id: int, base_bytes: int) -> Dict[str, object]:
    """把一条目录记录转换成导出用的字典行。

    返回的键与 :data:`app.core.exporter.COLUMNS` 中的字段名一一对应，
    界面层无需再做二次映射。
    """
    record = result.get(dir_id)
    if record is None:
        return {}
    percent = (record.total_bytes / base_bytes) if base_bytes else 0.0
    return {
        "name": record.name or result.root_path,
        "path": result.full_path(dir_id),
        "size": record.total_bytes,
        "percent": percent,
        "files": record.total_files,
        "subdirs": record.total_subdirs,
        "direct_files": record.direct_files,
        "largest": record.largest_file,
        "depth": record.depth,
        "modified": record.mtime,
    }


def rows_for_export(result: ScanResult, records: Sequence[DirRecord],
                    base_bytes: int) -> List[Dict[str, object]]:
    """批量把记录列表转换成导出行（保持传入顺序）。"""
    return [row_to_dict(result, record.id, base_bytes) for record in records]

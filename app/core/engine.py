"""文件夹扫描引擎（纯 Python，不依赖 Qt，便于测试与复用）。

核心算法
--------
广度优先（BFS）遍历 + 一次逆序累加：

1. 从根目录开始，用 ``os.scandir`` 逐个目录展开。``DirEntry`` 在 Windows 上
   会把名字、类型、大小、属性一次性缓存下来，比 ``os.listdir`` + ``os.stat``
   少一次系统调用，是 Python 里最快的遍历方式。
2. 遍历过程中**只记录直属数据**（direct_bytes / direct_files / subdirs），
   不做任何递归函数调用 —— 避免深层目录爆栈，也避免重复遍历。
3. BFS 保证子目录的 id 恒大于父目录，扫描结束后逆序遍历一次记录表，
   把每个目录的数据加到父目录上，即得到全部目录的递归大小（整体 O(N)）。

可控性
------
* ``cancel_event``：停止扫描（已完成部分的数据仍然保留可用）。
* ``pause_event``：暂停/继续（对应需求中的“中断与恢复”）。
* ``max_depth`` / ``max_records``：层级与记录数上限，防止扫全盘时内存失控。
* 长路径（>260 字符）自动加 ``\\\\?\\`` 前缀访问，避免 WinError 3。
"""

from __future__ import annotations

import os
import stat as _stat
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, List, Optional, Tuple

from .formatter import format_size
from .logger import get_logger
from .models import DirRecord, ScanResult, ScanStats

log = get_logger("engine")

# Windows 文件属性常量（Linux/macOS 上不存在，读取时用 getattr 保护）
FILE_ATTRIBUTE_HIDDEN = getattr(_stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)
FILE_ATTRIBUTE_SYSTEM = getattr(_stat, "FILE_ATTRIBUTE_SYSTEM", 0x4)
FILE_ATTRIBUTE_REPARSE_POINT = getattr(_stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

# 勾选“排除系统保留目录”时忽略这些名字（不区分大小写）
RESERVED_DIRS = frozenset({
    "$recycle.bin", "system volume information", "windowsapps",
    "recovery", "perflogs", "sysvol", "$extend", "config.msi",
})

_IS_WINDOWS = sys.platform.startswith("win")

# 每处理这么多条目检查一次暂停/取消标志并更新进度（数值越大越快）
_FLAG_CHECK_INTERVAL = 128


@dataclass(slots=True)
class ScanOptions:
    """一次扫描的参数集合。"""

    root: str
    follow_symlinks: bool = False   # 是否进入软链接 / 联接点
    skip_hidden: bool = False       # 跳过隐藏属性目录
    skip_system: bool = False       # 跳过系统属性目录
    skip_reserved: bool = False     # 跳过系统保留目录
    max_depth: int = 0              # 0 = 不限制
    max_records: int = 400000       # 目录记录上限（内存保护）
    progress_interval: float = 0.15 # 进度回调最小间隔（秒）


@dataclass(slots=True)
class ScanProgress:
    """回调给界面的进度快照。"""

    percent: float                  # 0 ~ 99 的平滑进度
    stats: ScanStats
    bytes_per_sec: float = 0.0
    items_per_sec: float = 0.0
    eta_seconds: Optional[float] = None
    elapsed: float = 0.0
    paused: bool = False
    dirs_total_estimated: int = 0


@dataclass
class FolderScanner:
    """文件夹大小扫描器。

    用法::

        scanner = FolderScanner(ScanOptions(root="D:/tmp"))
        result = scanner.run()          # 同步阻塞
        # 或在 Qt 线程中调用，配合 cancel_event / pause_event / on_progress
    """

    options: ScanOptions
    on_progress: Optional[Callable[[ScanProgress], None]] = None
    cancel_event: Optional[threading.Event] = None
    pause_event: Optional[threading.Event] = None
    # 扫描过程中产生的部分结果，界面可读取以实现“边扫边看”
    result: ScanResult = field(default_factory=lambda: ScanResult(root_path=""))

    # ------------------------------------------------------------ 生命周期
    def __post_init__(self) -> None:
        if self.cancel_event is None:
            self.cancel_event = threading.Event()
        if self.pause_event is None:
            self.pause_event = threading.Event()
        self._smoothed = 0.0
        self._last_emit = 0.0
        self._started = 0.0
        self._timed_out = False

    @property
    def is_paused(self) -> bool:
        """当前是否处于暂停状态。"""
        return bool(self.pause_event.is_set())

    @property
    def is_cancelled(self) -> bool:
        """是否已被请求停止。"""
        return bool(self.cancel_event.is_set())

    def request_pause(self) -> None:
        """请求暂停。"""
        self.pause_event.set()

    def request_resume(self) -> None:
        """请求继续。"""
        self.pause_event.clear()

    def request_cancel(self) -> None:
        """请求停止（保留已扫描到的数据）。"""
        self.cancel_event.set()
        self.pause_event.clear()  # 若正暂停，唤醒循环以便尽快退出

    # ------------------------------------------------------------ 主流程
    def run(self) -> ScanResult:
        """执行扫描并返回结果（阻塞直到完成 / 停止）。"""
        root = _normalize_root(self.options.root)
        result = self.result = ScanResult(root_path=root)
        result.started_at = time.time()
        self._started = time.monotonic()

        root_name = os.path.basename(root.rstrip("\\/")) or root
        root_record = result.add_record(-1, root_name, 0, _safe_mtime(root))

        queue: Deque[int] = deque([root_record.id])
        stats = ScanStats(dirs_pending=1, current_path=root)
        options = self.options
        processed = 0
        entries_seen = 0

        while queue:
            # ---- 协作式控制：暂停 / 停止 ----
            if self._handle_flags(stats, queue):
                break

            dir_id = queue.popleft()
            stats.dirs_pending = len(queue)
            record = result.get(dir_id)
            if record is None:  # 理论上不会发生，防御性判断
                continue
            record.truncated = bool(options.max_depth and record.depth >= options.max_depth)
            if record.truncated:
                stats.truncated_dirs += 1
                continue

            stats.current_path = result.full_path(dir_id)
            entries, err = self._scan_one_dir(result, record, queue, stats)
            processed += 1
            entries_seen += entries
            stats.dirs_done += 1
            # 把该目录的直属数据实时累加到祖先，界面可以“边扫边看”
            result.propagate_sizes(record)
            if err:
                record.error = True
                stats.errors += 1
                if len(result.errors) < 300:  # 错误列表限量保存，避免内存膨胀
                    result.errors.append((stats.current_path, err))

            # 每处理若干目录回调一次进度
            if entries_seen >= 2000 or processed % 200 == 0:
                self._emit(stats, queue, force=False)
                entries_seen = 0

        # 收尾：计算递归大小、合并扩展名统计
        self._wait_for_resume(stats)
        result.finalize()
        result.prune_extensions()
        result.skipped = stats.skipped
        result.cancelled = self.is_cancelled
        result.complete = (not self.is_cancelled) and stats.truncated_dirs == 0
        result.finished_at = time.time()
        stats.elapsed = time.monotonic() - self._started
        stats.dirs_pending = len(queue)
        self._emit(stats, queue, force=True, final=True)
        log.info("扫描结束：%s 目录 / %s 文件 / %s，用时 %.1fs，取消=%s",
                 len(result.records), result.root.total_files if result.root else 0,
                 format_size(result.total_bytes), stats.elapsed, result.cancelled)
        return result

    # ------------------------------------------------------------ 单目录处理
    def _scan_one_dir(self, result: ScanResult, record: DirRecord,
                      queue: Deque[int], stats: ScanStats) -> Tuple[int, str]:
        """扫描一个目录的直属内容。

        :return: (条目数, 错误信息；成功时为空字符串)
        """
        path = result.full_path(record.id)
        entries = 0
        options = self.options
        max_depth = options.max_depth
        child_depth = record.depth + 1
        try:
            with os.scandir(_api_path(path)) as iterator:
                for entry in iterator:
                    entries += 1
                    if entries % _FLAG_CHECK_INTERVAL == 0 and self._handle_flags(stats, queue):
                        return entries, ""
                    name = entry.name
                    try:
                        attributes = self._attributes(entry)
                    except OSError as exc:
                        stats.skipped += 1
                        log.debug("属性读取失败 %s：%s", name, exc)
                        continue
                    is_reparse = bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False

                    if is_dir:
                        if is_reparse and not options.follow_symlinks:
                            stats.skipped += 1      # 软链/联接点，避免重复统计与死循环
                            continue
                        if options.skip_hidden and attributes & FILE_ATTRIBUTE_HIDDEN:
                            stats.skipped += 1
                            continue
                        if options.skip_system and attributes & FILE_ATTRIBUTE_SYSTEM:
                            stats.skipped += 1
                            continue
                        if options.skip_reserved and name.lower() in RESERVED_DIRS:
                            stats.skipped += 1
                            continue
                        if max_depth and child_depth > max_depth:
                            stats.truncated_dirs += 1
                            record.truncated = True
                            continue
                        if len(result.records) >= options.max_records:
                            # 内存保护：不再记录新目录，同时标记结果不完整
                            stats.truncated_dirs += 1
                            record.truncated = True
                            self._timed_out = True
                            continue
                        child = result.add_record(record.id, name, child_depth)
                        queue.append(child.id)
                        continue

                    # ---- 普通文件 ----
                    if is_reparse and not options.follow_symlinks:
                        stats.skipped += 1
                        continue
                    if options.skip_hidden and attributes & FILE_ATTRIBUTE_HIDDEN:
                        stats.skipped += 1
                        continue
                    try:
                        size = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        size = 0
                    if size < 0:
                        size = 0
                    record.direct_bytes += size
                    record.direct_files += 1
                    # 说明：os.scandir 枚举的是文件系统真实条目，隐藏/系统文件
                    # 一样默认会被统计（与资源管理器“显示隐藏文件”开关无关）。
                    # 这里单独记一笔，方便用户核对“手工加总为何偏小”。
                    if attributes & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM):
                        record.hidden_bytes += size
                        record.hidden_files += 1
                    if size > record.largest_file:
                        record.largest_file = size
                    stats.files_done += 1
                    stats.bytes_done += size
                    if size:
                        ext = self._extension(name)
                        result.ext_bytes[ext] = result.ext_bytes.get(ext, 0) + size
                        result.ext_files[ext] = result.ext_files.get(ext, 0) + 1
        except PermissionError as exc:
            return entries, "拒绝访问"
        except FileNotFoundError:
            return entries, "目录不存在或已被删除"
        except NotADirectoryError:
            return entries, "不是目录"
        except OSError as exc:
            return entries, f"{type(exc).__name__}: {exc.strerror or exc}"
        return entries, ""

    # ------------------------------------------------------------ 工具方法
    @staticmethod
    def _attributes(entry: os.DirEntry) -> int:
        """读取文件属性（Windows 上从 DirEntry 缓存取，几乎零开销）。"""
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            return 0
        return int(getattr(info, "st_file_attributes", 0))

    @staticmethod
    def _extension(name: str) -> str:
        """提取并规范化扩展名（无扩展名归入 “(无扩展名)”）。"""
        dot = name.rfind(".")
        if dot <= 0 or dot == len(name) - 1:
            return "(无扩展名)"
        ext = name[dot + 1:].lower()
        if len(ext) > 8 or any(ch in ext for ch in "\\/:*?\"<>|"):
            return "(其他)"
        return f".{ext}"

    def _handle_flags(self, stats: ScanStats, queue: Deque[int]) -> bool:
        """检查停止/暂停标志。

        :return: True 表示应立即终止当前扫描
        """
        if self.cancel_event.is_set():
            return True
        if self.pause_event.is_set():
            self._wait_for_resume(stats)
        return self.cancel_event.is_set()

    def _wait_for_resume(self, stats: ScanStats) -> None:
        """阻塞等待恢复（期间仍响应停止请求）。"""
        if not self.pause_event.is_set():
            return
        self._emit(stats, None, force=True, paused=True)
        while self.pause_event.is_set() and not self.cancel_event.is_set():
            time.sleep(0.05)
        self._emit(stats, None, force=True)

    def _emit(self, stats: ScanStats, queue: Optional[Deque[int]],
              force: bool = False, final: bool = False, paused: bool = False) -> None:
        """计算进度并回调界面（带节流，避免高频信号拖慢扫描）。"""
        now = time.monotonic()
        if not force and not paused and now - self._last_emit < self.options.progress_interval:
            return
        self._last_emit = now
        stats.elapsed = now - self._started
        percent = self._estimate(queue, stats, final)
        if self.on_progress is not None:
            names = list(ScanStats.__dataclass_fields__)
            snapshot = ScanStats(**{k: getattr(stats, k) for k in names})
            self.on_progress(ScanProgress(
                percent=percent,
                stats=snapshot,
                bytes_per_sec=snapshot.bytes_done / snapshot.elapsed if snapshot.elapsed > 0 else 0.0,
                items_per_sec=(snapshot.files_done + snapshot.dirs_done) / snapshot.elapsed
                if snapshot.elapsed > 0 else 0.0,
                eta_seconds=self._eta(percent, snapshot),
                elapsed=snapshot.elapsed,
                paused=paused or self.pause_event.is_set(),
                dirs_total_estimated=snapshot.dirs_done + snapshot.dirs_pending,
            ))

    def _estimate(self, queue: Optional[Deque[int]], stats: ScanStats, final: bool) -> float:
        """进度估算：已完成“工作量” / (已完成 + 预估剩余)。

        目录开销按 6 个文件当量折算；剩余量按 “待扫目录数 × 平均单目录条目数” 估算。
        结果做单调不减 + EMA 平滑，保证进度条不会来回跳动。
        """
        if final:
            return 100.0
        done_units = stats.files_done + stats.dirs_done * 6.0
        pending_dirs = len(queue) if queue is not None else stats.dirs_pending
        if pending_dirs <= 0:
            raw = 92.0  # 队列暂时空但仍在收尾
        else:
            avg_units = done_units / max(1, stats.dirs_done)
            pending_units = pending_dirs * max(avg_units * 0.75, 3.0)
            if done_units + pending_units <= 0:
                raw = 0.0
            else:
                raw = done_units / (done_units + pending_units) * 100.0
        smoothed = self._smoothed + (raw - self._smoothed) * 0.35
        # 进度条只增不减（新发现的目录会让原始估算值下降，这里做单调化处理）
        self._smoothed = min(max(smoothed, self._smoothed), 99.0)
        return round(self._smoothed, 1)

    @staticmethod
    def _eta(percent: float, stats: ScanStats) -> Optional[float]:
        """按当前吞吐率估算剩余秒数，样本太少时返回 None（显示 --）。"""
        if percent < 1.0 or stats.elapsed < 1.0:
            return None
        total = stats.elapsed / (percent / 100.0)
        return max(0.0, total - stats.elapsed)


# ------------------------------------------------------------------ 路径工具
def _normalize_root(raw: str) -> str:
    """规范化根目录路径：去掉多余引号与结尾分隔符，补全盘符斜杠。"""
    path = (raw or "").strip().strip('"').strip()
    if not path:
        raise ValueError("扫描路径不能为空")
    path = os.path.abspath(path)
    if _IS_WINDOWS and len(path) <= 3 and path[1:3] == ":\\":
        return path  # 形如 "C:\\"
    return path.rstrip("\\/") or path


def _api_path(path: str) -> str:
    """转换为可访问系统调用的路径（Windows 超长路径自动加 \\\\?\\ 前缀）。"""
    if not _IS_WINDOWS or path.startswith("\\\\?\\"):
        return path
    if len(path) >= 250:
        # \\?\ 要求绝对路径、反斜杠分隔、不含 "." / ".."
        normalized = os.path.abspath(path).replace("/", "\\")
        if normalized[1:3] == ":\\" and len(normalized) > 3:
            return "\\\\?\\" + normalized
        if normalized[1:3] == ":\\":
            return "\\\\?\\" + normalized
    return path


def _safe_mtime(path: str) -> float:
    """安全获取目录修改时间，失败返回 0。"""
    try:
        return os.stat(_api_path(path)).st_mtime
    except OSError:
        return 0.0


def quick_estimate(path: str, limit: int = 20000) -> Tuple[int, int]:
    """对目录做一次“抽样式”快速估算（用于路径选择时的提示）。

    :param limit: 最多遍历的条目数
    :return: (字节数, 条目数)，达到 limit 时返回部分结果
    """
    total = 0
    count = 0
    stack = [path]
    while stack and count < limit:
        current = stack.pop()
        try:
            with os.scandir(_api_path(current)) as it:
                for entry in it:
                    count += 1
                    if count >= limit:
                        break
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not entry.is_symlink():
                                stack.append(entry.path)
                        else:
                            total += max(0, entry.stat(follow_symlinks=False).st_size)
                    except OSError:
                        continue
        except OSError:
            continue
    return total, count

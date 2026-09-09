"""扫描线程：把纯 Python 的 FolderScanner 包装成 Qt 后台任务。

要点：
* 使用 ``QThread`` 而非 ``ThreadPoolExecutor``，因为扫描需要“暂停/继续/停止”
  这类协作式控制，线程 + Event 的模型最直观；
* 所有跨线程通信通过信号（Qt 信号是线程安全的队列连接），
  界面线程绝不直接读写扫描线程的内部状态；
* ``result.records`` 是只追加列表，界面用 ``snapshot_records()`` 取快照后即可安全读取。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QThread, Signal

from ..core.engine import FolderScanner, ScanOptions, ScanProgress
from ..core.logger import get_logger
from ..core.models import ScanResult

log = get_logger("thread")


class ScanThread(QThread):
    """后台扫描线程。"""

    progress = Signal(object)      # ScanProgress
    partial_ready = Signal(int)    # 已记录目录数，通知界面可以刷新局部视图
    completed = Signal(object)     # ScanResult（正常结束或被停止）
    failed = Signal(str)           # 致命错误文本

    def __init__(self, options: ScanOptions, parent=None) -> None:
        super().__init__(parent)
        self._options = options
        self._scanner = FolderScanner(options, on_progress=self._on_progress)
        self._last_partial_emit = 0
        # 线程结束时间戳，供界面统计耗时
        self.result: Optional[ScanResult] = None

    # ------------------------------------------------------------ 控制接口
    @property
    def scanner(self) -> FolderScanner:
        """内部扫描器（用于暂停/继续/停止）。"""
        return self._scanner

    def pause(self) -> None:
        """暂停扫描（数据保留）。"""
        self._scanner.request_pause()

    def resume(self) -> None:
        """继续扫描。"""
        self._scanner.request_resume()

    def stop(self) -> None:
        """停止扫描并保留已有结果。"""
        self._scanner.request_cancel()

    def is_paused(self) -> bool:
        """是否处于暂停状态。"""
        return self._scanner.is_paused

    # ------------------------------------------------------------ 运行
    def run(self) -> None:  # pragma: no cover - 需要真实线程环境
        """线程入口：执行扫描并通过信号回传结果。"""
        try:
            result = self._scanner.run()
            self.result = result
            self.completed.emit(result)
        except ValueError as exc:  # 路径非法等用户可直接修复的问题
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - 兜底，保证线程不会静默死亡
            log.exception("扫描线程异常")
            self.failed.emit(f"扫描失败：{type(exc).__name__}: {exc}")

    def _on_progress(self, progress: ScanProgress) -> None:
        """扫描线程回调 -> 转成 Qt 信号（自动排队到界面线程）。"""
        self.progress.emit(progress)
        total = progress.stats.dirs_done
        if total - self._last_partial_emit >= 200:
            self._last_partial_emit = total
            self.partial_ready.emit(total)

    def wait_safely(self, timeout_ms: int = 3000) -> bool:
        """等待线程退出；超时不阻塞界面（返回 False 由界面提示）。"""
        if not self.isRunning():
            return True
        finished = self.wait(timeout_ms)
        if not finished:
            log.warning("扫描线程未在 %d ms 内退出", timeout_ms)
        return finished

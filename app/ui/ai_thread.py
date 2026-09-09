"""AI 分析后台线程：把阻塞式的 API 调用包装成 Qt 异步任务。

线程模型与 :class:`ScanThread` 一致：
* 网络请求全部在工作线程执行，界面线程只通过信号接收结果；
* ``cancel()`` 是协作式停止——当前一批请求完成后即退出，不强行中断
  已经发出的 HTTP 请求（避免半途连接悬挂）；
* 每分析完一批文件就发一次信号，界面可以边分析边显示，无需等全部完成。
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from PySide6.QtCore import QThread, Signal

from ..core.ai_client import (AiAnalysisError, AiClient, FileEntry,
                              FileInsight, build_messages, collect_files,
                              parse_insights)
from ..core.logger import get_logger

log = get_logger("ai_thread")


class CollectFilesThread(QThread):
    """后台递归收集文件清单线程。

    D 盘这类大型目录可能有几十万上百万个文件，同步在界面线程
    ``os.walk`` 会卡死窗口；收集逻辑挪到后台线程，界面先显示
    “正在收集文件清单…”提示，收完再发信号刷新状态。
    """

    collected = Signal(list)      # List[FileEntry]
    failed = Signal(str)          # 致命错误文本

    def __init__(self, directory: str, max_files: int = 200,
                 per_dir_limit: int = 50, parent=None) -> None:
        """
        :param directory: 目标目录
        :param max_files: 全局最多收集的文件数（与 AI 设置一致）
        :param per_dir_limit: 每个子目录最多取多少个文件（大文件优先）
        """
        super().__init__(parent)
        self._directory = directory
        self._max_files = max(1, int(max_files))
        self._per_dir_limit = max(1, int(per_dir_limit))

    def run(self) -> None:  # pragma: no cover - 需要真实线程环境
        """线程入口：递归收集文件并通过信号回传。"""
        try:
            entries = collect_files(self._directory, self._max_files,
                                    self._per_dir_limit)
            self.collected.emit(entries)
            log.info("文件清单收集完成：%d 个（%s）", len(entries), self._directory)
        except AiAnalysisError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - 兜底，保证线程不静默死亡
            log.exception("收集文件清单异常")
            self.failed.emit(f"收集文件清单失败：{type(exc).__name__}: {exc}")

    def wait_safely(self, timeout_ms: int = 5000) -> bool:
        """等待线程退出；超时不阻塞界面。"""
        if not self.isRunning():
            return True
        finished = self.wait(timeout_ms)
        if not finished:
            log.warning("收集文件清单线程未在 %d ms 内退出", timeout_ms)
        return finished


class AiAnalysisThread(QThread):
    """后台 AI 分析线程（分批请求，逐批回报）。"""

    batch_done = Signal(int, int)      # (已完成文件数, 总文件数)
    insights_ready = Signal(list)      # List[FileInsight]：本批结果
    failed = Signal(str)               # 致命错误文本
    finished_all = Signal()            # 全部批次结束（含被取消的情况）

    def __init__(self, client: AiClient, entries: Sequence[FileEntry],
                 directory: str, batch_size: int = 30, parent=None) -> None:
        """
        :param client: 已配置好的 API 客户端
        :param entries: 待分析文件清单（来自 :func:`collect_files`）
        :param directory: 文件所在目录（作为提示词上下文）
        :param batch_size: 每次请求携带的文件数
        """
        super().__init__(parent)
        self._client = client
        self._entries: List[FileEntry] = list(entries)
        self._directory = directory
        self._batch_size = max(1, int(batch_size))
        self._cancel = False

    # ------------------------------------------------------------ 控制接口
    def cancel(self) -> None:
        """请求停止：当前批次完成后不再发起下一批。"""
        self._cancel = True

    @property
    def total_files(self) -> int:
        """待分析文件总数。"""
        return len(self._entries)

    # ------------------------------------------------------------ 运行
    def run(self) -> None:  # pragma: no cover - 需要真实线程环境
        """线程入口：逐批请求 -> 解析 -> 发信号。"""
        total = len(self._entries)
        log.info("AI 分析开始：%d 个文件，每批 %d 个", total, self._batch_size)
        try:
            done = 0
            for start in range(0, total, self._batch_size):
                if self._cancel:
                    break
                chunk = self._entries[start:start + self._batch_size]
                messages = build_messages(chunk, self._directory)
                content = self._client.chat(messages)
                if self._cancel:        # 请求期间被取消：丢弃本批结果
                    break
                insights = parse_insights(content, chunk)
                self.insights_ready.emit(insights)
                done = min(start + len(chunk), total)
                self.batch_done.emit(done, total)
            self.finished_all.emit()
            log.info("AI 分析结束（%s），已完成 %d/%d",
                     "已取消" if self._cancel else "正常", done, total)
        except AiAnalysisError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - 兜底，保证线程不静默死亡
            log.exception("AI 分析线程异常")
            self.failed.emit(f"AI 分析失败：{type(exc).__name__}: {exc}")

    def wait_safely(self, timeout_ms: int = 5000) -> bool:
        """等待线程退出；超时不阻塞界面。"""
        if not self.isRunning():
            return True
        finished = self.wait(timeout_ms)
        if not finished:
            log.warning("AI 分析线程未在 %d ms 内退出", timeout_ms)
        return finished

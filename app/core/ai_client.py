"""AI 文件分析核心层：OpenAI 兼容 Chat Completions 客户端 + 文件清单收集。

设计要点：
* 仅使用标准库 ``urllib``，不引入新的第三方依赖；
* "收集文件 → 构造提示词 → 请求接口 → 解析 JSON" 四步都是纯函数，
  便于单元测试（网络层用本地模拟服务器验证）；
* 所有失败都以 :class:`AiAnalysisError` 抛出（带中文说明），由界面统一提示。
"""

from __future__ import annotations

import heapq
import json
import os
import socket
import stat
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .logger import get_logger

log = get_logger("ai")

__all__ = [
    "AiAnalysisError",
    "AiClient",
    "FileEntry",
    "FileInsight",
    "build_messages",
    "collect_files",
    "entry_key",
    "parse_insights",
]


class AiAnalysisError(RuntimeError):
    """AI 分析过程中的可读错误（界面直接展示 message）。"""


@dataclass(slots=True)
class FileEntry:
    """待分析的文件条目（只保留 AI 需要的最小信息）。"""

    name: str      # 文件名（含扩展名）
    path: str      # 完整路径
    size: int = 0  # 字节
    mtime: float = 0.0
    rel_dir: str = ""   # 相对根目录的子目录（正斜杠分隔，根目录为空串）

    @property
    def ext(self) -> str:
        """小写扩展名（含点），无扩展名返回空串。"""
        return os.path.splitext(self.name)[1].lower()


@dataclass(slots=True)
class FileInsight:
    """单个文件的 AI 识别结果。"""

    name: str = ""       # 文件名（与 FileEntry.name 一致）
    rel_dir: str = ""    # 所在子目录（与 FileEntry.rel_dir 一致，用于对齐）
    owner: str = ""      # 软件归属（如 "腾讯 / 微信"、"Windows 系统组件"）
    purpose: str = ""    # 用途说明
    category: str = ""   # 类型：程序/文档/配置/缓存/日志/驱动/媒体/其他
    ok: bool = False     # AI 是否成功返回了该文件的说明


def entry_key(rel_dir: str, name: str) -> str:
    """生成文件的唯一键：``子目录/文件名``（统一正斜杠与小写）。

    递归收集后不同子目录会出现同名文件，纯文件名不再唯一，
    界面对齐与结果解析都改用该键。规则：
    * 反斜杠统一成正斜杠、整体转小写、去掉开头多余的 ``./``；
    * AI 有时会把目录重复拼进文件名（如 ``sub/sub/a.txt``），这里自动去除。

    :param rel_dir: 相对子目录（根目录传空串）
    :param name: 文件名（允许携带 rel_dir 前缀，会自动纠正）
    :return: 形如 ``sub/a.txt`` 或 ``a.txt`` 的小写唯一键
    """
    rel = (rel_dir or "").strip().replace("\\", "/").strip("/")
    name = (name or "").strip().replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    if rel:
        prefix = rel.lower() + "/"
        while name.lower().startswith(prefix):   # 可能重复多层（p1/p1/a.txt）
            name = name[len(rel) + 1:]
    return (f"{rel}/{name}" if rel else name).lower()


# ----------------------------------------------------------------------
# 第 1 步：收集文件（递归）
# ----------------------------------------------------------------------
def collect_files(directory: str, max_files: int = 200,
                  per_dir_limit: int = 50) -> List[FileEntry]:
    """递归列出目录及其子目录内的文件，按大小降序取前 N 个。

    设计（针对 D 盘等“根目录全是文件夹”的情况）：
    * 用 ``os.walk`` 递归遍历，不跳过隐藏/系统目录——有些系统组件就藏在
      深层的隐藏目录里；
    * 每个目录内部先按大小降序取前 ``per_dir_limit`` 个（防止某个巨型
      目录独占名额），再全局合并取前 ``max_files`` 个；
    * 任何单目录访问失败（权限不足等）都跳过继续，不中断整体收集；
    * 符号链接不跟随（避免循环）；目录本身用 ``DirEntry.stat`` 判断，
      不为目录也不为普通文件（如管道）的条目直接跳过。

    :param directory: 目标目录
    :param max_files: 全局最多返回的文件数（防止一次分析过多拖慢/费 token）
    :param per_dir_limit: 每个子目录最多取多少个文件（大文件优先）
    :return: :class:`FileEntry` 列表（按大小降序，携带 rel_dir 相对路径）
    :raises AiAnalysisError: 目录不存在或无法读取
    """
    if not directory or not os.path.isdir(directory):
        raise AiAnalysisError(f"目录不存在或不可访问：{directory}")
    root = os.path.normpath(directory)
    # 全局堆：只保留最大的 max_files 个，省内存（避免把整个目录树都装进列表）
    heap: List[tuple] = []
    for current, _dirs, _files in os.walk(root, topdown=True):
        try:
            with os.scandir(current) as scanner:
                local = []
                for item in scanner:
                    try:
                        info = item.stat(follow_symlinks=False)
                    except OSError:
                        continue            # 权限不足/被占用：跳过，不中断整体
                    if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                        local.append((info.st_size, info.st_mtime,
                                      item.path, item.name))
                # 该目录内部取最大的 per_dir_limit 个
                local.sort(key=lambda t: t[0], reverse=True)
                for size, mtime, full_path, name in local[:per_dir_limit]:
                    rel = os.path.relpath(os.path.dirname(full_path), root)
                    rel_dir = "" if rel == "." else rel.replace(os.sep, "/")
                    dedup = (size, full_path)   # 路径已唯一，防重复加入
                    item = (size, dedup, mtime, full_path, name, rel_dir)
                    if len(heap) < max_files:
                        heapq.heappush(heap, item)
                    elif size > heap[0][0]:
                        heapq.heapreplace(heap, item)
        except OSError:
            continue                        # 该子目录不可读：跳过继续
    entries = [FileEntry(name=name, path=path, size=size, mtime=mtime,
                         rel_dir=rel_dir)
               for size, _dedup, mtime, path, name, rel_dir in heap]
    entries.sort(key=lambda e: e.size, reverse=True)
    return entries


# ----------------------------------------------------------------------
# 第 2 步：构造提示词
# ----------------------------------------------------------------------
_SYSTEM_PROMPT = (
    "你是 Windows 系统与常用软件专家，负责识别文件的功能与归属。"
    "请根据文件名、扩展名、大小、修改时间推断每个文件的用途和所属软件/系统组件。"
    "信息不足时给出最可能的推测，并在结尾用“（推测）”标注；"
    "禁止编造确定性的错误结论。只输出 JSON 数组，不要输出任何解释文字。"
)


def build_messages(entries: Sequence[FileEntry], directory: str) -> List[dict]:
    """构造 OpenAI 兼容接口的 ``messages`` 参数。

    :param entries: 本批次文件条目
    :param directory: 文件所在目录（提供上下文，提高归属判断准确率）
    :return: [{"role": "system", ...}, {"role": "user", ...}]
    """
    lines = [f"目录：{directory}",
             "文件清单（所在目录/文件名 | 大小字节 | 修改日期，子目录以“/”分隔）："]
    for entry in entries:
        mtime = (time.strftime("%Y-%m-%d", time.localtime(entry.mtime))
                 if entry.mtime else "未知")
        display = f"{entry.rel_dir}/{entry.name}" if entry.rel_dir else entry.name
        lines.append(f"- {display} | {entry.size} | {mtime}")
    user_prompt = (
        "\n".join(lines)
        + "\n\n请为上面每个文件输出一个 JSON 对象，字段：\n"
          'name：与清单完全一致的“所在目录/文件名”（根目录文件只写文件名，'
          '如 sub/a.txt）；\n'
          'owner：软件归属（如 "腾讯 / 微信"、"Windows 系统组件"、"Google / Chrome"）；\n'
          'purpose：不超过 50 字的用途说明；\n'
          'category：类型，取值限定为：程序/文档/配置/缓存/日志/驱动/媒体/其他。\n'
          "输出：仅一个 JSON 数组，按清单顺序排列。"
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


# ----------------------------------------------------------------------
# 第 4 步：解析结果
# ----------------------------------------------------------------------
def _extract_json_array(text: str) -> str:
    """从模型回复中提取 JSON 数组文本（容忍 Markdown 代码块围栏）。"""
    if not text or not text.strip():
        raise AiAnalysisError("AI 返回了空内容。")
    stripped = text.strip()
    # 剥离 ```json ... ``` 围栏（部分兼容服务会自动带格式）
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1:]
        if stripped.endswith("```"):
            stripped = stripped[:-3].strip()
    start, end = stripped.find("["), stripped.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise AiAnalysisError("AI 返回的内容中没有找到 JSON 数组。")
    return stripped[start:end + 1]


def parse_insights(text: str, entries: Sequence[FileEntry]) -> List[FileInsight]:
    """解析模型回复，并按“所在目录/文件名”对齐回文件清单。

    解析规则：
    * 只接受 JSON 数组，元素需包含 name 字段（大小写不敏感匹配）；
    * 递归收集后不同子目录可能同名，对齐键是 :func:`entry_key`
      （``子目录/文件名``），AI 返回的 name 即使漏带或带错目录前缀
      也能自动归一化纠正；
    * AI 漏掉的文件会生成 ``ok=False`` 的占位结果，保证结果条数
      始终等于文件数（界面表格逐行对应）。

    :param text: 模型原始回复
    :param entries: 本批次文件清单
    :return: 与 entries 顺序一致的 :class:`FileInsight` 列表
    """
    payload = _extract_json_array(text)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AiAnalysisError(f"AI 返回的内容不是有效 JSON：{exc}") from exc
    if not isinstance(data, list):
        raise AiAnalysisError("AI 返回的 JSON 不是数组。")

    by_key: Dict[str, FileEntry] = {
        entry_key(entry.rel_dir, entry.name): entry for entry in entries}
    filled: Dict[str, FileInsight] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        key = entry_key(str(item.get("rel_dir") or ""),
                        str(item.get("name") or ""))
        if key in by_key and key not in filled:
            target = by_key[key]
            filled[key] = FileInsight(
                name=target.name,
                rel_dir=target.rel_dir,
                owner=str(item.get("owner") or "").strip(),
                purpose=str(item.get("purpose") or "").strip(),
                category=str(item.get("category") or "").strip(),
                ok=True,
            )
    return [
        filled.get(entry_key(entry.rel_dir, entry.name), FileInsight(
            name=entry.name, rel_dir=entry.rel_dir,
            purpose="AI 未返回该文件的说明"))
        for entry in entries
    ]


# ----------------------------------------------------------------------
# 第 3 步：请求接口
# ----------------------------------------------------------------------
class AiClient:
    """OpenAI 兼容 Chat Completions 客户端（阻塞式，请在后台线程调用）。"""

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: float = 60.0) -> None:
        """
        :param base_url: 接口地址（需包含版本号，如 https://api.openai.com/v1）
        :param api_key: API Key
        :param model: 模型名（如 gpt-4o-mini）
        :param timeout: 单次请求超时秒数
        """
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.timeout = max(5.0, float(timeout))
        self.endpoint = f"{self.base_url}/chat/completions"

    def _validate(self) -> None:
        """请求前校验配置，缺失项给出可操作的中文提示。"""
        missing = []
        if not self.base_url:
            missing.append("API 地址（如 https://api.openai.com/v1）")
        if not self.api_key:
            missing.append("API Key")
        if not self.model:
            missing.append("模型名称")
        if missing:
            raise AiAnalysisError("请先在“AI 设置”中补充：" + "、".join(missing))

    def chat(self, messages: Sequence[dict]) -> str:
        """发送一次对话请求，返回模型回复文本。

        :param messages: :func:`build_messages` 构造的消息列表
        :return: 模型回复的字符串
        :raises AiAnalysisError: 配置缺失 / 网络 / HTTP / 响应格式错误
        """
        self._validate()
        payload = json.dumps({
            "model": self.model,
            "messages": list(messages),
            "temperature": 0.2,          # 低随机性，结果更稳定
            "stream": False,
        }).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
        )
        log.info("AI 请求 -> %s（模型 %s）", self.endpoint, self.model)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            hint = {401: "API Key 无效", 403: "没有权限",
                    404: "接口地址或模型名可能有误",
                    429: "请求过于频繁或额度不足"}.get(exc.code, "")
            raise AiAnalysisError(
                f"接口返回 HTTP {exc.code}{('：' + hint) if hint else ''}\n{detail}"
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise AiAnalysisError(
                f"请求超时（>{self.timeout:.0f} 秒），请检查网络或换用更快的模型。") from exc
        except urllib.error.URLError as exc:
            raise AiAnalysisError(f"无法连接 API：{exc.reason}") from exc
        except OSError as exc:
            raise AiAnalysisError(f"网络请求失败：{exc}") from exc

        try:
            data = json.loads(body)
            content = data["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise AiAnalysisError(
                f"接口响应格式异常（{exc}），请确认接口地址是 OpenAI 兼容服务。") from exc
        if not isinstance(content, str) or not content.strip():
            raise AiAnalysisError("接口返回的回复内容为空。")
        return content

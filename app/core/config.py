"""用户偏好设置：读写 JSON 配置文件（%APPDATA%\\SpaceViewer\\settings.json）。

特性：
* 只保存白名单内的键，避免脏数据；
* 原子写入（先写 .tmp 再 replace），防止断电/崩溃导致配置损坏；
* 读取失败时自动备份坏文件并使用默认值，保证程序永远能启动。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List

from .formatter import UNIT_CHOICES
from .logger import get_logger
from .paths import config_file

log = get_logger("config")

# 最近使用路径最多保留条数
MAX_RECENT = 12

DEFAULTS: Dict[str, Any] = {
    # 外观
    "theme": "dark",                 # dark / light
    "font_scale": 1.0,               # 界面字号缩放（0.9 ~ 1.3）
    # 扫描
    "last_path": "",                 # 上次扫描的根目录
    "recent_paths": [],              # 历史路径
    "max_depth": 0,                  # 递归层级上限，0 = 不限制
    "max_records": 400000,           # 目录记录上限，保护内存
    "follow_symlinks": False,        # 是否统计软链接/联接点指向的内容
    "skip_hidden": False,            # 跳过隐藏目录（Windows 隐藏属性）
    "skip_system": False,            # 跳过系统目录
    "skip_reserved": False,          # 跳过 $Recycle.Bin / System Volume Information 等
    # 显示与排序
    "sort_key": "size",              # size / name / files / subdirs / modified
    "sort_desc": True,               # True = 从大到小
    "size_unit": "auto",             # auto / B / KB / MB / GB
    "decimals": 2,                   # 小数位数
    "top_n": 500,                    # 列表最多显示条数
    "show_all_levels": False,        # False = 仅当前层，True = 全部层级（排行模式）
    "show_chart": True,              # 是否显示右侧可视化面板
    # AI 文件分析（OpenAI 兼容接口，手动触发）
    "ai_base_url": "",               # 接口地址，例如 https://api.openai.com/v1
    "ai_api_key": "",                # API Key（明文保存在本机配置文件中）
    "ai_model": "",                  # 模型名，例如 gpt-4o-mini
    "ai_batch_size": 30,             # 每次请求携带的文件数
    "ai_max_files": 200,             # 单次分析的最大文件数（按大小取前 N）
    # 窗口状态
    "geometry": [1320, 820],
    "window_pos": None,
    "maximized": False,
    "splitter": [],                  # 分隔条位置
    "column_widths": {},             # 表头列宽
}

_SORT_KEYS = ("size", "name", "files", "subdirs", "modified", "percent")


class ConfigManager:
    """配置管理器（单例风格，界面层共享一个实例即可）。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path else config_file()
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = dict(DEFAULTS)
        self.load()

    # ------------------------------------------------------------------ 读写
    def load(self) -> Dict[str, Any]:
        """从磁盘加载配置，异常时回退默认值。"""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.info("配置文件不存在，使用默认配置：%s", self._path)
            return self._data
        except Exception as exc:  # JSON 损坏、编码错误等
            log.warning("配置文件损坏，已重置：%s", exc)
            try:
                shutil.copy2(self._path, self._path.with_suffix(".bak"))
            except OSError:
                pass
            return self._data

        data: Dict[str, Any] = {}
        for key in DEFAULTS:
            if key in raw:
                data[key] = _coerce(key, raw[key])
        if not data.get("recent_paths"):
            data["recent_paths"] = []
        # 用 setdefault 保留 DEFAULTS 中新增的键，向后兼容旧配置文件
        merged = dict(DEFAULTS)
        merged.update(data)
        self._data = merged
        return self._data

    def save(self) -> bool:
        """原子写入磁盘，返回是否成功。"""
        with self._lock:
            payload = json.dumps(
                {k: self._data.get(k) for k in DEFAULTS},
                ensure_ascii=False, indent=2,
            )
            tmp = self._path.with_suffix(".tmp")
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as fp:
                    fp.write(payload)
                    fp.flush()
                    os.fsync(fp.fileno())
                os.replace(tmp, self._path)
                return True
            except OSError as exc:
                log.error("保存配置失败：%s", exc)
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                return False

    # ------------------------------------------------------------------ 访问
    def get(self, key: str, default: Any = None) -> Any:
        """读取一个配置项。"""
        return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any, save: bool = False) -> None:
        """写入一个配置项（仅白名单键生效）。"""
        if key not in DEFAULTS:
            log.debug("忽略未知配置项：%s", key)
            return
        self._data[key] = _coerce(key, value)
        if save:
            self.save()

    def update(self, mapping: Dict[str, Any], save: bool = True) -> None:
        """批量写入配置。"""
        for key, value in mapping.items():
            self.set(key, value)
        if save:
            self.save()

    @property
    def path(self) -> Path:
        """配置文件位置。"""
        return self._path

    # ------------------------------------------------------- 常用位置/历史
    @property
    def recent_paths(self) -> List[str]:
        """最近扫描过的路径列表（由新到旧）。"""
        return list(self._data.get("recent_paths") or [])

    def push_recent(self, path: str) -> None:
        """把路径加入历史记录（去重、限量）。"""
        path = (path or "").strip()
        if not path:
            return
        items = [p for p in self.recent_paths if p.lower() != path.lower()]
        items.insert(0, path)
        self._data["recent_paths"] = items[:MAX_RECENT]

    def remove_recent(self, path: str) -> None:
        """删除某条历史记录。"""
        target = (path or "").lower()
        self._data["recent_paths"] = [
            p for p in self.recent_paths if p.lower() != target]

    def clear_recent(self) -> None:
        """清空历史记录。"""
        self._data["recent_paths"] = []

    # ------------------------------------------------------------------ 便捷
    @property
    def theme(self) -> str:
        """当前主题名（dark/light）。"""
        return "light" if self._data.get("theme") == "light" else "dark"

    @theme.setter
    def theme(self, value: str) -> None:
        self._data["theme"] = "light" if value == "light" else "dark"

    def toggle_theme(self) -> str:
        """在深色/浅色之间切换并返回新的主题名。"""
        self.theme = "light" if self.theme == "dark" else "dark"
        return self.theme


def _coerce(key: str, value: Any) -> Any:
    """把外部输入转换成配置项期望的类型，容错处理。"""
    try:
        if key in {"max_depth", "max_records", "decimals", "top_n"}:
            return max(0, int(value))
        if key in {"sort_desc", "follow_symlinks", "skip_hidden", "skip_system",
                   "skip_reserved", "show_all_levels", "show_chart", "maximized"}:
            return bool(value)
        if key == "font_scale":
            return min(1.3, max(0.9, float(value)))
        if key == "theme":
            return "light" if str(value) == "light" else "dark"
        if key == "size_unit":
            return str(value) if str(value) in UNIT_CHOICES else "auto"
        if key == "sort_key":
            return str(value) if str(value) in _SORT_KEYS else "size"
        if key == "recent_paths":
            return [str(p) for p in (value or [])][:MAX_RECENT]
        if key in {"geometry", "window_pos", "splitter"}:
            return [int(v) for v in (value or [])] if value else value
        if key == "column_widths":
            return {str(k): int(v) for k, v in (value or {}).items()}
        if key == "last_path":
            return str(value or "")
        if key in {"ai_base_url", "ai_api_key", "ai_model"}:
            return str(value or "").strip()
        if key in {"ai_batch_size", "ai_max_files"}:
            # 合法范围：每批 1~100 个文件，单次最多 2000 个
            return min(100 if key == "ai_batch_size" else 2000,
                       max(1, int(value)))
    except (TypeError, ValueError):
        return DEFAULTS.get(key)
    return value

"""可复用的界面构件（卡片、指标块、路径栏、面包屑、扫描控制条等）。"""

from __future__ import annotations

import os
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
                               QProgressBar, QPushButton, QSizePolicy, QToolButton,
                               QVBoxLayout, QWidget)

from ..core.formatter import format_count, format_duration, format_size
from ..core.volumes import list_volumes, quick_locations
from .flow_layout import FlowLayout
from .theme import current_palette


# --------------------------------------------------------------------- 卡片
class Card(QFrame):
    """圆角卡片容器：可选标题栏 + 内容区。"""

    def __init__(self, title: str = "", parent: QWidget | None = None,
                 actions: Sequence[QWidget] = (), object_name: str = "Card") -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(14, 12, 14, 12)
        self._root.setSpacing(10)
        self._title = QLabel(title)
        self._title.setObjectName("H2")
        self._header = QHBoxLayout()
        self._header.setContentsMargins(0, 0, 0, 0)
        self._header.addWidget(self._title)
        self._header.addStretch(1)
        for action in actions:
            self._header.addWidget(action)
        if title or actions:
            self._root.addLayout(self._header)
        self._body = QVBoxLayout()
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(8)
        self._root.addLayout(self._body)

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        """向卡片内容区追加控件。"""
        self._body.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout) -> None:
        """向内容区追加子布局。"""
        self._body.addLayout(layout)

    def clear_body(self) -> None:
        """清空内容区（重建动态卡片时使用）。"""
        while self._body.count():
            item = self._body.takeAt(0)
            if item is None:
                break
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            delete = getattr(item, "deleteLater", None)
            if callable(delete):
                delete()

    def set_title(self, text: str) -> None:
        """修改标题。"""
        self._title.setText(text)

    def title(self) -> str:
        """当前标题。"""
        return self._title.text()


class MetricTile(QFrame):
    """单个指标小块：上方数值、下方说明。"""

    def __init__(self, name: str, value: str = "--", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Inner")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(1)
        self._value = QLabel(value)
        self._value.setObjectName("MetricValue")
        self._name = QLabel(name)
        self._name.setObjectName("MetricName")
        layout.addWidget(self._value)
        layout.addWidget(self._name)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_value(self, text: str, tip: str = "") -> None:
        """更新数值与悬浮说明。"""
        self._value.setText(str(text))
        if tip:
            self.setToolTip(tip)

    @property
    def value_label(self) -> QLabel:
        """数值标签（便于外部改样式）。"""
        return self._value


class PillButton(QPushButton):
    """胶囊按钮（用于排序、层级等可切换选项）。"""

    def __init__(self, text: str, parent: QWidget | None = None,
                 checkable: bool = True) -> None:
        super().__init__(text, parent)
        self.setObjectName("Pill")
        self.setCheckable(checkable)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)


class MenuButton(QPushButton):
    """点击弹出菜单的按钮（“排序方式”“单位”等）。"""

    def __init__(self, text: str, parent: QWidget | None = None,
                 pill: bool = True) -> None:
        super().__init__(text, parent)
        self.setObjectName("Pill" if pill else "")
        self._menu = QMenu(self)
        self.setMenu(self._menu)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    @property
    def menu(self) -> QMenu:  # noqa: D401 - 覆盖基类属性写法
        """按钮关联的菜单。"""
        return self._menu

    def set_menu(self, menu: QMenu) -> None:
        """替换整个菜单。"""
        self._menu = menu
        super().setMenu(menu)

    def add_action(self, text: str, handler: Callable[[], None],
                   checkable: bool = False, checked: bool = False,
                   shortcut: str = "") -> QAction:
        """便捷地添加一个菜单项。"""
        action = QAction(text, self)
        action.setCheckable(checkable)
        action.setChecked(checked)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(handler)
        self._menu.addAction(action)
        return action


# ------------------------------------------------------------------ 路径栏
class PathBar(QFrame):
    """根目录选择栏：手输路径 + 浏览 + 快捷位置 + 磁盘下拉。"""

    startRequested = Signal(str)    # 回车或点击“开始扫描”
    pathPicked = Signal(str)        # 通过对话框/菜单选择

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Inner")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 5, 6, 5)
        layout.setSpacing(6)

        self.edit = QLineEdit(self)
        self.edit.setObjectName("PathInput")
        self.edit.setPlaceholderText("输入或粘贴文件夹路径，例如 D:\\项目 或 C:\\Users\\你的用户名\\Desktop")
        self.edit.setClearButtonEnabled(True)
        self.edit.returnPressed.connect(lambda: self.startRequested.emit(self.path()))
        layout.addWidget(self.edit, 1)

        self.locations = MenuButton("快捷位置", self)
        self._build_locations_menu()
        layout.addWidget(self.locations)

        self.drives = MenuButton("磁盘", self)
        self._build_drives_menu()
        layout.addWidget(self.drives)

        browse = QPushButton("浏览…", self)
        browse.setCursor(Qt.CursorShape.PointingHandCursor)
        browse.clicked.connect(self._browse)
        layout.addWidget(browse)

    # ------------------------------------------------------------ 功能
    def path(self) -> str:
        """输入框中的路径（去掉首尾引号与空白）。"""
        return self.edit.text().strip().strip('"').strip()

    def set_path(self, path: str) -> None:
        """设置输入框路径。"""
        self.edit.setText(path or "")

    def set_valid(self, valid: bool) -> None:
        """标记路径是否有效（红色边框提示）。"""
        self.edit.setProperty("invalid", "false" if valid else "true")
        style = self.edit.style()
        style.unpolish(self.edit)
        style.polish(self.edit)

    def refresh_menus(self) -> None:
        """刷新“快捷位置/磁盘”菜单（磁盘插拔后调用）。"""
        self._build_locations_menu()
        self._build_drives_menu()

    def _build_locations_menu(self) -> None:
        menu = QMenu(self)
        for label, path in quick_locations():
            action = menu.addAction(f"{label}    {path}")
            action.setData(path)
            action.triggered.connect(lambda checked=False, p=path: self._picked(p))
        if not menu.actions():
            empty = menu.addAction("（未找到常用位置）")
            empty.setEnabled(False)
        self.locations.set_menu(menu)

    def _build_drives_menu(self) -> None:
        menu = QMenu(self)
        volumes = list_volumes()
        for volume in volumes:
            action = menu.addAction(volume.text())
            action.setData(volume.path)
            action.setToolTip(f"剩余 {format_size(volume.free)} / 共 {format_size(volume.total)}")
            action.triggered.connect(lambda checked=False, p=volume.path: self._picked(p))
        if not volumes:
            empty = menu.addAction("（未检测到磁盘分区）")
            empty.setEnabled(False)
        self.drives.set_menu(menu)

    def _picked(self, path: str) -> None:
        """内部：选中某路径后立即开始扫描。"""
        self.set_path(path)
        self.pathPicked.emit(path)
        self.startRequested.emit(path)

    def _browse(self) -> None:
        """调用系统文件夹选择对话框。"""
        from ..core.volumes import pick_folder_dialog

        chosen = pick_folder_dialog(self, self.path())
        if chosen:
            self._picked(chosen)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """支持 F5 直接开始扫描。"""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.startRequested.emit(self.path())
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------- 面包屑
class Breadcrumb(QWidget):
    """层级导航条：扫描根目录 \\ 子目录 \\ …，可点击回跳。"""

    navigate = Signal(int)  # 目录 id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = FlowLayout(self, margin=0, h_spacing=4, v_spacing=4)
        self._chain: List[Tuple[int, str]] = []
        self._buttons: List[QPushButton] = []   # 按钮池（复用，不销毁）
        self._seps: List[QLabel] = []           # 分隔符池（复用，不销毁）

    def _spawn_button(self) -> QPushButton:
        """新建一个导航按钮（仅池不足时调用）。"""
        button = QPushButton(self)
        button.setObjectName("Ghost")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(self._on_button_clicked)
        return button

    def _on_button_clicked(self) -> None:
        """根据按钮上记录的目录 id 发出导航信号。"""
        button = self.sender()
        if button is None:
            return
        dir_id = button.property("dirId")
        # 注意根目录 id 为 0，不能用 “or -1” 的写法（0 会被误判）
        self.navigate.emit(-1 if dir_id is None else int(dir_id))

    def set_chain(self, chain: Sequence[Tuple[int, str]]) -> None:
        """更新面包屑（chain 为 (目录 id, 显示名) 由根到当前）。

        与旧实现“全部 deleteLater 再新建”不同，这里复用按钮与分隔符：
        1. deleteLater 是异步销毁，旧按钮在销毁前会以游离状态闪现一帧；
        2. 整排重建让按钮宽度、布局高度每次都可能变化，视觉上在晃。
        """
        self._chain = list(chain)
        count = len(self._chain)
        while len(self._buttons) < count:
            self._buttons.append(self._spawn_button())
        while len(self._seps) < max(0, count - 1):
            sep = QLabel("›", self)
            sep.setObjectName("Muted")
            self._seps.append(sep)

        # 更新文本 / 记录的目录 id / 可见性
        for index, (dir_id, name) in enumerate(self._chain):
            button = self._buttons[index]
            button.setText(name)
            button.setProperty("dirId", int(dir_id))
            button.setVisible(True)
        for index, sep in enumerate(self._seps):
            sep.setVisible(index < count - 1)

        # 目标显示顺序：按钮0 分隔0 按钮1 分隔1 …
        ordered: List[QWidget] = []
        for index in range(count):
            if index:
                ordered.append(self._seps[index - 1])
            ordered.append(self._buttons[index])
        self._reorder(ordered)

    def _reorder(self, ordered: List[QWidget]) -> None:
        """把布局条目重排成 ``ordered`` 的顺序（仅顺序变化时才动布局）。"""
        current = [self._layout.itemAt(i).widget()
                   for i in range(self._layout.count())]
        if current == ordered:
            return
        while self._layout.count():
            self._layout.takeAt(0)
        for widget in ordered:
            self._layout.addWidget(widget)
        # 未出现在本次序列里的池成员必须隐藏，否则会游离显示在角落
        used = {id(widget) for widget in ordered}
        for widget in self._buttons + self._seps:
            if id(widget) not in used and widget.isVisible():
                widget.setVisible(False)

    def clear(self) -> None:
        """清空。"""
        self.set_chain([])

    def current_id(self) -> int:
        """当前所在目录 id，空则 -1。"""
        return self._chain[-1][0] if self._chain else -1


# ------------------------------------------------------------ 扫描控制区
class ScanPanel(Card):
    """扫描控制 + 实时进度（含进度条、预估剩余时间、统计指标）。"""

    startClicked = Signal()
    pauseClicked = Signal()
    resumeClicked = Signal()
    stopClicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("扫描进度", parent)
        self._last_dir_id = 0

        self.bar = QProgressBar(self)
        self.bar.setObjectName("Big")
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.add(self.bar)

        self.status = QLabel("等待开始 —— 选择一个文件夹，点击“开始扫描”。")
        self.status.setObjectName("Secondary")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.add(self.status)

        # 用 FlowLayout：右侧栏变窄时指标块自动换行，不会被压扁
        # （不传 parent，避免与 Card 已有的 QVBoxLayout 冲突）
        tiles = FlowLayout(margin=0, h_spacing=8, v_spacing=8)
        self.tile_total = MetricTile("已扫描大小", "--", self)
        self.tile_files = MetricTile("文件数", "--", self)
        self.tile_dirs = MetricTile("目录数", "--", self)
        self.tile_speed = MetricTile("扫描速度", "--", self)
        self.tile_eta = MetricTile("预计剩余", "--", self)
        for tile in (self.tile_total, self.tile_files, self.tile_dirs,
                     self.tile_speed, self.tile_eta):
            # 固定宽度：扫描中数值/速度/剩余时间每秒都在变，
            # 若宽度随文本变化，流式布局会不停重排导致卡片抖动
            tile.setFixedWidth(104)
            tiles.addWidget(tile)
        self.add_layout(tiles)

        buttons = FlowLayout(margin=0, h_spacing=8, v_spacing=8)
        self.btn_start = QPushButton("▶  开始扫描", self)
        self.btn_start.setObjectName("Primary")
        self.btn_start.setShortcut(QKeySequence("Ctrl+S"))
        self.btn_start.clicked.connect(self.startClicked)
        self.btn_pause = QPushButton("⏸  暂停", self)
        self.btn_pause.clicked.connect(self._pause_or_resume)
        self.btn_stop = QPushButton("⏹  停止", self)
        self.btn_stop.setObjectName("Danger")
        self.btn_stop.clicked.connect(self.stopClicked)
        buttons.addWidget(self.btn_start)
        buttons.addWidget(self.btn_pause)
        buttons.addWidget(self.btn_stop)
        buttons.addStretch(1)
        self.add_layout(buttons)

        self._running = False
        self._paused = False
        self.set_buttons()

    # ------------------------------------------------------------ 按钮状态
    def _pause_or_resume(self) -> None:
        if self._paused:
            self.resumeClicked.emit()
        else:
            self.pauseClicked.emit()

    def set_running(self, running: bool, paused: bool = False) -> None:
        """切换按钮可用性与文案。"""
        self._running = running
        self._paused = paused
        self.set_buttons()

    def set_buttons(self) -> None:
        """根据运行状态刷新按钮。"""
        self.btn_start.setEnabled(not self._running)
        self.btn_pause.setEnabled(self._running)
        self.btn_stop.setEnabled(self._running)
        self.btn_pause.setText("▶  继续" if self._paused else "⏸  暂停")
        self.btn_start.setText("▶  重新扫描" if not self._running and self._last_dir_id else
                               "▶  开始扫描")

    # ------------------------------------------------------------ 进度更新
    def begin(self, root: str) -> None:
        """开始一次扫描时重置界面。"""
        self._last_dir_id = 0
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.status.setText(f"正在扫描：{root}")
        for tile in (self.tile_total, self.tile_files, self.tile_dirs,
                     self.tile_speed, self.tile_eta):
            tile.set_value("--")
        self.set_running(True)

    def update_progress(self, percent: float, bytes_done: int, files: int, dirs: int,
                        speed: float, eta: Optional[float], current: str,
                        paused: bool = False) -> None:
        """刷新进度条与指标。"""
        self.bar.setValue(int(max(0.0, min(100.0, percent)) * 10))
        self.tile_total.set_value(format_size(bytes_done))
        self.tile_files.set_value(format_count(files))
        self.tile_dirs.set_value(format_count(dirs))
        self.tile_speed.set_value(f"{format_size(speed)}/s" if speed else "--")
        self.tile_eta.set_value(format_duration(eta) if eta else "--")
        prefix = "已暂停 —— " if paused else ""
        suffix = f"正在扫描：{current}" if current else "正在准备…"
        text = f"{prefix}{suffix}  ·  {percent:.1f}%"
        if self.status.text() != text:
            self.status.setText(text)
        self.status.setToolTip(current)
        if paused != self._paused:
            self.set_running(True, paused)

    def finish(self, message: str, elapsed: float) -> None:
        """扫描结束。"""
        self._last_dir_id = 1
        self.bar.setValue(self.bar.maximum())
        self.status.setText(f"{message}（用时 {format_duration(elapsed)}）")
        self.set_running(False)

    def fail(self, message: str) -> None:
        """扫描失败。"""
        self._last_dir_id = 1
        self.status.setText(f"扫描中断：{message}")
        self.bar.setRange(0, 100)
        self.set_running(False)


def build_help_text() -> str:
    """“使用说明”对话框正文（HTML）。"""
    return """
<div style='line-height:150%'>
<p><b>三步完成分析</b></p>
<ol>
  <li>在顶部输入框粘贴路径，或用 <i>快捷位置 / 磁盘 / 浏览…</i> 选择文件夹；</li>
  <li>点击 <b>▶ 开始扫描</b>，可随时 <b>暂停 / 继续 / 停止</b>（停止后已完成的数据仍然可查看）；</li>
  <li>在列表中查看排行，双击进入子目录，右键可打开位置、复制路径或导出。</li>
</ol>
<p><b>小技巧</b></p>
<ul>
  <li><b>仅当前层 / 全部层级</b>：前者看某个文件夹内部构成，后者把全盘所有层级目录放在一起排行，找“罪魁祸首”最快。</li>
  <li><b>暂停/继续</b>：状态栏与进度卡片会显示“已暂停”，数据不会丢失。</li>
  <li><b>导出</b>：支持 CSV（Excel 直接双击打开）与 XLSX（自带数据条、筛选、冻结表头）。</li>
  <li><b>AI 分析</b>：点击工具条上的 <b>AI 分析</b>（或 <code>Ctrl+I</code>），可识别当前目录下
      所有文件的用途与软件归属（需在“AI 设置”里配置 OpenAI 兼容接口；
      只有手动点击“开始分析”才会发起请求，结果可导出 CSV/Excel）。</li>
  <li>扫描 C 盘等系统目录时，若提示“拒绝访问”，请以管理员身份运行，或勾选“排除系统保留目录”。</li>
  <li><b>为什么手工加总比扫描结果小？</b>本程序统计的是文件系统里的全部条目，包括隐藏与系统文件
      （如 <code>pagefile.sys</code>、<code>hiberfil.sys</code>、<code>$RECYCLE.BIN</code>），
      资源管理器默认不显示它们；另外无权限访问的目录无法统计、软链接/联接点默认不重复计入，
      这些都会造成差异。选中一行右键“查看详情”可看到其中隐藏/系统文件的占比。</li>
  <li>快捷键：<code>Ctrl+O</code> 浏览、<code>Ctrl+S</code> 扫描、<code>Ctrl+P</code> 暂停/继续、
      <code>Esc</code> 停止、<code>Ctrl+E</code> 导出、<code>Ctrl+I</code> AI 分析、
      <code>Ctrl+T</code> 切换主题、
      <code>Alt+↑</code> 返回上级、<code>F5</code> 重新扫描。</li>
</ul>
</div>
"""

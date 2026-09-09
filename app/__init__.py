"""空间查看器 —— Windows 文件夹大小分析与排行应用。

包结构说明：
    app.core  纯 Python 逻辑层（扫描引擎、数据模型、格式化、配置、导出、日志）
    app.ui    界面层（PySide6 窗口、控件、主题）

设计原则：core 层不依赖 Qt，便于单元测试与后续复用（例如换成命令行版本）。
"""

__version__ = "1.0.0"

APP_NAME = "空间查看器"
APP_ID = "SpaceViewer"

# 空间查看器（SpaceViewer）

Windows 文件夹大小分析工具 —— 一眼看穿磁盘空间被谁占了。

基于 **PySide6** 构建，APIFox 风格的现代化界面，支持深浅色主题。扫完磁盘/文件夹后，按占用大小排行展示，并附可视化图表与 AI 文件用途分析。

![界面截图](截图/c1248589deb8f49d4695e8e06525660c.png)

## 功能特性

- **递归扫描**：统计每个文件夹的占用大小、文件数与子目录数，大文件优先排序
- **多维度排行**：按 大小 / 名称 / 文件数 / 子目录数 / 修改时间 排序，支持正序倒序
- **实时进度**：扫描过程可视化（已处理目录、速度、剩余时间），支持 暂停 / 继续 / 停止
- **散文件聚合**：目录直属的零散文件自动聚合为一行（如"散文件 56 个 · 180 MB"），双击可查看明细
- **自绘图表**：横向条形排行图 + 环形占比图（文件类型分布），无需第三方绘图库，跟随深浅色主题
- **AI 文件分析**：接入 OpenAI 兼容接口（DeepSeek / Kimi / 智谱 / 通义千问 等预设），根据"文件名 / 大小 / 修改时间"推断每个文件的用途与软件归属
- **导出报告**：一键导出 CSV / Excel（xlsx）分析报告
- **现代化界面**：深浅色主题、圆角卡片、键盘快捷键，高分屏适配

## 环境要求

- Windows 10 / 11
- Python 3.10+

## 安装与运行

```bash
# 1. 安装依赖（国内用户推荐清华源，速度更快）
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2. 启动
python main.py

# 也可以双击仓库根目录的「启动.bat」
```

如果只需使用、不想装 Python 环境，可直接运行打包好的单文件 exe（见下）。

## 打包成 exe

```bash
pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
pyinstaller --noconfirm --clean --onefile --windowed --name "空间查看器" main.py
```

产物在 `dist/空间查看器.exe`，单文件免安装，双击即用。

## AI 文件分析（可选功能）

> 程序**不会上传文件内容**，只把「文件名 / 大小 / 修改时间」的文字清单发送给你配置的 AI 服务，AI 凭文件名推断用途与归属，结果仅供参考。

- 打开「AI 文件分析」对话框后，点击 **AI 设置** 选择服务商预设（OpenAI / DeepSeek / Kimi / 智谱 / 通义千问），填入 API Key 与模型名
- 点击「开始分析」才发起请求（**纯手动触发**，程序不会自动调用）
- 可调参数：每批文件数、最多分析文件数；目录很大时递归收集每个子目录占用最大的文件
- API Key 明文保存在本机 `%APPDATA%\SpaceViewer\settings.json`，**不会上传到本仓库或任何远端**

## 运行测试

```bash
python -m unittest discover -s tests -v
```

110 项单元测试覆盖核心引擎、AI 分析链路（本地模拟服务器，不访问真实网络）、导出与界面冒烟。

## 目录结构

```
空间查看器/
├── main.py               # 程序入口
├── requirements.txt      # 依赖清单
├── app/
│   ├── core/             # 纯 Python 核心层（无 Qt 依赖）
│   │   ├── engine.py     # 扫描引擎（递归统计/暂停/停止）
│   │   ├── ai_client.py  # AI 分析客户端（OpenAI 兼容）
│   │   ├── exporter.py   # CSV / Excel 导出
│   │   └── ...
│   └── ui/               # Qt 界面层（PySide6）
│       ├── main_window.py# 主窗口
│       ├── ai_dialog.py  # AI 分析对话框
│       ├── charts.py     # 自绘条形图 / 环形图
│       └── ...
├── tests/                # 单元测试（110 项）
└── 截图/                 # 界面截图
```

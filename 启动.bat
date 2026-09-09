@echo off
chcp 65001 >nul
REM ==========================================================================
REM  空间查看器 - 一键启动脚本
REM  双击本文件即可运行；若提示缺少 PySide6，会自动用清华源安装依赖。
REM ==========================================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.10 及以上版本，
    echo        安装时勾选 "Add Python to PATH"。
    pause
    exit /b 1
)

python -c "import PySide6" >nul 2>nul
if errorlevel 1 (
    echo [提示] 首次运行，正在从清华源安装依赖，请稍候...
    python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
)

python main.py
if errorlevel 1 pause

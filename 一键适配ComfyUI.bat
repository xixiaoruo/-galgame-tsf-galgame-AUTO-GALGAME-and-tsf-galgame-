@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   自动化AI Galgame - 一键适配本机 ComfyUI（双击即用）
echo ============================================
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 未找到 python，请安装 Python 3.11+ 后重试
    pause
    exit /b 1
)
python "%~dp0测试工具\comfy_adapt.py" %*
if errorlevel 1 (
    echo [WARN] 适配未完全成功：可先手动启动 ComfyUI，再双击本脚本重试；游戏照常启动
    pause
)
echo.
echo [Next] 启动游戏...
start "" "%~dp0TSF_Galgame.exe"

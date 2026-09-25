@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo ============================================
echo   自动化AI Galgame 一键启动器
echo ============================================
echo.
echo [1/3] 检查 8765 端口占用...
set FOUND=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
    echo   - 发现占用进程 PID=%%p，结束旧实例（不影响当前对局存档）...
    taskkill /PID %%p /F /T >nul 2>nul
    set FOUND=1
)
if "%FOUND%"=="0" (
    echo   - 端口空闲
)
echo.
echo [2/3] 启动游戏服务（新窗口）...
start "自动化AI Galgame" /D "%~dp0" python server.py
timeout /t 4 /nobreak >nul
echo.
echo [3/3] 检测服务状态...
curl -s -o nul -w "   http://127.0.0.1:8765 -> %%{http_code}" http://127.0.0.1:8765/
echo.
echo.
echo 完成！浏览器将自动打开游戏页面
echo （若未自动打开，请访问 http://127.0.0.1:8765 ）
start http://127.0.0.1:8765
timeout /t 2 /nobreak >nul

@echo off
chcp 65001 >nul
setlocal
rem ============================================================
rem  自动化AI Galgame 启动器（新版：原生窗口，不开浏览器）
rem  指向 dist\TSF_Galgame\TSF_Galgame.exe（onedir，必须整目录）
rem  旧版（浏览器模式）保留为  start-旧版浏览器模式.bat
rem ============================================================

set "ROOT=%~dp0"
set "TARGET=%ROOT%dist\TSF_Galgame\TSF_Galgame.exe"

echo.
echo ============================================
echo   自动化AI Galgame  （原生窗口版）
echo ============================================
echo.

if not exist "%TARGET%" goto :missing

echo 正在启动（原生窗口，不会自动打开浏览器）...
echo.
start "" /D "%ROOT%dist\TSF_Galgame" "%TARGET%"
exit /b 0

:missing
echo [错误] 找不到新版 exe：
echo        %TARGET%
echo.
echo 请确认整个目录都在（不能只复制 exe 文件）：
echo        dist\TSF_Galgame\TSF_Galgame.exe
echo        dist\TSF_Galgame\_internal\...
echo.
pause
exit /b 1

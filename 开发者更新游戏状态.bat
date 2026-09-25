@echo off
rem 本脚本刻意保持纯 ASCII：UTF-8 的 .bat 配上 chcp 65001 会让 cmd 误解码后续行；
rem 括号块内的 echo 文本也不能含裸括号（会被当成块定界符）。故用标签结构。
rem 目标路径写成 "%~dp0."：%~dp0 自带结尾反斜杠，写成 "%~dp0" 会把闭合引号
rem 转义掉（\"），导致 /E /XF 等开关被吞进目标路径（robocopy 报错 123/16）。
setlocal
cd /d "%~dp0"
title Update Game State

echo ============================================
echo   Automated AI Galgame - Update Game State
echo ============================================
echo.

if exist "%~dp0update\staging\TSF_Galgame.exe" goto apply_staged
if exist "%~dp0update\TSF_Galgame.exe" goto apply_exe

echo [ERROR] No update package found.
echo         Players: download it first in game: Settings - Update from GitHub
echo         Devs:    put the new TSF_Galgame.exe into update\ and rerun
echo.
pause
exit /b 1

:apply_staged
echo [1/4] Stopping running game process...
taskkill /IM TSF_Galgame.exe /F >nul 2>nul
%SystemRoot%\System32\ping.exe -n 2 127.0.0.1 >nul
echo [2/4] Applying full update package: program + web + docs...
set APPLY_TRIES=0
:apply_pkg
robocopy "%~dp0update\staging" "%~dp0." /E /XF config.json /XD data update /NFL /NDL /NJH /NJS /R:2 /W:2 >nul
if not errorlevel 8 goto staged_ok
set /a APPLY_TRIES+=1
if %APPLY_TRIES% geq 3 goto apply_fail
echo [WARN] Some files were busy, retrying...
%SystemRoot%\System32\ping.exe -n 3 127.0.0.1 >nul
goto apply_pkg

:staged_ok
echo [3/4] Cleaning up package...
rmdir /s /q "%~dp0update\staging" >nul 2>nul
goto launch

:apply_exe
echo [1/4] Stopping running game process...
taskkill /IM TSF_Galgame.exe /F >nul 2>nul
%SystemRoot%\System32\ping.exe -n 2 127.0.0.1 >nul
echo [2/4] Replacing game executable...
copy /y "%~dp0update\TSF_Galgame.exe" "%~dp0TSF_Galgame.exe" >nul
if errorlevel 1 goto apply_fail
echo [3/4] Cleaning up package...
del "%~dp0update\TSF_Galgame.exe" >nul 2>nul
goto launch

:apply_fail
echo [ERROR] Copy failed - a file may be busy. Close all game windows and retry,
echo         or right-click this file and choose "Run as administrator".
pause
exit /b 1

:launch
echo [4/4] Starting new version...
start "" "%~dp0TSF_Galgame.exe"
echo.
echo Done. Saves / CGs / config live in D:\TSF_Galgame_Data and are untouched.
%SystemRoot%\System32\ping.exe -n 3 127.0.0.1 >nul

@echo off
rem Keep this file pure ASCII: a UTF-8 .bat with chcp 65001 makes cmd mis-decode
rem the following lines, and bare parentheses inside a bracketed block are read
rem as block delimiters. Hence: ASCII only, and labels instead of blocks.
rem The destination MUST be written as %~dp0. -- %~dp0 ends with a backslash,
rem so "%~dp0" escapes the closing quote and swallows /E /XF into the path.
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

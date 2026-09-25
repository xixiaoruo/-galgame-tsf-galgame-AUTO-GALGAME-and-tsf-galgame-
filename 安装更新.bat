@echo off
setlocal
echo ============================================
echo   自动化AI Galgame - Install Update
echo ============================================
if not exist "%~dp0update\TSF_Galgame.exe" (
    echo [ERROR] No update package found in update\TSF_Galgame.exe
    pause
    exit /b 1
)
echo [1/4] Stopping running game process...
taskkill /IM TSF_Galgame.exe /F >nul 2>nul
%SystemRoot%\System32\ping.exe -n 2 127.0.0.1 >nul
echo [2/4] Replacing game program...
copy /y "%~dp0update\TSF_Galgame.exe" "%~dp0TSF_Galgame.exe" >nul
if errorlevel 1 (
    echo [ERROR] Replace failed - file may be locked.
    echo Try again after closing all game windows, or
    echo right-click this file - "Run as administrator".
    pause
    exit /b 1
)
echo [3/4] Cleaning update package...
del "%~dp0update\TSF_Galgame.exe" >nul 2>nul
echo [4/4] Starting new version...
start "" "%~dp0TSF_Galgame.exe"
echo.
echo DONE. New version is starting.
%SystemRoot%\System32\ping.exe -n 2 127.0.0.1 >nul

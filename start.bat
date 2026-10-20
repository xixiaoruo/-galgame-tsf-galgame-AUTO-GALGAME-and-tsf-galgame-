@echo off
setlocal
cd /d "%~dp0"
set "ROOT=%~dp0"
if not exist "%ROOT%start_current.ps1" goto :missing_script
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%start_current.ps1"
exit /b %errorlevel%

:missing_script
echo [ERROR] start_current.ps1 is missing.
pause
exit /b 1

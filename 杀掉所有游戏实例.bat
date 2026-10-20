@echo off
rem ============================================================================
rem  杀掉所有游戏实例（便携）
rem ----------------------------------------------------------------------------
rem  用途：替换/更新 TSF_Galgame.exe 之前必须先释放文件锁；否则会报
rem        "文件正由另一进程使用，因此该进程无法访问此文件"。
rem  行为：终止全部 TSF_Galgame.exe 及其子进程。
rem  安全：只按项目 exe 名称匹配，不删任何文件、不碰存档；不会全局终止
rem        其他应用的 QtWebEngineProcess.exe。存档在独立数据目录里。
rem  用法：双击即可。也可在命令行加 /WAIT 在杀掉后暂停（默认就暂停）。
rem ============================================================================
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo.
echo   关闭所有 TSF_Galgame 实例…
echo   ------------------------------------------------------------

set /a N=0
for /f "tokens=*" %%P in ('tasklist /fi "imagename eq TSF_Galgame.exe" /nh /fo csv 2^>nul ^| findstr /i "TSF_Galgame.exe"') do (
  set /a N+=1
)

taskkill /f /im TSF_Galgame.exe >nul 2>&1
if errorlevel 1 (
  if !N!==0 (
    echo   [跳过] 没有正在运行的 TSF_Galgame 实例
  ) else (
    echo   [注意] 检测到 !N! 个实例但终止失败（可能需要管理员权限）
  )
) else (
  echo   [完成] 已请求终止全部 TSF_Galgame 实例
)

rem 等待文件锁真正释放（进程退出与句柄释放之间有间隙）。
rem 用 ping 而不是 timeout：timeout 在输出被重定向时会报
rem "Input redirection is not supported" 并立刻跳过等待。
ping -n 3 127.0.0.1 >nul 2>&1

echo   ------------------------------------------------------------
echo   现在可以安全替换 TSF_Galgame.exe 了。
echo.

if /i "%~1"=="/NOWAIT" goto :eof
echo   按任意键关闭此窗口…
pause >nul
endlocal

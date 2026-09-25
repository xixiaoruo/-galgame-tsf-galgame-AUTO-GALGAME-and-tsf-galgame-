@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 开发者更新游戏状态

echo ============================================
echo   自动化AI Galgame - 更新游戏状态
echo ============================================
echo.

if not exist "%~dp0update\staging\TSF_Galgame.exe" if not exist "%~dp0update\TSF_Galgame.exe" (
    echo [ERROR] 没有找到更新包。
    echo         玩家：先在游戏内「设置 - 从 GitHub 更新」下载，再运行本脚本
    echo         开发者：把新的 TSF_Galgame.exe 放进 update\ 后，再运行本脚本
    echo.
    pause
    exit /b 1
)

echo [1/4] 关闭正在运行的游戏进程...
taskkill /IM TSF_Galgame.exe /F >nul 2>nul
%SystemRoot%\System32\ping.exe -n 2 127.0.0.1 >nul

if exist "%~dp0update\staging\TSF_Galgame.exe" (
    echo [2/4] 应用完整更新包（程序 + 界面 + 文档；保留个人配置与素材）...
    robocopy "%~dp0update\staging" "%~dp0" /E /XF config.json /XD data update 角色库 /NFL /NDL /NJH /NJS /R:1 /W:1 >nul
    if errorlevel 8 (
        echo [ERROR] 覆盖失败：文件可能被占用，或需要以管理员身份运行。
        pause
        exit /b 1
    )
    echo [3/4] 清理更新包...
    rmdir /s /q "%~dp0update\staging" >nul 2>nul
) else (
    echo [2/4] 替换程序文件...
    copy /y "%~dp0update\TSF_Galgame.exe" "%~dp0TSF_Galgame.exe" >nul
    if errorlevel 1 (
        echo [ERROR] 替换失败：文件可能被占用；请关闭所有游戏窗口后重试，
        echo         或右键本文件选择「以管理员身份运行」。
        pause
        exit /b 1
    )
    echo [3/4] 清理更新包...
    del "%~dp0update\TSF_Galgame.exe" >nul 2>nul
)

echo [4/4] 启动新版本...
start "" "%~dp0TSF_Galgame.exe"
echo.
echo 完成，新版本正在启动。
echo 存档、CG 与配置都在独立数据目录（D:\TSF_Galgame_Data），不受影响。
%SystemRoot%\System32\ping.exe -n 3 127.0.0.1 >nul

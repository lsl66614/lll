@echo off
title OCS AI 网关
cd /d "%~dp0"

set "PORT=8000"
set "ADMIN=http://127.0.0.1:%PORT%/admin"

echo ================================================
echo    OCS AI 网关
echo    管理台: %ADMIN%
echo ================================================
echo.

if not exist "main.py" (
    echo [错误] 当前目录没有 main.py，请先完整解压压缩包。
    pause
    exit /b 1
)

set "UV="

if exist "%~dp0uv.exe" (
    set "UV=%~dp0uv.exe"
    echo [信息] 使用文件夹内的 uv.exe
) else (
    where uv >nul 2>nul
    if not errorlevel 1 (
        set "UV=uv"
        echo [信息] 使用系统已安装的 uv
    ) else (
        if exist "%USERPROFILE%\.local\bin\uv.exe" (
            set "UV=%USERPROFILE%\.local\bin\uv.exe"
            echo [信息] 使用用户目录里的 uv
        )
    )
)

if not defined UV (
    echo [错误] 没有找到 uv，缺了它无法启动。
    echo.
    echo 方法一 绿色便携：
    echo   下载 https://github.com/astral-sh/uv/releases/latest
    echo   在 Assets 里下载 uv-x86_64-pc-windows-msvc.zip
    echo   解压出 uv.exe，放到这个目录：
    echo   %CD%
    echo.
    echo 方法二 安装到系统：
    echo   打开 PowerShell，执行下面这一行：
    echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 ^| iex"
    echo   装完关掉终端重开，再双击本脚本。
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    if not exist ".env.example" (
        echo [错误] 缺少 .env 与 .env.example，压缩包不完整。
        pause
        exit /b 1
    )
    copy ".env.example" ".env" >nul
    echo [提示] 已生成 .env，默认值即可，AI 的 Key 在网页里填。
)

netstat -ano | findstr ":%PORT%" | findstr /i "LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo [提示] 端口 %PORT% 已被占用，可能已经在运行。
    echo        正在打开管理台...
    start "" "%ADMIN%"
    echo.
    pause
    exit /b 0
)

if not exist ".venv" (
    echo [1/2] 首次运行，正在下载 Python 和依赖，需要几分钟。
    echo        请勿关闭窗口。慢了可以先执行：
    echo        set UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
    echo.
    "%UV%" sync
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败，请检查网络后重新双击。
        pause
        exit /b 1
    )
    echo.
)

echo [2/2] 正在启动，稍后自动打开管理台。
echo        保持窗口开启，关掉即停止服务。
echo.
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process '%ADMIN%'"
echo ------------------------------------------------
echo.

"%UV%" run python main.py

echo.
echo 服务已停止。
pause

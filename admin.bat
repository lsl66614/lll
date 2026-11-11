@echo off
title 打开 OCS AI 管理台

set "PORT=8000"
set "ADMIN=http://127.0.0.1:%PORT%/admin"

netstat -ano | findstr ":%PORT%" | findstr /i "LISTENING" >nul 2>nul
if not errorlevel 1 (
    start "" "%ADMIN%"
    exit /b 0
)

echo ================================================
echo    网关还没有启动
echo ================================================
echo.
echo 请先双击「start.bat」，等窗口里出现
echo   Uvicorn running on http://127.0.0.1:%PORT%
echo 之后再双击本脚本。
echo.
echo 现在为你尝试启动网关...
echo.
start "" "%~dp0start.bat"

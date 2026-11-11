@echo off
cd /d "%~dp0"
set "PORT=8000"
set "ADMIN=http://127.0.0.1:%PORT%/admin"
set "SILENT=0"
if /i "%~1"=="--silent" set "SILENT=1"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. Please run start.bat once to install dependencies.
  pause
  exit /b 1
)

netstat -ano | findstr ":%PORT%" | findstr /i "LISTENING" >nul 2>nul
if not errorlevel 1 (
  if "%SILENT%"=="0" start "" "%ADMIN%"
  exit /b 0
)

if "%SILENT%"=="0" start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:8000/admin'"
echo ================================================
echo   OCS AI Gateway  %ADMIN%
echo   Close this window to stop the gateway.
echo ================================================
".venv\Scripts\python.exe" main.py
pause

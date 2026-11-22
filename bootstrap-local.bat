@echo off
rem One-click: stop services -> init embedded PostgreSQL / db / DDL / deps -> start services
rem Log: .runtime\bootstrap.log
setlocal EnableExtensions
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [1/3] Stopping old services ...
set SILENT=1
call "%~dp0stop-services.bat" >nul 2>&1

echo [2/3] Init database and dependencies (first run 1-3 min) ...
"%PY%" "%~dp0scripts\bootstrap_local.py"
if errorlevel 1 (
  echo.
  echo FAILED. See .runtime\bootstrap.log
  pause
  exit /b 1
)

echo [3/3] Starting services ...
call "%~dp0start-silent.bat"
echo Done. Open http://127.0.0.1:8101/console/automodel.html in about 30 seconds.
timeout /t 8 >nul

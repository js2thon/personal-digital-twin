@echo off
rem Install automodel dependencies into project .venv, then restart API
setlocal EnableExtensions
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo .venv not found: %PY%
  pause
  exit /b 1
)
echo [1/2] Installing pandas pyyaml openpyxl pytest into .venv ...
"%PY%" -m pip install pandas pyyaml openpyxl pytest
if errorlevel 1 (
  echo pip install FAILED
  pause
  exit /b 1
)
echo [2/2] Restarting API on 8101 (supervisor restarts it in ~10s) ...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8101" ^| findstr "LISTENING"') do taskkill /f /pid %%P >nul 2>&1
echo Done. Wait about 30 seconds.
timeout /t 8 >nul

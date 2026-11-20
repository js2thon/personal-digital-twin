@echo off
rem ============================================================
rem  本地部署健康自检（只读，不改变服务状态）
rem  双击运行；全绿即可用浏览器打开 http://127.0.0.1:8101/console/
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul

set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY for %%P in (python.exe) do if not defined PY set "PY=%%~$PATH:P"

if not defined PY (
  echo [ERROR] 未找到可用的 python.exe
  pause
  exit /b 1
)

"%PY%" "%~dp0check_health.py"
echo.
pause

@echo off
rem ============================================================
rem  Silent launcher for Personal Digital Twin
rem  Double-click to start services in background (no console window).
rem  Logs : .runtime\uvicorn.out.log / uvicorn.err.log / pg-stderr.log
rem  Stop : stop-services.bat
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "PYW="
if exist "%~dp0.venv\Scripts\pythonw.exe" set "PYW=%~dp0.venv\Scripts\pythonw.exe"
rem PATH 里的 pythonw 常是 WorkBuddy 内置 3.13（没装 uvicorn），故先试系统 3.12/3.11
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
if not defined PYW for %%P in (pythonw.exe) do if not defined PYW set "PYW=%%~$PATH:P"

if defined PYW (
  start "" "%PYW%" "%~dp0start_hidden.py"
) else (
  start "" /min python "%~dp0start_hidden.py"
)
exit

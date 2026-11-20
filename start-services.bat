@echo off
setlocal EnableExtensions
title Personal Digital Twin - Services
cd /d "%~dp0"
set "ROOT=%~dp0"
set "PGBIN=%ROOT%.runtime\pgsql\bin"
set "PGDATA=%ROOT%.runtime\pgdata"
set "PGLOG=%ROOT%.runtime\pg.log"
set "UVLOG=%ROOT%.runtime\uvicorn.log"
rem Local calls must never go through a corporate proxy.
set "NO_PROXY=127.0.0.1,localhost"
set "no_proxy=127.0.0.1,localhost"
rem Daemon mode: "start-services.bat daemon" auto-restarts uvicorn if it exits.
rem Silencer:    "start-services.bat daemon silent" never pauses waiting for a key
rem              (used by start-silent.vbs, which runs the window hidden).
set "DAEMON="
set "SILENT="
if /i "%~1"=="daemon" set "DAEMON=1"
if /i "%~2"=="silent" set "SILENT=1"

echo ============================================================
echo  Personal Digital Twin - local services
echo  Console : http://127.0.0.1:8101/
echo  Keep this window open while using the console.
echo ============================================================

echo [1/3] PostgreSQL 127.0.0.1:5434
netstat -ano | findstr ":5434" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
  echo       already running, skip
) else (
  echo       starting ... crash recovery can take 1-3 minutes
  "%PGBIN%\pg_ctl.exe" -D "%PGDATA%" -l "%PGLOG%" -o "-p 5434 -h 127.0.0.1" -w -t 300 start
  if errorlevel 1 (
    echo       FAILED to start PostgreSQL, see %PGLOG%
    goto :FAIL
  )
)

echo [2/3] Loading .env
for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ROOT%.env") do (
  if not "%%A"=="" set "%%A=%%B"
)
set "CELERY_TASK_ALWAYS_EAGER=1"

rem If another instance is serving 8101 correctly, this script has nothing to do.
rem If the port is held by a dead/session-bound instance, wait and take over when
rem it exits (no fixed timeout: the earlier 10-minute give-up caused outages).
set /a WAITED=0
:WAIT8101
netstat -ano | findstr ":8101" | findstr "LISTENING" >nul 2>&1
if %errorlevel% NEQ 0 goto :PORTFREE
curl.exe -s --noproxy "*" --max-time 3 http://127.0.0.1:8101/healthz 2>nul | findstr /C:"status" >nul 2>&1
if %errorlevel%==0 (
  echo       8101 is already serving a healthy console. Nothing to start.
  echo       Console: http://127.0.0.1:8101/
  goto :EOF
)
set /a WAITED+=1
echo       8101 busy but not healthy, standby ... (check %WAITED%)
rem ping 代替 timeout：隐藏窗口下没有控制台输入，timeout 会报错退出
ping -n 6 127.0.0.1 >nul 2>&1
goto :WAIT8101
:PORTFREE

echo [3/3] Starting API + Console on 8101
cd /d "%ROOT%backend"
rem Resolve a Python that actually has uvicorn + fastapi installed.
set "PYEXE="
if exist "%ROOT%.venv\Scripts\python.exe" set "PYEXE=%ROOT%.venv\Scripts\python.exe"
if not defined PYEXE (
  for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PYEXE (
      "%%P" -c "import uvicorn, fastapi" >nul 2>&1 && set "PYEXE=%%P"
    )
  )
)
if not defined PYEXE (
  echo [ERROR] 未找到 Python 解释器：请先创建 .venv\Scripts\python.exe，或把 python 加入 PATH。 >&2
  exit /b 1
)
echo       python: %PYEXE%
echo       logs:   %UVLOG%
:RUNUV
echo [%date% %time%] uvicorn starting (daemon=%DAEMON%) >> "%UVLOG%"
"%PYEXE%" -m uvicorn app.main:app --host 127.0.0.1 --port 8101 --no-proxy-headers >> "%UVLOG%" 2>&1
echo [%date% %time%] uvicorn exited with code %errorlevel% >> "%UVLOG%"
if defined DAEMON (
  echo       uvicorn exited, restarting in 10s ... ^(see %UVLOG%^)
  ping -n 11 127.0.0.1 >nul 2>&1
  goto :WAIT8101
)
echo.
echo uvicorn has stopped. Press any key to close this window.
if not defined SILENT pause >nul
goto :EOF

:FAIL
echo.
echo Startup failed. Open %PGLOG% for details.
if not defined SILENT pause

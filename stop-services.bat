@echo off
setlocal EnableExtensions
title Personal Digital Twin - Stop
cd /d "%~dp0"
set "ROOT=%~dp0"
set "PGBIN=%ROOT%.runtime\pgsql\bin"
set "PGDATA=%ROOT%.runtime\pgdata"

echo [1/2] Stopping API on 8101 ...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8101" ^| findstr "LISTENING"') do (
  echo       kill pid %%P
  taskkill /f /pid %%P >nul 2>&1
)

echo [2/2] Stopping PostgreSQL 5434 ...
"%PGBIN%\pg_ctl.exe" -D "%PGDATA%" -m fast -w -t 60 stop >nul 2>&1
if errorlevel 1 (
  echo       graceful stop failed, force killing postgres ...
  taskkill /f /im postgres.exe >nul 2>&1
)

echo Done. Both services stopped.
if not defined SILENT pause

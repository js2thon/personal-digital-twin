@echo off
rem ============================================================
rem  停用「开机自启」——删除启动文件夹里的自启项，
rem  不影响当前正在运行的服务，不改任何配置。
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS=%STARTUP%\PersonalTwinServices.vbs"

if exist "%VBS%" (
  del /f /q "%VBS%" >nul 2>&1
  if exist "%VBS%" (
    echo [ERROR] 删除失败，请检查权限。
  ) else (
    echo [OK] 已停用开机自启（下次登录不再自动启动）。
    echo      当前正在运行的服务不受影响；如需停止请运行 stop-services.bat
  )
) else (
  echo [INFO] 开机自启本来就未启用，无需操作。
)
echo.
pause

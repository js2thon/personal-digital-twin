@echo off
rem ============================================================
rem  确认「开机自启」处于启用状态——仅本机自用
rem
rem  本机的自启项是启动文件夹里的 PersonalTwinServices.vbs（登录时
rem  静默调用 start-services.bat daemon 拉起 PG 5434 + API 8101）。
rem  本脚本复用它，不另建第二个自启项，避免双守护进程。
rem
rem  关闭：运行 disable-autostart.bat
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS=%STARTUP%\PersonalTwinServices.vbs"
set "SRCVBS=%~dp0deploy\PersonalTwinServices.vbs"

if not exist "%STARTUP%" (
  echo [ERROR] 未找到启动文件夹：%STARTUP%
  pause
  exit /b 1
)

if exist "%VBS%" (
  echo [OK] 开机自启已启用（检测到现有自启项）。
  echo      项：%VBS%
  echo      无需重复添加。下次登录将静默启动本地服务。
) else (
  if not exist "%SRCVBS%" (
    echo [ERROR] 未找到待安装的自启模板：%SRCVBS%
    echo         请确认 deploy 目录完整。
    pause
    exit /b 1
  )
  copy /y "%SRCVBS%" "%VBS%" >nul 2>&1
  if exist "%VBS%" (
    echo [OK] 已启用开机自启。
    echo      项：%VBS%
    echo      下次登录将静默启动 PG 5434 + API 8101。
  ) else (
    echo [ERROR] 复制失败，请检查权限（可能需要管理员权限）。
  )
)
echo.
pause

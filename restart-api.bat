@echo off
rem Restart API on 8101 only (supervisor in start_hidden.py brings it back in ~10s)
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8101" ^| findstr "LISTENING"') do taskkill /f /pid %%P >nul 2>&1
echo API restarting, wait about 20 seconds.
timeout /t 5 >nul

' Personal Digital Twin - hidden auto-start daemon
' Runs start-services.bat (daemon mode) with no visible window at logon.
' Logs: .runtime\uvicorn.log and .runtime\pg.log inside the project folder.
' To stop services: use Task Manager to end "python.exe" running uvicorn
' and "postgres.exe", or delete this file from the Startup folder.
' 使用前请把 <PROJECT_ROOT> 替换为本机项目绝对路径（不要提交含本机用户名的路径）。
' 例如：root = "D:\work\personal-digital-twin"
Set fso = CreateObject("Scripting.FileSystemObject")
root = "<PROJECT_ROOT>"
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd /c """"" & root & "\start-services.bat"" daemon""", 0, False

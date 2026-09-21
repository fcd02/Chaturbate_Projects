@echo off
setlocal
cd /d "%~dp0"
if not exist mobile_server.pid (
  echo The mobile reviewer does not appear to be running.
  pause
  exit /b 0
)
set /p PID=<mobile_server.pid
taskkill /PID %PID% /T /F
if exist mobile_server.pid del /q mobile_server.pid

echo.
echo The Python server was stopped. Tailscale Serve remains configured,
echo so the same private URL will work again after restarting the server.
pause

@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (set "PY=py") else (set "PY=python")

if exist mobile_server.pid (
  set /p OLD_PID=<mobile_server.pid
  tasklist /FI "PID eq %OLD_PID%" 2>nul | find "%OLD_PID%" >nul
  if not errorlevel 1 exit /b 0
  del /q mobile_server.pid >nul 2>nul
)

start "CTBRec Mobile Reviewer" /min %PY% -u "%~dp0ctbrec_mobile_server.py"
exit /b 0

@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Apply-ChatGPT-Update.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo CTBRec update exited with code %RC%.
pause
exit /b %RC%

@echo off
setlocal
cd /d "%~dp0"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "TARGET=%STARTUP%\CTBRec_Mobile_Reviewer.cmd"
>"%TARGET%" echo @echo off
>>"%TARGET%" echo call "%~dp0start_server_only.bat"
if errorlevel 1 (
  echo Could not create the Windows Startup entry.
) else (
  echo Created:
  echo %TARGET%
  echo.
  echo The local reviewer will start whenever you sign into Windows.
  echo Tailscale Serve persists after it has been enabled once.
)
pause

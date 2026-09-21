@echo off
setlocal
cd /d "%~dp0"

for /f %%P in ('powershell -NoProfile -Command "$c=Get-Content -Raw ''%~dp0mobile_config.json'' ^| ConvertFrom-Json; if($c.port){$c.port}else{8787}"') do set "PORT=%%P"
if not defined PORT set "PORT=8787"

call "%~dp0start_server_only.bat"
timeout /t 2 /nobreak >nul

set "TS=tailscale.exe"
where tailscale.exe >nul 2>nul
if errorlevel 1 (
  if exist "C:\Program Files\Tailscale\tailscale.exe" (
    set "TS=C:\Program Files\Tailscale\tailscale.exe"
  ) else (
    echo.
    echo Tailscale was not found. Install it on the PC, sign in, and run
    echo this file again. The local dashboard will still open for testing.
    start "" "http://127.0.0.1:%PORT%"
    pause
    exit /b 1
  )
)

echo.
echo Publishing the LOCAL-ONLY server privately through Tailscale Serve...
"%TS%" serve --bg --yes %PORT%
echo.
echo Your private iPhone URL is shown below:
"%TS%" serve status

echo.
echo The first Serve setup can open a Tailscale consent page to enable HTTPS.
echo Approve it, then rerun this file if the URL was not displayed.
echo.
start "" "http://127.0.0.1:%PORT%"
pause

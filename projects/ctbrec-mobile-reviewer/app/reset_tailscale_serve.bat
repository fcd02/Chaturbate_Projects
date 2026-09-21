@echo off
setlocal
set "TS=tailscale.exe"
where tailscale.exe >nul 2>nul
if errorlevel 1 set "TS=C:\Program Files\Tailscale\tailscale.exe"
echo WARNING: This clears ALL Tailscale Serve rules configured on this PC.
choice /M "Continue"
if errorlevel 2 exit /b 0
"%TS%" serve reset
pause

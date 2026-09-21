@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3.10 "%~dp0bridge.py" setup %*
  set "RC=%ERRORLEVEL%"
) else (
  where python >nul 2>nul
  if not %ERRORLEVEL%==0 (
    echo Python was not found. Install Python 3.10+ or add it to PATH.
    set "RC=9009"
  ) else (
    python "%~dp0bridge.py" setup %*
    set "RC=%ERRORLEVEL%"
  )
)

echo.
if not "%RC%"=="0" echo Discovery Git Bridge setup exited with code %RC%.
pause
exit /b %RC%

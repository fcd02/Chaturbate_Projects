@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo CTBRec Mobile Reviewer - one-time Python setup
echo ============================================================

where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py"
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo.
    echo Python was not found. Install 64-bit Python 3.11 or newer,
    echo enable "Add Python to PATH", then run this file again.
    pause
    exit /b 1
  )
  set "PY=python"
)

%PY% -m pip install --upgrade pip
if errorlevel 1 goto :failed
%PY% -m pip install -r requirements.txt
if errorlevel 1 goto :failed

%PY% mobile_self_test.py
if errorlevel 1 goto :failed

echo.
echo Setup finished. Next, install/sign into Tailscale on this PC and
 echo your iPhone, then run start_mobile_reviewer.bat.
pause
exit /b 0

:failed
echo.
echo Setup failed. Review the message above and mobile_reviewer.log.
pause
exit /b 1

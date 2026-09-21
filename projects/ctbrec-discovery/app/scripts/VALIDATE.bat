@echo off
setlocal
cd /d "%~dp0.."

set "PYTHON_CMD=python"
where py >nul 2>&1
if %ERRORLEVEL%==0 set "PYTHON_CMD=py -3.10"

%PYTHON_CMD% validation\runner.py --mode auto
if errorlevel 1 goto :fail

echo.
echo ALL CURRENT-RELEASE DISCOVERY VALIDATION PASSED
pause
exit /b 0

:fail
echo.
echo CURRENT-RELEASE VALIDATION FAILED
pause
exit /b 1

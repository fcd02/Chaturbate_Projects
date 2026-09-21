@echo off
setlocal
cd /d "%~dp0\.."
where py >nul 2>nul && (py -3 -m discovery.check_config --config discovery_config.json) || (python -m discovery.check_config --config discovery_config.json)
echo.
pause

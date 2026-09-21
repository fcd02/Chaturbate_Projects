@echo off
setlocal
cd /d "%~dp0.."
if not exist discovery_config.json (
  copy /Y discovery_config.example.json discovery_config.json >nul
  echo Created discovery_config.json. Edit mobile_catalog_cache before relying on automatic import.
)
py -3.10 -m discovery.server --config discovery_config.json
if errorlevel 1 python -m discovery.server --config discovery_config.json
pause

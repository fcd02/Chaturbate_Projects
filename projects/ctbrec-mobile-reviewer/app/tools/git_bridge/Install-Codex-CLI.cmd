@echo off
setlocal

echo ============================================================
echo Codex CLI installer - avoids PowerShell npm.ps1 policy issue
echo ============================================================
echo.

where node.exe >nul 2>nul
if errorlevel 1 (
  echo Node.js was not found.
  echo Install it first with:
  echo   winget install --id OpenJS.NodeJS.LTS -e --source winget
  pause
  exit /b 1
)

where npm.cmd >nul 2>nul
if errorlevel 1 (
  echo npm.cmd was not found even though Node.js appears installed.
  echo Close this window, reopen a terminal, and try again.
  pause
  exit /b 1
)

echo Installing/updating Codex CLI...
call npm.cmd install -g @openai/codex@latest
if errorlevel 1 (
  echo.
  echo Codex installation failed.
  pause
  exit /b 1
)

set "PATH=%PATH%;%APPDATA%\npm"
where codex.cmd >nul 2>nul
if errorlevel 1 (
  if exist "%APPDATA%\npm\codex.cmd" (
    set "CODEX=%APPDATA%\npm\codex.cmd"
  ) else (
    echo.
    echo Codex installed, but codex.cmd is not currently on PATH.
    echo Close/reopen PowerShell and run: codex --version
    pause
    exit /b 0
  )
) else (
  set "CODEX=codex.cmd"
)

echo.
call "%CODEX%" --version
if errorlevel 1 (
  echo Codex installed but could not start.
  echo Try: codex doctor
  pause
  exit /b 1
)

echo.
echo Installation succeeded.
echo Next run:
echo   codex --login
echo and choose Sign in with ChatGPT.
echo.
pause
exit /b 0

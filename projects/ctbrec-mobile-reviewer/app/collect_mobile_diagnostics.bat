@echo off
setlocal
cd /d "%~dp0"
echo.
echo CTBRec Mobile Reviewer diagnostics collector
echo ============================================
echo This excludes Recu session/browser-profile data and redacts obvious PIN/secret/token/cookie/password fields.
echo Runtime logs/caches can still contain model names and local filesystem paths.
echo.
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 collect_mobile_diagnostics.py
) else (
  python collect_mobile_diagnostics.py
)
echo.
echo Upload the newly created CTBRec_Mobile_Reviewer_Diagnostics_*.zip file to ChatGPT.
pause

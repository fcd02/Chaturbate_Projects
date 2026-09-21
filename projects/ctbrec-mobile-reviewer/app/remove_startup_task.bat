@echo off
setlocal
set "TARGET=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\CTBRec_Mobile_Reviewer.cmd"
if exist "%TARGET%" (
  del /q "%TARGET%"
  echo Removed the CTBRec Mobile Reviewer Startup entry.
) else (
  echo No CTBRec Mobile Reviewer Startup entry was found.
)
pause

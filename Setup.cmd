@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1" -Mode setup
set "KAIZO_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %KAIZO_EXIT%

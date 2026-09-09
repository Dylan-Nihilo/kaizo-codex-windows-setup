@echo off
setlocal
if exist "%LOCALAPPDATA%\KAIZO-Setup\Logs" start "" explorer.exe "%LOCALAPPDATA%\KAIZO-Setup\Logs"
if exist "%~dp0logs" start "" explorer.exe "%~dp0logs"
if exist "%LOCALAPPDATA%\KAIZO-Setup\Logs" exit /b 0
if exist "%~dp0logs" exit /b 0
echo No log folder found. Run Setup.cmd first.
pause

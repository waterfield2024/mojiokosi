@echo off
setlocal
cd /d "%~dp0"
call bootstrap.cmd %*
if errorlevel 1 (
  pause
  exit /b 1
)

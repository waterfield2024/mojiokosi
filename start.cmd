@echo off
setlocal
cd /d "%~dp0"
call bootstrap.cmd --ensure --launch %*
if errorlevel 1 (
  pause
  exit /b 1
)

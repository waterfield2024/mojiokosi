@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
if /i "%PROCESSOR_ARCHITECTURE%"=="AMD64" goto architecture_ok
if /i "%PROCESSOR_ARCHITEW6432%"=="AMD64" goto architecture_ok
echo Windows 10 / 11 の x64 版が必要です。
exit /b 1

:architecture_ok
set "WHISPER_SETUP_PYTHON=.venv\Scripts\python.exe"
call :check_python
if not errorlevel 1 goto ready
set "WHISPER_SETUP_PYTHON=.python\runtime\python.exe"
call :check_python
if not errorlevel 1 goto ready
set "WHISPER_SETUP_PYTHON=.python\3.12.10\python.exe"
call :check_python
if not errorlevel 1 goto ready
py -3.12 -I -c "import sys,struct,tkinter,venv;assert sys.version_info[:2]==(3,12) and struct.calcsize('P')==8" >nul 2>&1
if not errorlevel 1 goto system_python
set "WHISPER_SETUP_PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
call :check_python
if not errorlevel 1 goto ready

echo Python 3.12 をこのフォルダー内に自動で準備します。初回のみダウンロードします。
rem Official full runtime (including Tcl/Tk), pinned from python.org/ftp/python/index-windows.json.
rem Standard inline PowerShell commands; no execution-policy or Windows protection changes.
powershell.exe -NoProfile -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; function Get-ArchiveHash($path){$stream=[IO.File]::OpenRead($path); $digest=[Security.Cryptography.SHA256]::Create(); try {[BitConverter]::ToString($digest.ComputeHash($stream)).Replace('-','')} finally {$stream.Dispose(); $digest.Dispose()}}; $root=(Get-Location).Path; $cache=Join-Path $root '.cache\downloads'; $parent=Join-Path $root '.python'; New-Item -ItemType Directory -Force -Path $cache,$parent | Out-Null; $zip=Join-Path $cache 'python-3.12.10-amd64.zip'; $hash='8649692de846c56a7189d6dae5c322ab20deb1b5908b6f39426b62a36f39415d'; $target=Join-Path $parent '3.12.10'; if(Test-Path -LiteralPath $target){throw 'The local Python folder is incomplete. Rename .python\3.12.10 and retry.'}; if(!(Test-Path -LiteralPath $zip) -or (Get-ArchiveHash $zip) -ne $hash){$partial=$zip+'.download-'+[guid]::NewGuid().ToString('N'); try {Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.zip' -OutFile $partial -TimeoutSec 120; if((Get-ArchiveHash $partial) -ne $hash){throw 'Python download checksum mismatch'}; Move-Item -LiteralPath $partial -Destination $zip -Force} finally {if(Test-Path -LiteralPath $partial){Remove-Item -LiteralPath $partial}}}; Add-Type -AssemblyName System.IO.Compression.FileSystem; $stage=Join-Path $parent ('staging-'+[guid]::NewGuid().ToString('N')); [IO.Compression.ZipFile]::ExtractToDirectory($zip,$stage); if(!([IO.Path]::GetFullPath($stage).StartsWith($parent+[IO.Path]::DirectorySeparatorChar)) -or !([IO.Path]::GetFullPath($target).StartsWith($parent+[IO.Path]::DirectorySeparatorChar))){throw 'Invalid Python destination'}; Move-Item -LiteralPath $stage -Destination $target"
if errorlevel 1 exit /b 1
set "WHISPER_SETUP_PYTHON=.python\3.12.10\python.exe"
call :check_python
if errorlevel 1 (
  echo Python を起動できませんでした。上のエラーをご確認ください。
  exit /b 1
)

:ready
"%WHISPER_SETUP_PYTHON%" -X utf8 setup_local.py %*
exit /b %errorlevel%

:system_python
py -3.12 -X utf8 setup_local.py %*
exit /b %errorlevel%

:check_python
if not exist "%WHISPER_SETUP_PYTHON%" exit /b 1
"%WHISPER_SETUP_PYTHON%" -I -c "import sys,struct,tkinter,venv;assert sys.version_info[:2]==(3,12) and struct.calcsize('P')==8" >nul 2>&1
exit /b %errorlevel%

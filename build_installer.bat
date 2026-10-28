@echo off
setlocal enableextensions
cd /d "%~dp0"
title Grabwave installer builder
echo ============================================================
echo  Grabwave installer builder  (double-click, then wait ~5-10 min)
echo  Result: dist\Grabwave-Setup-x.y.exe  - the file you hand to users.
echo ============================================================
echo.

where py >nul 2>nul
if errorlevel 1 (
  echo Python 3.10 or newer is required to BUILD the installer ^(users of the installer need nothing^).
  echo Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^), then run this again.
  pause & exit /b 1
)

echo [1/5] Preparing build tools...
if not exist .venv ( py -3 -m venv .venv || goto :fail )
call .venv\Scripts\activate || goto :fail
python -m pip install -U pip >nul || goto :fail
pip install -r requirements.txt || goto :fail

echo [2/5] Getting ffmpeg...
if exist ffmpeg\ffmpeg.exe (
  echo   Using the ffmpeg folder already here.
) else (
  echo   Downloading ffmpeg ^(about 90 MB^)...
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference='Stop'; $z=Join-Path $env:TEMP 'vg_ffmpeg.zip'; $x=Join-Path $env:TEMP 'vg_ffmpeg';" ^
    "Invoke-WebRequest 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile $z;" ^
    "if (Test-Path $x) { Remove-Item $x -Recurse -Force }; Expand-Archive $z -DestinationPath $x;" ^
    "New-Item -ItemType Directory -Force ffmpeg | Out-Null;" ^
    "Get-ChildItem $x -Recurse -Include ffmpeg.exe,ffprobe.exe | Copy-Item -Destination ffmpeg -Force" || goto :fail
)
if not exist ffmpeg\ffmpeg.exe ( echo ffmpeg.exe is missing. & goto :fail )

echo [3/5] Packaging the app...
pyinstaller --noconfirm --clean Grabwave.spec || goto :fail

echo [4/5] Looking for Inno Setup ^(makes the installer^)...
call :findiscc
if not defined ISCC (
  echo   Not found - installing it with winget...
  winget install -e --id JRSoftware.InnoSetup --silent --accept-package-agreements --accept-source-agreements
  call :findiscc
)
if not defined ISCC (
  echo Could not find Inno Setup. Install it from https://jrsoftware.org/isdl.php and run this again.
  pause & exit /b 1
)

for /f %%v in ('python -c "import re;print(re.search(r'VERSION = .([0-9.]+)', open('app.py').read()).group(1))"') do set VER=%%v

echo [5/5] Building the installer ^(version %VER%^)...
"%ISCC%" /DMyAppVersion=%VER% installer\Grabwave.iss || goto :fail

echo.
echo DONE:  dist\Grabwave-Setup-%VER%.exe
explorer dist
pause
exit /b 0

:findiscc
set ISCC=
for %%p in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe" "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe") do (
  if exist %%p set ISCC=%%~p
)
exit /b 0

:fail
echo.
echo Something went wrong ^(see the messages above^). Send me a screenshot and I'll fix it.
pause
exit /b 1

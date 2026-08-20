@echo off
setlocal enabledelayedexpansion
title PlotCut - Setup

set "DIR=%~dp0"

echo ============================================
echo   PlotCut - Setup
echo   Subtitle -^> script + cut edit -^> CapCut
echo   (no GPU / no torch - takes 1-2 minutes)
echo ============================================
echo.

REM [1/5] Python
echo [1/5] Checking Python...
set "PYURL=https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
set "PYEXE=%TEMP%\py_setup.exe"
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo     Python not found. Downloading Python 3.11...
    del "%PYEXE%" >nul 2>&1
    echo     [1] curl ...
    curl -L -f --retry 3 --show-error -o "%PYEXE%" "%PYURL%"
    if not exist "%PYEXE%" (
        echo     curl failed - [2] PowerShell ...
        powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest '%PYURL%' -OutFile '%PYEXE%' } catch {}"
    )
    if not exist "%PYEXE%" (
        echo     [3] certutil ...
        certutil -urlcache -split -f "%PYURL%" "%PYEXE%" >nul 2>&1
    )
    if not exist "%PYEXE%" (
        echo.
        echo [ERROR] Python download failed - antivirus blocking or low disk space.
        echo   FIX: install Python 3.11 manually from https://www.python.org/downloads/
        echo        During install, CHECK "Add python.exe to PATH", then run setup.bat again.
        pause
        exit /b 1
    )
    "%PYEXE%" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0
    del "%PYEXE%" >nul 2>&1
    set "PYPATH=%LOCALAPPDATA%\Programs\Python\Python311"
    set "PATH=!PYPATH!;!PYPATH!\Scripts;%PATH%"
    python --version >nul 2>&1
    if !errorlevel! neq 0 (
        echo [ERROR] Python install failed. Install Python 3.11 from https://python.org and check "Add to PATH".
        pause
        exit /b 1
    )
) else (
    for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo     %%v
)
python -m pip install --upgrade pip -q
echo.

REM [2/5] Python packages
echo [2/5] Installing packages...
python -m pip install tkinterdnd2 pillow -q
if %errorlevel% neq 0 (
    echo [ERROR] package install failed. Check internet connection.
    pause
    exit /b 1
)
echo     tkinterdnd2 / pillow installed.
echo.

REM [3/5] ffmpeg (needed by pydub and ffprobe)
echo [3/5] Checking ffmpeg (ffprobe reads the movie length)...
ffprobe -version >nul 2>&1
if %errorlevel% neq 0 (
    echo     Installing ffmpeg via winget...
    winget install --id Gyan.FFmpeg -e --source winget --accept-source-agreements --accept-package-agreements
    ffprobe -version >nul 2>&1
    if !errorlevel! neq 0 (
        echo     [INFO] ffmpeg not on PATH yet.
        echo            Close this window, open a NEW terminal, and run setup.bat again,
        echo            or install manually: https://www.gyan.dev/ffmpeg/builds/
    )
) else (
    echo     ffmpeg/ffprobe: OK
)
echo.

REM [4/5] CapCut (PlotCut writes projects into its draft folder)
echo [4/5] Checking CapCut...
set "CCAPPS=%LOCALAPPDATA%\CapCut\Apps"
set "CCDRAFT=%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft"
if not exist "%CCAPPS%" (
    echo     [WARN] CapCut not found on this PC.
    echo            PlotCut writes projects into the CapCut draft folder,
    echo            so CapCut must be installed: https://www.capcut.com/
    echo            Install it later and run setup.bat again.
) else (
    set "CCVER="
    for /f "delims=" %%d in ('dir /b /ad "%CCAPPS%" 2^>nul') do set "CCVER=%%d"
    echo     CapCut !CCVER! : OK
    echo !CCVER! | findstr /b "8.9." >nul
    if !errorlevel! neq 0 (
        echo     [WARN] The bundled draft template came from CapCut 8.9.1.
        echo            Another major version may be refused when updating.
    )
    if not exist "%CCDRAFT%" (
        echo     [INFO] Draft folder not there yet - it appears once you
        echo            create one project inside CapCut.
    )
)
echo.

REM [5/5] icon + shortcut
echo [5/5] Creating icon and shortcut...
python "%DIR%create_icon.py"
powershell -NoProfile -ExecutionPolicy Bypass -File "%DIR%create_shortcut.ps1"
echo.

echo ============================================
echo   Setup complete!
echo   Start via the "PlotCut" shortcut on your Desktop.
echo.
echo   No API key needed - PlotCut never calls an LLM API.
echo.
echo   To have the script written for you, double-click Codex-Setup
echo   (the file starting with 'Codex') once and sign in with ChatGPT.
echo ============================================
timeout /t 5 /nobreak >nul
exit

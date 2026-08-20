@echo off
setlocal enabledelayedexpansion
title Codex - Setup

set "PROJDIR=%~dp0"
set "NODEDIR=%ProgramFiles%\nodejs"
set "NPMBIN=%APPDATA%\npm"

echo ============================================
echo   OpenAI Codex - Setup
echo   Node.js -^> Codex CLI -^> ChatGPT login
echo   (no API key needed - ChatGPT Plus covers it)
echo ============================================
echo.

REM ---------------------------------------------------------------
REM [1/3] Node.js   - the MSI is machine-wide, so it needs admin
REM ---------------------------------------------------------------
echo [1/3] Checking Node.js...
call :ADDPATH
where node >nul 2>&1
if !errorlevel! equ 0 (
    for /f "delims=" %%v in ('node -v') do echo     Node.js %%v : OK
    goto NODE_OK
)

echo     Node.js not found.
where winget >nul 2>&1
if !errorlevel! neq 0 (
    echo.
    echo     [ERROR] winget is not available on this PC.
    echo             Install Node.js LTS by hand: https://nodejs.org/
    echo             Then double-click this file again.
    goto FAIL
)

REM Not elevated? Relaunch this same file once, elevated.
net session >nul 2>&1
if !errorlevel! neq 0 (
    if /i "%~1"=="elevated" (
        echo     [ERROR] Still no administrator rights.
        echo             Right-click this file and pick "Run as administrator".
        goto FAIL
    )
    echo     Installing Node.js needs administrator rights.
    echo     A Windows permission prompt will pop up - press Yes.
    echo.
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList 'elevated' -Verb RunAs"
    if !errorlevel! neq 0 (
        echo.
        echo     [ERROR] The permission prompt was refused or blocked.
        echo             Right-click this file and pick "Run as administrator".
        goto FAIL
    )
    echo     Setup continues in the new window. You can close this one.
    timeout /t 8 /nobreak >nul
    exit /b 0
)

echo     Installing Node.js LTS via winget...
winget install --id OpenJS.NodeJS.LTS -e --source winget --accept-source-agreements --accept-package-agreements
call :ADDPATH
where node >nul 2>&1
if !errorlevel! neq 0 (
    echo.
    echo     [INFO] Node.js was installed but is not on PATH in this window yet.
    echo            Close this window and double-click this file again.
    goto FAIL
)
for /f "delims=" %%v in ('node -v') do echo     Node.js %%v : installed

:NODE_OK
echo.

REM ---------------------------------------------------------------
REM [2/3] Codex CLI   - lands in %APPDATA%\npm, no admin needed
REM ---------------------------------------------------------------
echo [2/3] Installing Codex CLI...
where codex >nul 2>&1
if !errorlevel! equ 0 echo     Codex already installed - updating to latest...
call npm install -g @openai/codex
if !errorlevel! neq 0 (
    echo.
    echo     [ERROR] npm install failed.
    echo             Check your internet connection and try again.
    goto FAIL
)

call :ADDPATH
where codex >nul 2>&1
if !errorlevel! neq 0 (
    echo.
    echo     [INFO] Codex was installed but is not on PATH in this window yet.
    echo            Close this window and double-click this file again.
    goto FAIL
)
for /f "delims=" %%v in ('codex --version 2^>nul') do echo     Codex %%v : OK
echo.

REM ---------------------------------------------------------------
REM [3/3] Login + start
REM ---------------------------------------------------------------
echo [3/3] Starting Codex...
echo.
echo     A login prompt will appear below.
echo.
echo     ^>^> Choose "Sign in with ChatGPT"  (your Plus plan covers it)
echo     ^>^> Do NOT choose the API key option - that is billed separately.
echo.
echo     Your browser will open. Log in, come back here, and you are done.
echo.
echo ============================================
echo.
cd /d "%PROJDIR%"
codex
goto END

:ADDPATH
if exist "%NODEDIR%\node.exe" set "PATH=%PATH%;%NODEDIR%"
if exist "%NPMBIN%" set "PATH=%PATH%;%NPMBIN%"
exit /b 0

:FAIL
echo.
echo ============================================
echo   Setup did not finish. Read the message above.
echo ============================================
echo.
pause
exit /b 1

:END
echo.
pause
endlocal

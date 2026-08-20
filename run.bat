@echo off

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed. Run setup.bat first.
    pause
    exit /b 1
)

python -c "import tkinterdnd2" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Required packages not found. Run setup.bat first.
    pause
    exit /b 1
)

start "" pythonw "%~dp0gui.py"

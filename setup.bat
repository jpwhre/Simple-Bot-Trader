@echo off
REM Simple Bot Trader — Setup Script (Windows)
REM Detects Python, installs dependencies, creates a desktop shortcut.
REM Usage:  double-click setup.bat or run from Command Prompt.

setlocal enabledelayedexpansion

set APP_NAME=Simple Bot Trader
set APP_DIR=%~dp0
set VENV_DIR=%APP_DIR%venv
set REQ_FILE=%APP_DIR%requirements.txt

echo ============================================
echo   %APP_NAME% — Setup
echo ============================================
echo.

REM --- Python check --------------------------------------------------------
set PYTHON=
where python >nul 2>&1
if %ERRORLEVEL% equ 0 (
    for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
    for /f "tokens=1,2 delims=." %%a in ("!PYVER!") do (
        set MAJOR=%%a
        set MINOR=%%b
    )
    if !MAJOR! geq 3 (
        if !MINOR! geq 9 (
            set PYTHON=python
        )
    )
)

where python3 >nul 2>&1
if %ERRORLEVEL% equ 0 (
    if not defined PYTHON (
        for /f "tokens=2" %%v in ('python3 --version 2^>^&1') do set PYVER=%%v
        for /f "tokens=1,2 delims=." %%a in ("!PYVER!") do (
            set MAJOR=%%a
            set MINOR=%%b
        )
        if !MAJOR! geq 3 (
            if !MINOR! geq 9 (
                set PYTHON=python3
            )
        )
    )
)

if not defined PYTHON (
    echo [FAIL] Python 3.9+ is required but not found.
    echo.
    echo Install Python from: https://www.python.org/downloads/
    echo   - Check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo [OK] Found %PYTHON%
%PYTHON% --version
echo.

REM --- Virtual environment --------------------------------------------------
if not exist "%VENV_DIR%" (
    echo Creating virtual environment...
    %PYTHON% -m venv "%VENV_DIR%"
    echo [OK] Virtual environment created
) else (
    echo [OK] Virtual environment already exists
)
echo.

REM --- Install dependencies -------------------------------------------------
call "%VENV_DIR%\Scripts\activate.bat"
echo Installing dependencies from requirements.txt...
pip install --upgrade pip -q >nul 2>&1
pip install -r "%REQ_FILE%" -q
if %ERRORLEVEL% neq 0 (
    echo [FAIL] Failed to install dependencies.
    pause
    exit /b 1
)
echo [OK] Dependencies installed
echo.

REM --- Desktop shortcut -----------------------------------------------------
echo Creating desktop shortcut...
set SHORTCUT_DIR=%USERPROFILE%\Desktop
set SHORTCUT_VBS=%TEMP%\create_shortcut.vbs

(
    echo Set oWS = WScript.CreateObject^("WScript.Shell"^)
    echo sLinkFile = "%SHORTCUT_DIR%\%APP_NAME%.lnk"
    echo Set oLink = oWS.CreateShortcut^(sLinkFile^)
    echo oLink.TargetPath = "%VENV_DIR%\Scripts\pythonw.exe"
    echo oLink.Arguments = "%APP_DIR%main.py"
    echo oLink.WorkingDirectory = "%APP_DIR%"
    echo oLink.Description = "%APP_NAME%"
    echo oLink.WindowStyle = 7
    echo oLink.Save
) > "%SHORTCUT_VBS%"

cscript /nologo "%SHORTCUT_VBS%" >nul 2>&1
del "%SHORTCUT_VBS%" >nul 2>&1

if exist "%SHORTCUT_DIR%\%APP_NAME%.lnk" (
    echo [OK] Desktop shortcut created
) else (
    echo [WARN] Could not create desktop shortcut (non-critical)
)
echo.

REM --- Done ----------------------------------------------------------------
echo ============================================
echo   Setup complete!
echo ============================================
echo.
echo To run the bot:
echo   1. Double-click "Simple Bot Trader" on your Desktop
echo   2. Or run: %VENV_DIR%\Scripts\python.exe %APP_DIR%main.py
echo.
echo To run with an isolated profile (separate exchange):
echo   %VENV_DIR%\Scripts\python.exe %APP_DIR%main.py --config-dir C:\my-other-bot
echo.
pause

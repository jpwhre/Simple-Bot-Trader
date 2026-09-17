@echo off
REM Simple Bot Trader - Setup Script (Windows)
REM Detects Python, installs dependencies, creates a desktop shortcut.
REM Usage:  double-click setup.bat or run from Command Prompt.

setlocal enabledelayedexpansion

set APP_NAME=Simple Bot Trader
set APP_DIR=%~dp0
set VENV_DIR=%APP_DIR%venv
set REQ_FILE=%APP_DIR%requirements.txt

REM --- Install to a permanent folder if we are running from a temp dir --------
REM Windows' zip extractor (or a browser) often drops the app in %TEMP%, where
REM the shortcuts/venv later break (Win7/10 cleanup, reboots). Move the app to a
REM stable per-user Programs folder first, then re-run setup from there.
set STABLE=%LOCALAPPDATA%\Programs\SimpleBotTrader
echo %APP_DIR% | findstr /i /c:"\Temp\" >nul
if not errorlevel 1 (
    if /i not "%APP_DIR%"=="%STABLE%\" (
        echo.
        echo Detected a temporary install folder - moving to %STABLE%
        if exist "%APP_DIR%venv" rmdir /s /q "%APP_DIR%venv"
        if not exist "%STABLE%" mkdir "%STABLE%"
        xcopy "%APP_DIR%." "%STABLE%\" /e /i /y >nul
        cd /d "%STABLE%"
        call "%STABLE%\setup.bat"
        exit /b 0
    )
)

echo ============================================
echo   %APP_NAME% - Setup
echo ============================================
echo.

REM --- Python check --------------------------------------------------------
set PYTHON=
set BROWSE=
:check_python
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

REM Python launcher (many machines have `py` even without python on PATH)
where py >nul 2>&1
if %ERRORLEVEL% equ 0 (
    if not defined PYTHON (
        py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" >nul 2>&1
        if !ERRORLEVEL! equ 0 set PYTHON=py -3
    )
)

if defined PYTHON goto python_found

echo [FAIL] Python 3.9+ is required but not found.
echo.
set /p PYCHOICE=  Try to install Python now [Y/N]:
echo.
if /I "!PYCHOICE!"=="Y" (
    where winget >nul 2>&1
    if !ERRORLEVEL! equ 0 (
        echo   [OK] Installing Python 3.12 via winget...
        winget install --id Python.Python.3.12 --silent --accept-source-agreements --accept-package-agreements >nul 2>&1
    ) else (
        echo   winget is not available - you will download Python manually.
    )
    set BROWSE=1
    echo.
    echo   Opening the official Python download page...
    start "" "https://www.python.org/downloads/"
) else (
    echo   Download Python from: https://www.python.org/downloads/
    echo   - Check "Add Python to PATH" during installation.
)
echo.
if not defined BROWSE (
    echo   Opening the official Python download page...
    start "" "https://www.python.org/downloads/"
)
echo.
echo   After Python is installed, press Y here and the installer re-checks
echo   and continues automatically.
set /p PYAGAIN=  Press Y to re-check (or N to quit) [Y/N]:
echo.
if /I "!PYAGAIN!"=="Y" (
    REM PATH may need a new window: also probe the common per-user install dir.
    if not defined PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PYTHON="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    if not defined PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set PYTHON="%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    goto check_python
)
echo   Python was not detected. Close this window and double-click setup.bat again.
pause
exit /b 1

:python_found
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
REM OneDrive-safe Desktop path (folder redirection moves the real Desktop).
set SHORTCUT_DIR=%USERPROFILE%\Desktop
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set SHORTCUT_DIR=%%D
set SHORTCUT_VBS=%TEMP%\create_shortcut.vbs

(
    echo Set oWS = WScript.CreateObject^("WScript.Shell"^)
    echo sLinkFile = "%SHORTCUT_DIR%\%APP_NAME%.lnk"
    echo Set oLink = oWS.CreateShortcut^(sLinkFile^)
    echo oLink.TargetPath = "%VENV_DIR%\Scripts\pythonw.exe"
    echo oLink.Arguments = "%APP_DIR%main.py"
    echo oLink.WorkingDirectory = "%APP_DIR%"
    echo oLink.Description = "%APP_NAME%"
    echo oLink.IconLocation = "%APP_DIR%sbt\ui\styles\icon.png"
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

REM --- Startup shortcut (return-to-state: reopen at login if it was running) --
echo Creating startup shortcut...
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set STARTUP_VBS=%TEMP%\create_startup.vbs

(
    echo Set oWS = WScript.CreateObject^("WScript.Shell"^)
    echo sLinkFile = "%STARTUP_DIR%\%APP_NAME%.lnk"
    echo Set oLink = oWS.CreateShortcut^(sLinkFile^)
    echo oLink.TargetPath = "%VENV_DIR%\Scripts\pythonw.exe"
    echo oLink.Arguments = "%APP_DIR%main.py --if-was-launched"
    echo oLink.WorkingDirectory = "%APP_DIR%"
    echo oLink.Description = "%APP_NAME% auto-reopen"
    echo oLink.IconLocation = "%APP_DIR%sbt\ui\styles\icon.png"
    echo oLink.WindowStyle = 7
    echo oLink.Save
) > "%STARTUP_VBS%"

cscript /nologo "%STARTUP_VBS%" >nul 2>&1
del "%STARTUP_VBS%" >nul 2>&1

if exist "%STARTUP_DIR%\%APP_NAME%.lnk" (
    echo [OK] Startup shortcut created
) else (
    echo [WARN] Could not create startup shortcut (non-critical)
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

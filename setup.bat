@echo off
echo === ASNB Buyer Setup ===
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo ERROR: Python not found. Install from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

python --version
echo.

if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv
)

echo Installing dependencies...
.venv\Scripts\pip install -r requirements.txt

if not exist config.ini (
    echo.
    echo Creating config.ini from template...
    copy config.ini.template config.ini
    echo.
    echo ===================================================
    echo  NEXT STEP: Edit config.ini to add your profile
    echo  Then run: run.bat
    echo ===================================================
) else (
    echo.
    echo config.ini already exists.
    echo Run: run.bat or gui.bat
)

echo.
echo Setup complete!
pause

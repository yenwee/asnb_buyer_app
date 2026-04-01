@echo off
if not exist .venv (
    echo ERROR: Run setup.bat first.
    pause
    exit /b 1
)
if not exist config.ini (
    echo ERROR: config.ini not found. Run setup.bat first, then edit config.ini.
    pause
    exit /b 1
)

.venv\Scripts\python -m asnb.gui

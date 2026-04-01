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

if "%~1"=="" (
    echo Usage: run.bat ^<profile^>
    echo.
    echo Available profiles:
    .venv\Scripts\python -c "from asnb.config import load_config, get_profiles; profiles = get_profiles(load_config()); [print(f'  {k:15s} ({v.get(\"username\",\"?\")})') for k,v in profiles.items()]" 2>nul || echo   (none found - add [Profile.xxx] to config.ini)
    echo.
    echo Example: run.bat yenwee
    pause
    exit /b 0
)

.venv\Scripts\python -m asnb.main --profile %1
pause

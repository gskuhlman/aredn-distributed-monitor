@echo off
REM Start the AREDN Network Monitor using the project virtual environment.
REM Works from any directory (e.g. double-click or a shortcut).
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found in "%~dp0.venv".
    echo Create it with:
    echo     py -3.10 -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

".venv\Scripts\python.exe" app.py %*
if errorlevel 1 pause

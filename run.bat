@echo off
rem Lecture Recorder launcher for Windows. First run sets everything up.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Setting up Lecture Recorder for the first time...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo Python 3.10 or newer is required. Get it from https://www.python.org/downloads/
        pause
        exit /b 1
    )
    .venv\Scripts\python -m pip install --upgrade pip
    .venv\Scripts\python -m pip install -e .
    if errorlevel 1 ( pause & exit /b 1 )
)
.venv\Scripts\python -m lecturerecorder %*

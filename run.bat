@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m backend.brain %*
) else (
    echo [Kira] Virtual environment not found at .venv\Scripts\python.exe
    echo Please set up your environment or check your folder.
    pause
)


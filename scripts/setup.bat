@echo off
setlocal enabledelayedexpansion

echo =======================================================
echo          Kira Desktop Assistant - Setup Script
echo =======================================================

cd /d "%~dp0\.."

:: 1. Check Python installation
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python is not found on PATH. Please install Python 3.11+ and add it to PATH.
    exit /b 1
)

:: 2. Create virtual environment if it doesn't exist
if not exist ".venv\Scripts\python.exe" (
    echo [1/5] Creating Python virtual environment in .venv...
    python -m venv .venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        exit /b 1
    )
) else (
    echo [1/5] Virtual environment .venv already exists.
)

:: 3. Install Python dependencies
echo [2/5] Installing Python dependencies from requirements.lock...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>nul
if exist "requirements.lock" (
    ".venv\Scripts\python.exe" -m pip install -r requirements.lock
) else (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
if %errorlevel% neq 0 (
    echo [WARN] Installation from lockfile had errors; attempting requirements.txt fallback...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

:: 4. Install Playwright browser
echo [3/5] Installing Playwright Chromium browser...
".venv\Scripts\playwright.exe" install chromium

:: 5. Install Electron dependencies
if exist "avatar\package.json" (
    echo [4/5] Installing avatar Node.js dependencies...
    where npm >nul 2>nul
    if %errorlevel% equ 0 (
        pushd avatar
        call npm install
        popd
    ) else (
        echo [WARN] npm not found on PATH. Please install Node.js to run the Electron avatar.
    )
)

:: 6. Setup local .env from example if missing
if not exist ".env" (
    if exist ".env.example" (
        echo [INFO] Creating initial .env from .env.example...
        copy ".env.example" ".env" >nul
    )
)

:: 7. Run system doctor
echo [5/5] Running Kira doctor system probe...
".venv\Scripts\python.exe" -m backend.doctor

echo.
echo =======================================================
echo Setup complete! To start Kira:
echo   - Run the avatar app: cd avatar ^&^& npm start
echo   - Or run the backend: run.bat
echo =======================================================

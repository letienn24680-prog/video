@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

if not exist ".env" (
    if exist ".env.example" (
        copy /Y ".env.example" ".env" >nul
        echo [AI Video Studio] Created .env from .env.example
    ) else (
        echo [WARNING] Missing .env and .env.example!
    )
)

set "PYTHON_BIN=python"
if exist ".venv\Scripts\python.exe" (
    set "PYTHON_BIN=.venv\Scripts\python.exe"
)

echo.
echo ============================================================
echo   AI Video Studio - Local Development Server
echo   URL:    http://127.0.0.1:8000
echo   Docs:   http://127.0.0.1:8000/docs
echo   Python: %PYTHON_BIN%
echo ============================================================
echo.

"%PYTHON_BIN%" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
endlocal

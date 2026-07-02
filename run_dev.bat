@echo off
setlocal ENABLEEXTENSIONS ENABLEDELAYEDEXPANSION
cd /d "%~dp0"

set "PYEXE="
if exist .venv\Scripts\python.exe (
    set "PYEXE=.venv\Scripts\python.exe"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python was not found. Create .venv first or install Python and add it to PATH.
        pause
        exit /b 1
    )
    set "PYEXE=python"
)

echo Starting CAN Scope v00.00.xx in dev mode...
"%PYEXE%" app.py
if errorlevel 1 (
    echo.
    echo App exited with an error.
)
pause

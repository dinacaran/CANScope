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

"%PYEXE%" -m PyInstaller CANScope.spec --noconfirm
if errorlevel 1 (
    echo.
    echo Build failed.
    pause
    exit /b 1
)

if not exist dist\CANScope\CANScope.exe (
    echo.
    echo Build completed but CANScope.exe was not found.
    pause
    exit /b 1
)

echo.
echo Build successful.
echo Output: dist\CANScope\
pause

@echo off
REM Run the CANScope test suite.
REM Run this from the repo root: tests\run_tests.bat

cd /d "%~dp0.."

echo Generating binary fixtures if needed...
python tests\fixtures\_generate.py

echo.
echo Running tests...
python -m pytest tests\ -v --tb=short %*

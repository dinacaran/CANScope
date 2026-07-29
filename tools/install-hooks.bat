@echo off
REM Install the shared pre-commit hook for the PUBLIC CANScope repo.
REM Run once after cloning:  tools\install-hooks.bat
REM
REM Uses core.hooksPath so the hook stays tracked in-repo (tools/hooks/) instead
REM of being copied into the untracked .git/hooks/ directory.

git config core.hooksPath tools/hooks
if errorlevel 1 (
    echo Failed to set core.hooksPath — are you inside the git repo?
    exit /b 1
)

echo Installed: core.hooksPath -^> tools/hooks
echo The pre-commit hook now blocks diagnostics-path commits and runs the test suite.

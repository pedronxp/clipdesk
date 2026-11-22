@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" dashboard/server.py
) else (
    python dashboard/server.py
)
pause

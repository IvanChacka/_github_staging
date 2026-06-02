@echo off
cd /d "D:\ivan\prob"
set LOGS_DIR=D:\ivan\prob\logs

echo [%date% %time%] Starting watchdog...

"%cd%\.venv\Scripts\python.exe" watchdog.py > "%LOGS_DIR%\watchdog_stdout.log" 2>&1

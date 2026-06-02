@echo off
:loop
C:\veighna_studio\python.exe D:\ivan\vol\iv_guard.py
timeout /t 5 /nobreak >nul
goto loop

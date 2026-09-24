@echo off
rem Starts the voice assistant with this project's own Python: no terminal to open,
rem no .venv to activate. Double-click it, or let "python voice.py --autostart on"
rem run it at every login.
title AI Agent voice
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Could not find .venv\Scripts\python.exe in %CD% -- see the Voice section of README.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" voice.py %*
if errorlevel 1 pause

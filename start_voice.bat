@echo off
rem Starts the voice assistant with this project's own Python: no terminal to open,
rem no .venv to activate. Double-click it, or let "python voice.py --autostart on"
rem run it at every login. pythonw.exe runs it without a terminal window: it lives
rem in its small floating window and the icon by the clock (log: output\voice.log).
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" voice.py %*
  exit /b 0
)
if not exist ".venv\Scripts\python.exe" (
  echo Could not find .venv\Scripts\python.exe in %CD% -- see the Voice section of README.md.
  pause
  exit /b 1
)
title AI Agent voice
".venv\Scripts\python.exe" voice.py %*
if errorlevel 1 pause

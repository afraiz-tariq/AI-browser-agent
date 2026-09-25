@echo off
rem Builds the double-clickable app: dist\AI Agent\AI Agent.exe
rem Run it from the project folder, with the project's .venv set up as in
rem README.md (Installation). It installs everything the app window, voice
rem and Windows arm need into that .venv, then PyInstaller (ai_agent.spec).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Could not find .venv\Scripts\python.exe in %CD% -- see Installation in README.md.
  pause
  exit /b 1
)
set PY=.venv\Scripts\python.exe
%PY% -m pip install -r requirements.txt openai faster-whisper sounddevice pyttsx3 pystray pillow pywebview pywinauto pyinstaller || goto :failed
%PY% -m PyInstaller ai_agent.spec --noconfirm || goto :failed
echo.
echo Done: dist\AI Agent\AI Agent.exe
echo First run creates a .env next to it for your API key. Zip the whole "dist\AI Agent"
echo folder to give it to someone -- but never with your own .env inside.
pause
exit /b 0
:failed
echo Build failed -- see the messages above.
pause
exit /b 1

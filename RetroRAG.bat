@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo RetroRAG is not set up on this PC yet - running setup first...
  call setup.bat || exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m retrorag %*

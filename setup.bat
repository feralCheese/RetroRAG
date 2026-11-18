@echo off
setlocal
cd /d "%~dp0"
echo ============================================
echo   RetroRAG setup (run once on each PC)
echo ============================================
echo.

rem ---- find Python 3.10+
set "PY="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3.10 or newer was not found.
  echo Install it with:   winget install Python.Python.3.13
  echo or from https://www.python.org/downloads/   then run setup.bat again.
  pause
  exit /b 1
)

rem ---- a .venv copied from another PC does not work: rebuild it if it is broken
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import PySide6, pymupdf" >nul 2>nul || (
    echo Existing .venv is broken or came from another PC - recreating it...
    rmdir /s /q .venv
  )
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment with %PY% ...
  %PY% -m venv .venv || (echo Failed to create the virtual environment. & pause & exit /b 1)
)

echo Installing packages (this takes a few minutes the first time)...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo Package install failed. & pause & exit /b 1)


echo.
".venv\Scripts\python.exe" -c "from retrorag.ocr import find_tesseract; import sys; p=find_tesseract(); print('Tesseract OCR:', p or 'NOT FOUND'); sys.exit(0 if p else 1)"
if errorlevel 1 (
  echo   OCR needs Tesseract. Install it with:   winget install UB-Mannheim.TesseractOCR
  echo   ^(or put a Tesseract-OCR folder next to this file^)
)

echo.
choice /c YN /n /m "Portable mode - keep settings and the default library inside this folder? [Y/N] "
if errorlevel 2 (
  if exist portable.flag del portable.flag
) else (
  echo RetroRAG portable mode> portable.flag
)

echo.
echo Setup complete. Start RetroRAG with RetroRAG.bat
pause

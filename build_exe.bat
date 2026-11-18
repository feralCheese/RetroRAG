@echo off
rem Builds a standalone, portable copy of RetroRAG in dist\RetroRAG (no Python needed on the target PC).
rem Copy that whole folder (or zip it) to any Windows 10/11 PC and run RetroRAG.exe.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call setup.bat
".venv\Scripts\python.exe" -m pip install "pyinstaller>=6.0" || (echo Could not install PyInstaller & pause & exit /b 1)
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean retrorag.spec || (echo Build failed & pause & exit /b 1)

echo RetroRAG portable mode> "dist\RetroRAG\portable.flag"

rem Optionally ship Tesseract inside the build so OCR works on PCs without it installed
set "TESS="
if exist "%ProgramFiles%\Tesseract-OCR\tesseract.exe" set "TESS=%ProgramFiles%\Tesseract-OCR"
if not defined TESS if exist "%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe" set "TESS=%LOCALAPPDATA%\Programs\Tesseract-OCR"
if defined TESS (
  choice /c YN /n /m "Bundle Tesseract OCR from %TESS% into the build (about 80 MB, Apache-2.0 licensed)? [Y/N] "
  if not errorlevel 2 xcopy /e /i /q /y "%TESS%" "dist\RetroRAG\Tesseract-OCR" >nul
)

echo.
echo Built: %CD%\dist\RetroRAG
echo Copy that folder anywhere (USB drive, another PC) and run RetroRAG.exe.
pause

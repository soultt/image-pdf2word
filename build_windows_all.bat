@echo off
setlocal
cd /d "%~dp0"

REM Build both Windows distributions: slim (no torch) and full (with torch).
REM Do not use the `py` launcher: it may point at a Python without the project
REM dependencies, producing a broken exe.
set PY=python
where python >nul 2>nul
if errorlevel 1 (
  echo Python not found on PATH.
  exit /b 1
)

%PY% -c "import fitz, mcp, uvicorn" >nul 2>nul
if errorlevel 1 (
  echo Current Python is missing project dependencies ^(fitz, mcp, uvicorn^).
  echo Activate the virtual environment used for pdf2word and retry.
  exit /b 1
)

echo Installing/updating PyInstaller...
%PY% -m pip install --upgrade pyinstaller
if errorlevel 1 goto :failed

echo.
echo === Building SLIM build ^(deps downloaded on first table use^) ===
%PY% -m PyInstaller --clean --noconfirm pdf2word.spec
if errorlevel 1 goto :failed

echo.
echo === Building FULL build ^(torch/transformers bundled^) ===
%PY% -m PyInstaller --clean --noconfirm pdf2word-full.spec
if errorlevel 1 goto :failed

echo.
echo Build finished:
echo   Slim: dist\pdf2word\pdf2word.exe
echo   Full: dist\pdf2word-full\pdf2word-full.exe
echo Config and history are stored next to each executable.
exit /b 0

:failed
echo.
echo Build failed.
exit /b 1

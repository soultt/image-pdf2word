@echo off
setlocal
cd /d "%~dp0"

REM Use the Python interpreter that has the project's dependencies installed.
REM Do not use the `py` launcher: on some machines it defaults to a different
REM Python version with none of these packages, producing a broken exe.
set PY=python
where python >nul 2>nul
if errorlevel 1 (
  echo Python not found on PATH.
  exit /b 1
)

%PY% -c "import fitz, mcp, uvicorn" >nul 2>nul
if errorlevel 1 (
  echo Current Python is missing project dependencies (fitz, mcp, uvicorn).
  echo Activate the virtual environment used for pdf2word and retry.
  exit /b 1
)

echo Installing PyInstaller...
%PY% -m pip install --upgrade pyinstaller
if errorlevel 1 goto :failed

echo Building pdf2word.exe with %PY%...
%PY% -m PyInstaller --clean --noconfirm pdf2word.spec
if errorlevel 1 goto :failed

echo.
echo Build finished: "%cd%\dist\pdf2word\pdf2word.exe"
echo Config and history are stored next to the executable.
exit /b 0

:failed
echo.
echo Build failed.
exit /b 1

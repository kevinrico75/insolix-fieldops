@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Rico FieldOps

echo.
echo ==============================================
echo        RICO FIELDOPS - STARTING
echo ==============================================
echo.

set "PYTHON_EXE="

rem Prefer the standard Windows Python launcher when available.
where py >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%P"
)

rem Fall back to python.exe on PATH.
if not defined PYTHON_EXE (
    where python >nul 2>nul
    if not errorlevel 1 (
        for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%P"
    )
)

if not defined PYTHON_EXE goto :NO_PYTHON

echo Python found:
echo   %PYTHON_EXE%
echo.

if not exist ".venv\Scripts\python.exe" (
    echo First-time setup: creating local environment...
    "%PYTHON_EXE%" -m venv ".venv"
    if errorlevel 1 goto :VENV_ERROR
)

echo Checking required packages...
".venv\Scripts\python.exe" -c "import fastapi,uvicorn,jinja2,multipart" >nul 2>nul
if errorlevel 1 (
    echo Installing required packages. This only needs to happen once...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    if errorlevel 1 goto :PIP_ERROR
    ".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
    if errorlevel 1 goto :PIP_ERROR
)

echo.
echo Rico FieldOps is starting at:
echo   http://127.0.0.1:8000
echo.
echo Keep this black window OPEN while using FieldOps.
echo To stop FieldOps, close this window or press Ctrl+C.
echo.

rem Open the browser after a short delay while this window runs the server.
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8000'"

".venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8000
set "EXITCODE=%ERRORLEVEL%"

echo.
echo FieldOps stopped with exit code %EXITCODE%.
if not "%EXITCODE%"=="0" (
    echo If you did not intentionally stop it, take a picture of this window and send it to me.
)
pause
exit /b %EXITCODE%

:NO_PYTHON
echo ERROR: Python 3 is not installed, or Windows cannot find it.
echo.
echo Install Python 3.11 or newer from:
echo   https://www.python.org/downloads/windows/
echo.
echo IMPORTANT during installation:
echo   Check the box labeled "Add python.exe to PATH".
echo.
echo Then close this window and double-click START_WINDOWS.bat again.
echo.
pause
exit /b 1

:VENV_ERROR
echo.
echo ERROR: Windows could not create the local Python environment.
echo Take a picture of this window and send it to me so I can identify the exact cause.
echo.
pause
exit /b 1

:PIP_ERROR
echo.
echo ERROR: Required FieldOps packages could not be installed.
echo Make sure this computer has an internet connection, then try again.
echo If it still fails, take a picture of this window and send it to me.
echo.
pause
exit /b 1

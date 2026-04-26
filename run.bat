@echo off
setlocal
cd /d "%~dp0"

set "VENV_DIR=.venv-paddle"
set "PY_EXE=%CD%\%VENV_DIR%\Scripts\python.exe"
set "PYTHONIOENCODING=utf-8"
set "GLOG_minloglevel=2"
set "FLAGS_minloglevel=2"

if not exist "%PY_EXE%" (
    echo [ERROR] Virtual environment was not found: %VENV_DIR%
    echo [ERROR] Please run install.bat first.
    pause
    exit /b 1
)

if not exist "app_paddle.py" (
    echo [ERROR] app_paddle.py was not found.
    echo [ERROR] Please run this script from the project directory.
    pause
    exit /b 1
)

"%PY_EXE%" -m py_compile app_paddle.py >nul 2>nul
if errorlevel 1 (
    echo [WARN] app_paddle.py did not pass a quick compile check.
    echo [WARN] The app will still start so the full error can be shown.
)

echo [YKT] Starting PaddleOCR app...
"%PY_EXE%" app_paddle.py
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Program exited with code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%

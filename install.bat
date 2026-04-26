@echo off
setlocal
cd /d "%~dp0"

set "VENV_DIR=.venv-paddle"
set "PYTHON_CMD="
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "PYTHONIOENCODING=utf-8"

echo [YKT] Installing runtime environment...
echo [YKT] Project directory: %CD%

where py >nul 2>nul
if not errorlevel 1 (
    py -3.11 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py -3.11"
)

if not defined PYTHON_CMD (
    where python >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    echo.
    echo [ERROR] Python was not found.
    echo [ERROR] Please install Python 3.11, then run install.bat again.
    pause
    exit /b 1
)

echo [YKT] Python command: %PYTHON_CMD%
%PYTHON_CMD% -c "import sys; print('[YKT] Python version:', sys.version.replace('\n', ' ')); raise SystemExit(0 if (3, 10) <= sys.version_info[:2] <= (3, 11) else 1)"
if errorlevel 1 (
    echo [WARN] Python 3.10 or 3.11 is recommended for PaddleOCR on Windows.
    echo [WARN] Installation will continue, but dependency installation may fail.
)

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [YKT] Creating virtual environment: %VENV_DIR%
    %PYTHON_CMD% -m venv "%VENV_DIR%"
    if errorlevel 1 goto :fail
) else (
    echo [YKT] Virtual environment already exists: %VENV_DIR%
)

set "PY_EXE=%CD%\%VENV_DIR%\Scripts\python.exe"

echo [YKT] Upgrading pip tools...
"%PY_EXE%" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto :fail

if exist "requirements.txt" (
    echo [YKT] Installing base requirements...
    "%PY_EXE%" -m pip install -r requirements.txt
    if errorlevel 1 goto :fail
)

if exist "requirements_paddle.txt" (
    echo [YKT] Installing PaddleOCR requirements...
    "%PY_EXE%" -m pip install -r requirements_paddle.txt
    if errorlevel 1 goto :fail
)

echo [YKT] Checking Python files...
"%PY_EXE%" -m py_compile app.py app_paddle.py
if errorlevel 1 goto :fail

echo.
echo [YKT] Installation completed.
echo [YKT] Start the app with run.bat.
pause
exit /b 0

:fail
echo.
echo [ERROR] Installation failed.
echo [ERROR] Please check the messages above and run install.bat again.
pause
exit /b 1

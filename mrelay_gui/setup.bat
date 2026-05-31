@echo off
chcp 65001 >nul
setlocal

for %%I in ("%~dp0.") do set "PROJECT_DIR=%%~fI"
set "VENV_DIR=%PROJECT_DIR%\.venv"
set "REQ_FILE=%PROJECT_DIR%\requirements.txt"

if not exist "%REQ_FILE%" (
    echo [ERROR] requirements.txt not found: %REQ_FILE%
    exit /b 1
)

pushd "%PROJECT_DIR%" || exit /b 1

where uv >nul 2>nul
if errorlevel 1 goto :use_venv

echo [INFO] Using uv
uv venv "%VENV_DIR%"
if errorlevel 1 goto :fail

uv pip install --python "%VENV_DIR%\Scripts\python.exe" -r "%REQ_FILE%"
if errorlevel 1 goto :fail
goto :verify

:use_venv
echo [INFO] uv not found; using python venv
set "PY_BOOT=py -3"
where py >nul 2>nul
if errorlevel 1 set "PY_BOOT=python"

if not exist "%VENV_DIR%\Scripts\python.exe" (
    call %PY_BOOT% -m venv "%VENV_DIR%"
    if errorlevel 1 goto :fail
)

"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :fail

"%VENV_DIR%\Scripts\python.exe" -m pip install -r "%REQ_FILE%"
if errorlevel 1 goto :fail

:verify
"%VENV_DIR%\Scripts\python.exe" -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] tkinter is not available in this Python installation.
    goto :fail
)

echo [OK] Virtual environment is ready: %VENV_DIR%
echo [INFO] Activate: "%VENV_DIR%\Scripts\activate.bat"
set "RC=0"
goto :done

:fail
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" set "RC=1"
echo [ERROR] setup failed: %RC%

:done
popd
endlocal & exit /b %RC%

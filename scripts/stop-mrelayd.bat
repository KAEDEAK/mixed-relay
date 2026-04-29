@echo off
REM Stop MixedRelay server by killing whatever LISTENs on the given TCP port.
REM Usage: stop-mrelayd.bat [port]
REM Default port: 6767  (override with arg or MRELAY_PORT env var)

setlocal enabledelayedexpansion
set "PORT=%~1"
if "%PORT%"=="" set "PORT=%MRELAY_PORT%"
if "%PORT%"=="" set "PORT=6767"

set "FOUND="
for /f "tokens=5" %%P in ('netstat -ano -p TCP ^| findstr /R /C:":%PORT% .*LISTENING"') do (
    set "FOUND=1"
    echo [stop-mrelayd] killing PID %%P listening on :%PORT%
    taskkill /PID %%P /F >nul 2>&1
    if errorlevel 1 (
        echo [stop-mrelayd] taskkill failed for PID %%P
    ) else (
        echo [stop-mrelayd] PID %%P terminated
    )
)

if not defined FOUND (
    echo [stop-mrelayd] no listener on port %PORT%
    exit /b 1
)
exit /b 0

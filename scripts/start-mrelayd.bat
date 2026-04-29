@echo off
REM MixedRelay v0.0.3 server launcher (Windows cmd) — localhost only
REM Usage: start-mrelayd.bat [listen_addr]
REM
REM Default binds to 127.0.0.1:6767 (loopback only). For LAN access,
REM use start-mrelayd_shared.bat instead.
REM
REM All wire I/O is echoed to stdout. Channel logs / cursors / archives
REM live under MRELAY_DATA (default: data/ next to the repo root).

setlocal
set "ROOT=%~dp0.."
pushd "%ROOT%"

if "%MRELAY_GO%"=="" (
    for /f "delims=" %%G in ('where go 2^>nul') do (
        set "MRELAY_GO=%%G"
        goto :found_go
    )
)
:found_go
if "%MRELAY_LISTEN%"=="" set "MRELAY_LISTEN=127.0.0.1:6767"
if not "%~1"=="" set "MRELAY_LISTEN=%~1"
if "%MRELAY_DATA%"=="" set "MRELAY_DATA=data"

if "%MRELAY_GO%"=="" (
    echo [ERROR] go not found in PATH.
    echo Install Go and add it to PATH, or set MRELAY_GO env var to go.exe path.
    popd
    exit /b 1
)
if not exist "%MRELAY_GO%" (
    echo [ERROR] go.exe not found at "%MRELAY_GO%"
    echo Set MRELAY_GO env var to your go.exe path.
    popd
    exit /b 1
)

echo [mrelayd] root = %ROOT%
echo [mrelayd] go   = %MRELAY_GO%
echo [mrelayd] addr = %MRELAY_LISTEN%
echo [mrelayd] data = %MRELAY_DATA%
echo.

"%MRELAY_GO%" run ./cmd/mrelayd --addr %MRELAY_LISTEN% --data %MRELAY_DATA%
set RC=%ERRORLEVEL%
popd
exit /b %RC%

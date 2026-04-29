@echo off
REM MixedRelay v0.0.3 server launcher (Windows cmd) — LAN/shared mode
REM Usage: start-mrelayd_shared.bat [listen_addr]
REM
REM Binds to 0.0.0.0:6767 by default so other hosts on the LAN can connect.
REM
REM SECURITY: MixedRelay has NO authentication and NO encryption.
REM Anyone on the network can join channels and read all messages.
REM Only run this on a trusted LAN. Never put secrets on the relay.
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
if "%MRELAY_LISTEN%"=="" set "MRELAY_LISTEN=0.0.0.0:6767"
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

echo [WARN] LAN/shared mode — listening on %MRELAY_LISTEN%
echo [WARN] No authentication, no encryption. Anyone on this network can read everything.
echo [WARN] Only use on a trusted LAN. Never put secrets on this relay.
echo.
echo [mrelayd] root = %ROOT%
echo [mrelayd] go   = %MRELAY_GO%
echo [mrelayd] addr = %MRELAY_LISTEN%
echo [mrelayd] data = %MRELAY_DATA%
echo.

"%MRELAY_GO%" run ./cmd/mrelayd --addr %MRELAY_LISTEN% --data %MRELAY_DATA%
set RC=%ERRORLEVEL%
popd
exit /b %RC%

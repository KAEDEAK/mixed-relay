@echo off
REM MixedRelay v0.0.3 server build (Windows cmd)
REM Compiles ./cmd/mrelayd to mrelayd.exe at the repo root.
REM Usage: build-mrelayd.bat

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

echo [build] root = %ROOT%
echo [build] go   = %MRELAY_GO%
echo [build] out  = %ROOT%\mrelayd.exe
echo.

"%MRELAY_GO%" build -o mrelayd.exe ./cmd/mrelayd
set RC=%ERRORLEVEL%
if %RC%==0 echo [build] OK
popd
exit /b %RC%

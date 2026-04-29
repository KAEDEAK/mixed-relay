@echo off
REM MixedRelay GUI client launcher (Windows cmd).
REM Double-click to start the Tkinter chat window.

setlocal
set "ROOT=%~dp0.."
pushd "%ROOT%"

if "%MRELAY_PY%"=="" set "MRELAY_PY=python"
if "%MRELAY_ADDR%"=="" set "MRELAY_ADDR=127.0.0.1:6767"
if "%MRELAY_NICK%"=="" set "MRELAY_NICK=alice"
if "%MRELAY_KIND%"=="" set "MRELAY_KIND=human"

echo [mrelay-gui] root = %ROOT%
echo [mrelay-gui] addr = %MRELAY_ADDR%
echo [mrelay-gui] nick = %MRELAY_NICK%
echo.

"%MRELAY_PY%" -m mrelay_gui
set RC=%ERRORLEVEL%
popd
if "%MRELAY_NOPAUSE%"=="" pause
exit /b %RC%

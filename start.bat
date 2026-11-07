@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title TokenLens
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY ( where py >nul 2>nul && set "PY=py -3" )
if not defined PY goto nopython

echo.
echo   TokenLens is starting ... the browser will open automatically.
echo   All data is read locally on this machine. Close this window to stop.
echo.
%PY% backend\server.py
if errorlevel 1 goto fail
exit /b 0

:nopython
echo.
echo   [x] Python not found.
echo       Install Python 3.9+ and tick "Add python.exe to PATH".
echo       https://www.python.org/downloads/
echo.
pause
exit /b 1

:fail
echo.
echo   [!] The server exited with an error. See the messages above.
pause
exit /b 1
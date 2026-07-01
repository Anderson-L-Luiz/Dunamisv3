@echo off
setlocal
cd /d "%~dp0"
title Dunamis v3
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY goto nopy
%PY% launcher.py
echo.
echo Server stopped. You can close this window.
pause
exit /b 0

:nopy
echo Python 3.11+ is required and was not found on PATH.
echo Install it from https://www.python.org/downloads/ and tick "Add Python to PATH".
pause
exit /b 1

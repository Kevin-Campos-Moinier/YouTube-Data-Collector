@echo off
rem Double-click to start the smdc dashboard (installs it on first use).
cd /d "%~dp0"
set "PY=py -3"
%PY% -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul || set "PY=python"
%PY% -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul || goto nopython
%PY% launch.py %*
if errorlevel 1 pause
exit /b
:nopython
echo Python 3.11 or newer is required. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup, then double-click this file again.
pause
exit /b 1

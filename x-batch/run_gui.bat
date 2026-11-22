@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 goto NOPY
python gui.py
if errorlevel 1 goto FAILED
exit /b 0
:NOPY
echo.
echo [ERROR] python not found.
echo Install Python 3.10+ and tick "Add python.exe to PATH".
echo.
pause
exit /b 1
:FAILED
echo.
echo [HINT] If it reports missing packages, run install_deps.bat first.
echo Details: startup_error.log
echo.
pause
exit /b 1

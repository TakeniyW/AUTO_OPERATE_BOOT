@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 goto NOPY
python main.py --warmup
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
echo [HINT] Check logs\warmup_*.log for details.
echo If ixBrowser is not running, start it and try again.
echo.
pause
exit /b 1

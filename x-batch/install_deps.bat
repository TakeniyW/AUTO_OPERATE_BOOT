@echo off
setlocal
cd /d "%~dp0"
echo ============================================
echo   Installing x-batch dependencies ...
echo ============================================
where python >nul 2>nul
if errorlevel 1 goto NOPY
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto MIRROR
goto DONE
:MIRROR
echo.
echo [WARN] Default index failed, retrying with Tsinghua mirror ...
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
:DONE
echo.
echo Install finished. Press any key to close.
pause >nul
exit /b 0
:NOPY
echo.
echo [ERROR] python not found.
echo Install Python 3.10+ and tick "Add python.exe to PATH".
echo Download: https://www.python.org/downloads/
echo.
pause
exit /b 1

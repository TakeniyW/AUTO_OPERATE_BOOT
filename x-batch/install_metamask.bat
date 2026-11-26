@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 goto NOPY
echo ============================================================
echo  Downloading and unpacking MetaMask to: extensions\metamask
echo  (sources: Chrome Web Store CRX, then MetaMask GitHub)
echo ============================================================
python modules\ext_install.py extensions\metamask
if errorlevel 1 goto FAILED
echo.
echo [OK] MetaMask is ready. Next time you open a profile it will be
echo      loaded automatically via --load-extension.
echo.
pause
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
echo [HINT] Download failed. Check your network, or manually unpack a
echo        MetaMask build into extensions\metamask (must contain manifest.json).
echo.
pause
exit /b 1

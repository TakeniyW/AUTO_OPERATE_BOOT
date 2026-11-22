@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 goto NOPYTHON

python gui.py
if errorlevel 1 goto FAILED
exit /b 0

:NOPYTHON
echo [错误] 没有找到 python，请先安装 Python 3.10 或更高版本。
echo        安装时务必勾选 "Add python.exe to PATH"。
echo        下载地址: https://www.python.org/downloads/
echo.
pause
exit /b 1

:FAILED
echo.
echo [提示] 如果上面提示缺少依赖，请先双击 install_deps.bat 安装依赖。
echo        详细错误已写入: startup_error.log
echo.
pause
exit /b 1

@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   正在安装 x-batch 依赖，请保持网络畅通 ...
echo ============================================
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto MIRROR
goto DONE

:MIRROR
echo.
echo [失败] 官方源安装失败，改用清华镜像重试 ...
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

:DONE
echo.
echo 安装结束，按任意键关闭。
pause >nul

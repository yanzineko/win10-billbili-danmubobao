@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 弹幕播报机 - 安装

if exist ".venv\Scripts\python.exe" goto ready

where python >nul 2>nul
if errorlevel 1 goto nopython

echo 第一次运行，正在创建运行环境...
python -m venv .venv
if errorlevel 1 goto novenv

echo 正在安装依赖，大概一两分钟...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto nodeps

:ready
echo.
echo 环境就绪，正在启动...
start "" ".venv\Scripts\pythonw.exe" main.py
exit /b 0

:nopython
echo.
echo 没找到 python。
echo 先去 python.org 装一个 3.10 以上的版本，安装时记得勾上 Add Python to PATH。
echo.
pause
exit /b 1

:novenv
echo.
echo 创建虚拟环境失败，看看上面报了什么。
pause
exit /b 1

:nodeps
echo.
echo 依赖装失败了，把上面的报错截图发我。
pause
exit /b 1

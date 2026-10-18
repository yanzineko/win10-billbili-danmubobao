@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 弹幕播报机

if not exist ".venv\Scripts\pythonw.exe" goto noenv

start "" ".venv\Scripts\pythonw.exe" main.py
exit /b 0

:noenv
echo 还没装运行环境，正在自动装一次...
call "%~dp0install.bat"
exit /b 0

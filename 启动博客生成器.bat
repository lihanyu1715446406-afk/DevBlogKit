@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动博客生成器...
python start_web.py
pause

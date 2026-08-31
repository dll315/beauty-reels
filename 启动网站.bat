@echo off
chcp 65001 >nul
title 小姐姐放映厅
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
  python server.py
  goto :eof
)
where py >nul 2>nul
if %errorlevel%==0 (
  py server.py
  goto :eof
)
echo [错误] 未找到 Python，请先安装 Python 3.7+：https://www.python.org/downloads/
echo 安装时请勾选 "Add Python to PATH"
pause

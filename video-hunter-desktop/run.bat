@echo off
chcp 65001 >nul
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
  echo [错误] 没有找到 python，请先安装 Python 3.10+ 并加入 PATH
  pause
  exit /b 1
)

python main.py %*
if errorlevel 1 pause

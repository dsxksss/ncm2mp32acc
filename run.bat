@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 优先使用本项目虚拟环境的 python，找不到则退回系统 python
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" ncm2acc.py %*

echo.
echo 程序已结束（关闭本窗口即可）。
pause

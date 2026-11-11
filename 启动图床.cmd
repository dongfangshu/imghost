@echo off
chcp 65001 >nul
title imghost - 内部图床
cd /d "%~dp0"

REM ============================================================
REM  双击这个文件即可启动图床。
REM  要改端口/目录，改下面两行就行。
REM ============================================================

set PORT=8899
set DIR=%~dp0images
set PY=python

%PY% -c "import sys" >nul 2>&1
if errorlevel 1 set PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe

"%PY%" imghost.py --port %PORT% --dir "%DIR%" --open

if errorlevel 1 (
    echo.
    echo   启动失败。
    echo   如果提示找不到 Python，请安装 Python 3（安装时勾选 Add to PATH），
    echo   或者用记事本打开本文件，把 PY 改成你机器上 python.exe 的完整路径。
    echo.
    pause
)

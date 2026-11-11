@echo off
chcp 65001 >nul
setlocal

REM 双击这个文件即可部署图床。它会自己申请管理员权限。

set SCRIPT=%~dp0deploy.ps1
if not exist "%SCRIPT%" (
    echo.
    echo   找不到 deploy.ps1，请确认它和本文件在同一个目录。
    echo.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*

if errorlevel 1 (
    echo.
    echo   部署没有成功，请把上面的红色错误信息发给维护人。
    echo.
    pause
)

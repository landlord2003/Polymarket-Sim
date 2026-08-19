@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 量化信号面板

set "PORT=8787"

echo ============================================
echo   量化信号面板 启动器
echo ============================================
echo.

REM ---- 1) 清理占用端口的旧进程，避免多实例抢端口跑旧代码 ----
echo [1/3] 清理旧的面板进程...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
    echo       已结束占用 %PORT% 端口的进程 PID=%%a
)

REM ---- 2) 定位可用的 python ----
echo [2/3] 定位 Python...
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined PY set "PY=python"
echo       使用: %PY%

REM ---- 3) 启动（浏览器会自动打开）----
echo [3/3] 启动服务，浏览器将自动打开 http://127.0.0.1:%PORT%
echo.
echo   * 关闭本窗口即停止服务
echo   * 若浏览器未自动打开，手动访问上面的地址
echo.
set "QT_WEB_PORT=%PORT%"
"%PY%" a_share/webui.py

echo.
echo 服务已停止。按任意键关闭窗口...
pause >nul

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
echo [1/4] 清理旧的面板进程...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
    echo       已结束占用 %PORT% 端口的进程 PID=%%a
)

REM ---- 2) 定位可用的 python ----
echo [2/4] 定位 Python...
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined PY set "PY=python"

if not exist "%PY%" (
    echo.
    echo [错误] 找不到可用的 Python: %PY%
    echo        请确认项目 .venv 或 WorkBuddy 托管环境已正确安装。
    pause
    exit /b 1
)
echo       使用: %PY%

REM ---- 3) 启动（浏览器会自动打开；日志写入 webui.log）----
echo [3/4] 启动服务，浏览器将自动打开 http://127.0.0.1:%PORT%
echo       启动日志: %~dp0webui.log
echo.
set "QT_WEB_PORT=%PORT%"
set "PYTHONUNBUFFERED=1"
"%PY%" a_share/webui.py > "%~dp0webui.log" 2>&1
set "EXITCODE=%ERRORLEVEL%"

REM ---- 4) 服务已退出 ----
echo.
if %EXITCODE% neq 0 (
    echo [错误] 服务异常退出，退出码: %EXITCODE%
    echo        请查看日志: %~dp0webui.log
    echo        常见原因：依赖未安装、端口被占、Python 环境损坏
) else (
    echo [信息] 服务已停止。
)
echo.
echo 按任意键关闭窗口...
pause >nul

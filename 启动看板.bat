@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined PY set "PY=python"
echo Starting quant trading dashboard, browser will open automatically...
"%PY%" a_share/webui.py
pause

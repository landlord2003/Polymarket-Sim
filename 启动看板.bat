@echo off
cd /d "%~dp0"
if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" (
  "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" launch_dashboard.py
) else (
  python launch_dashboard.py
)

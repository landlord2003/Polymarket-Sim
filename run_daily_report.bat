@echo off
REM =====================================================================
REM  Quant Trading - 收盘日报定时任务入口（本机 / 生产）
REM  - 定时任务(Windows 任务计划程序)只在本机跑，E:\Workbuddy\Quant-Trading
REM    仅作为测试机器人，不挂定时任务。
REM  - 使用 WorkBuddy 托管的 Python（envs/default venv），无需单独装环境。
REM  - 纯 ASCII 编写，避免 GBK 终端把中文/emoji 解析坏导致启动失败。
REM =====================================================================
cd /d D:\WorkBuddy\2026-08-19-09-07-31\quant-trading
set PYTHONPATH=

set PY="%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist %PY% set PY=python

%PY% a_share\run_daily.py --screener >> daily_report.log 2>&1

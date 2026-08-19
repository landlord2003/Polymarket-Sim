@echo off
cd /d E:\Workbuddy\Quant-Trading
set PYTHONPATH=
.venv\Scripts\python.exe a_share\run_daily.py --screener >> daily_report.log 2>&1

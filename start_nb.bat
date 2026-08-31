@echo off
REM Polymarket Sim NB 启动器（Windows 入口，保持 ASCII 避免 GBK 乱码）
REM 用法：start_nb.bat          直接起
REM       start_nb.bat --setup  先建 venv + 装依赖
REM 中文提示由 start_nb.py 输出；本文件仅做 ASCII 委派。
python start_nb.py %*

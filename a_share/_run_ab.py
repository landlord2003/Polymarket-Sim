"""单进程串行跑 proxy→real 两次回测（共享进程内 K 线缓存 + 基准磁盘缓存）。

用法：
  python -u _run_ab.py
输出：D:/WorkBuddy/output/ml_refined_proxy.md 与 ml_refined_real.md
同一进程内 _HIST_CACHE 共享，real 直接复用 proxy 已抓的 K 线（瞬时、标签一致、公平）。
"""
from __future__ import annotations
import sys
import ml_model as M

OUT = "D:/WorkBuddy/output"

print(">>> [1/2] proxy 回测（--money proxy）", flush=True)
sys.argv = ["ml_model.py", "--universe", "--money", "proxy",
            "--out", f"{OUT}/ml_refined_proxy.md"]
M.main()

print(">>> [2/2] real 回测（--money real，复用已抓 K 线）", flush=True)
sys.argv = ["ml_model.py", "--universe", "--money", "real",
            "--out", f"{OUT}/ml_refined_real.md"]
M.main()

print("=== AB DONE ===", flush=True)

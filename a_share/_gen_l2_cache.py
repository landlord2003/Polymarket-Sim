"""生成 L2 日频因子合成缓存（pipeline 全链路验证用）。

用法:
  python _gen_l2_cache.py --n 20 --days 600        # 给 core 前 20 只生成 600 日合成因子
  python _gen_l2_cache.py --all                     # 给 core 全 39 只
真实数据缓存请用 _validate_l2_real.py（AKShare 仅当日）。
"""
from __future__ import annotations
import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import l2_features as L2
import ml_model as M


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20, help="取 core 前 N 只")
    ap.add_argument("--all", action="store_true", help="core 全量 39 只")
    ap.add_argument("--days", type=int, default=2400, help="每只生成交易日数(匹配K线缓存长度)")
    ap.add_argument("--n-trades", type=int, default=400, help="每日合成逐笔笔数")
    args = ap.parse_args()

    syms = M.build_universe()
    if not args.all:
        syms = syms[:args.n]
    print(f"[gen-l2] 生成 {len(syms)} 只 × {args.days} 日合成 L2 因子缓存 (n_trades={args.n_trades})")
    ok = 0
    for (s, name, _) in syms:
        try:
            p = L2.gen_synth_daily_cache(s, n_days=args.days, n_trades=args.n_trades)
            ok += 1
        except Exception as e:
            print(f"  [warn] {s} {name} 失败: {repr(e)[:120]}")
    print(f"[gen-l2] 完成 {ok}/{len(syms)} 只 -> {L2.CACHE_DIR}")


if __name__ == "__main__":
    main()

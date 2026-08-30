# -*- coding: utf-8 -*-
"""
成交概率敏感性分析：回答「这套做市策略到底能不能用于实战」。

背景
----
旧看板（pairs 模式）假设每一笔挂单 100% 成交、且同轮双边建平，于是权益曲线一路向上。
那不是策略赚钱，是假设赚钱。本脚本把两个真实的约束加回去：

  A. 挂单不必然成交 —— 按价格改善幅度给成交概率（fill_prob）
  B. 真实库存管理   —— 每轮只尝试一腿，未平敞口跨轮持有，价格真实演化，
                       敞口承担波动风险，受止损与库存上限约束

只有价格真实变动，库存才有风险，「逆向选择」的损失才会显现出来。
因此本脚本每 PRICE_EVERY 轮拉取一次真实 Polymarket 盘口，而不是复用同一批报价。

用法
----
    .venv/Scripts/python.exe a_share/fill_sensitivity.py
    .venv/Scripts/python.exe a_share/fill_sensitivity.py --rounds 200 --every 8
"""
import argparse
import importlib.util
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import sim_rigor  # noqa: E402
from sim_rigor import RigorVirtualBook, rigor_params_from_config  # noqa: E402
import polymarket as P  # noqa: E402


def _load_simsrv():
    spec = importlib.util.spec_from_file_location(
        "simsrv", os.path.join(_HERE, "sim_server.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    # 测试期间禁止落盘，避免污染真实的统计流水
    m.save_trade = lambda *a, **k: None
    m.save_equity_sample = lambda *a, **k: None
    m.update_run_meta_round = lambda *a, **k: None
    return m


def new_book():
    b = RigorVirtualBook(rigor=rigor_params_from_config())
    b._save = lambda: None
    b._record_volume = lambda *a, **k: None
    b._save_caps = lambda: None
    b.max_skew = 300
    b.fee_rate = 0.005
    return b


def run_case(sim, mode, fill_base, rounds, price_every, init_eq=10000.0):
    """跑一组配置，返回统计结果。"""
    sim.SIM_MODE = mode
    sim.FILL_BASE = float(fill_base)
    sim.APPLY_FILL = True
    sim.book = new_book()

    rows = P.fetch_poly_quotes(limit=300, force=True)
    sim.MARKETS_LIVE = rows
    sim.MM_SET = [x["token_id"] for x in sim.select_mm(rows)]

    eq_curve = []
    attempts0 = sim.FILL_ATTEMPTS[0]
    hits0 = sim.FILL_HITS[0]
    t0 = time.time()
    for i in range(rounds):
        # 让价格真实演化：库存风险 / 逆向选择损失只有这样才能显现
        if price_every > 0 and i > 0 and i % price_every == 0:
            try:
                fresh = P.fetch_quotes_fresh(limit=300)
                if fresh:
                    sim.MARKETS_LIVE = fresh
            except Exception:
                pass
        sim.step()
        eq_curve.append(sim.STATE["equity"])

    s = sim.STATE
    att = sim.FILL_ATTEMPTS[0] - attempts0
    hit = sim.FILL_HITS[0] - hits0
    peak = max(eq_curve) if eq_curve else init_eq
    trough = min(eq_curve) if eq_curve else init_eq
    # 标准最大回撤
    rp = eq_curve[0] if eq_curve else init_eq
    mdd = 0.0
    for v in eq_curve:
        if v > rp:
            rp = v
        d = (rp - v) / rp * 100.0 if rp > 0 else 0.0
        if d > mdd:
            mdd = d
    return {
        "mode": mode,
        "fill_base": float(fill_base),
        "p_at_015": round(sim.fill_prob(0.15), 3),
        "rounds": rounds,
        "equity": round(s["equity"], 2),
        "realized": round(s["realized"], 2),
        "unrealized": round(s["unrealized"], 2),
        "fill_rate": round(hit / att * 100, 1) if att else 0.0,
        "attempts": att,
        "open_mkts": s["n_markets"],
        "inv_notional": round(s["inv_notional"], 2),
        "peak": round(peak, 2),
        "trough": round(trough, 2),
        "mdd_pct": round(mdd, 2),
        "secs": round(time.time() - t0, 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=120, help="每组跑多少轮")
    ap.add_argument("--every", type=int, default=0,
                    help="每多少轮刷新一次真实盘口（0=不刷新）。"
                         "注意：一次全量拉取约需 20 秒，且 Gamma 盘口在秒/分钟级很稳定"
                         "（实测 8 秒内 300 个市场价格变化为 0），因此默认关闭，"
                         "本脚本主要衡量成交概率与库存管理模式的影响")
    args = ap.parse_args()

    sim = _load_simsrv()
    cases = [
        ("pairs", 1.00),   # 旧行为：同轮双边建平 + 100% 成交（乐观上界）
        ("inv", 1.00),     # 真实库存管理，但假设必成交（隔离 B 的影响）
        ("inv", 0.60),
        ("inv", 0.45),
        ("inv", 0.30),     # 默认假设：挂在最优价时成交率 30%
        ("inv", 0.15),     # 悲观：挂在最优价时成交率仅 15%
    ]

    print("=" * 104)
    print("成交概率敏感性分析 · 真实 Polymarket 盘口 · "
          + ("价格每 %d 轮刷新一次" % args.every if args.every > 0
             else "价格静止（短周期内盘口本就稳定）"))
    print("=" * 104)
    print("%-6s %-9s %-7s %10s %11s %10s %9s %8s %8s" % (
        "模式", "FILL_BASE", "p(0.15)", "最终权益", "已实现", "浮动", "成交率", "回撤%", "耗时s"))
    print("-" * 104)

    results = []
    for mode, base in cases:
        r = run_case(sim, mode, base, args.rounds, args.every)
        results.append(r)
        print("%-6s %-9.2f %-7.3f %10.2f %11.2f %10.2f %8.1f%% %8.2f %8.1f" % (
            r["mode"], r["fill_base"], r["p_at_015"], r["equity"],
            r["realized"], r["unrealized"], r["fill_rate"],
            r["mdd_pct"], r["secs"]))
        sys.stdout.flush()

    print("-" * 104)
    base_eq = results[0]["equity"]
    print()
    print("相对旧模式（pairs + 100%成交）的收益保留率：")
    for r in results[1:]:
        keep = (r["equity"] - 10000.0) / (base_eq - 10000.0) * 100 if base_eq != 10000 else 0
        print("  %-6s base=%.2f  保留 %6.1f%%   （权益 $%.2f，回撤 %.2f%%，敞口 %d 个市场 / $%.0f）"
              % (r["mode"], r["fill_base"], keep, r["equity"], r["mdd_pct"],
                 r["open_mkts"], r["inv_notional"]))

    # 找盈亏平衡点
    neg = [r for r in results if r["equity"] < 10000.0]
    print()
    if neg:
        worst = min(neg, key=lambda r: r["equity"])
        print("结论：在 FILL_BASE <= %.2f（p(0.15)=%.2f）时策略已转亏，权益 $%.2f"
              % (worst["fill_base"], worst["p_at_015"], worst["equity"]))
        pos = [r for r in results if r["equity"] >= 10000.0]
        if pos:
            brk = min(pos, key=lambda r: r["fill_base"])
            print("      盈亏临界点约在 FILL_BASE = %.2f 附近（对应 p(0.15) ≈ %.2f）"
                  % (brk["fill_base"], brk["p_at_015"]))
    else:
        print("结论：本组参数区间内策略仍为正，但收益相对乐观假设大幅缩水。")
    print()
    print("注：FILL_BASE = 挂在市场最优价时的成交率；p(0.15) = 当前 adverse=0.15 挂单位的成交率。")
    print("    真实的成交率只能用真钱小额挂单测出来，这里的值是假设，用于看策略对它的敏感度。")


if __name__ == "__main__":
    main()

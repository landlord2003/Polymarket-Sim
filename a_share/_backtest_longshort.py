"""② 多空实盘扣费回测（基于 ③ 确认的排名信号）。

复用 _xsec.build_panel 构造横截面面板，walk-forward：
  - 每个 rebalance 日：用历史样本训练 LR，对当前截面打分、排序
  - 口径A（实盘可行）：买入打分 Top-N（等额权重），对比沪深300，
      扣 A股费用（佣金万2.5/单边 + 卖出印花税千1）
  - 口径B（理想多空）：Top-N 收益 − Bottom-N 收益（毛差，忽略做空可行性）

输出：净值曲线CSV + 指标JSON + 摘要打印。
"""
import os
import json
import numpy as np
import pandas as pd
from datetime import datetime

import _xsec as X
import ml_model as M

HERE = M.HERE
HORIZON = X.HORIZON
OUT_DIR = "D:/WorkBuddy/output"
COMM = 0.00025          # 佣金 万2.5/单边
STAMP = 0.001           # 卖出印花税 千1
N_LONG = 8              # 多/空头各 N 只


def fit_lr(Xtr, ytr):
    mu = np.nanmean(Xtr, axis=0)
    sd = np.where(np.nanstd(Xtr, axis=0) > 0, np.nanstd(Xtr, axis=0), 1.0)
    Xtr_s = (Xtr - mu) / sd
    m = M.LogisticRegression()
    m.fit(Xtr_s, ytr)
    return m, mu, sd


def main():
    mm, uni, days = "real", "core", 2400
    panel = X.build_panel(M.build_universe(), mm, days)
    if not panel:
        print("[warn] 无有效标的"); return

    # 沪深300 用作基准
    bench = M.fetch_benchmark_histories(days=days).get("沪深300")
    bclose = bench["close"].values.astype(float)
    bidx = {pd.Timestamp(d): j for j, d in enumerate(bench.index)}

    all_dates = sorted({d for p in panel for d in p["dates"]})
    reb, last_m, prev = [], None, None
    for d in all_dates:
        m = str(d)[:7]
        if m != last_m:
            if last_m is not None and prev is not None:
                reb.append(prev)
            last_m = m
        prev = d
    if prev is not None:
        reb.append(all_dates[-1])
    print(f"截面数: {len(reb)} | 标的: {len(panel)} | horizon={HORIZON} | N={N_LONG}")

    net_ret_A, bench_ret, ls_gross = [], [], []
    dates_out = []
    holding = []   # 每期持仓与收益明细
    for rb in reb:
        rb_ts = pd.Timestamp(rb)
        Xtr, ytr = [], []
        for p in panel:
            for j, d in enumerate(p["dates"]):
                if pd.Timestamp(d) < rb_ts:
                    Xtr.append(p["X_new"][j]); ytr.append(1.0 if p["fwd"][j] > 0 else 0.0)
        if len(Xtr) < M.MIN_TRAIN:
            continue
        Xtr = np.array(Xtr); ytr = np.array(ytr)
        m, mu, sd = fit_lr(Xtr, ytr)
        # 当前截面打分
        sc, sym, fwd = [], [], []
        for p in panel:
            best = -1
            for j, d in enumerate(p["dates"]):
                if pd.Timestamp(d) <= rb_ts: best = j
                else: break
            if best < 0: continue
            s = float(m.predict_proba(((p["X_new"][best] - mu) / sd).reshape(1, -1))[0])
            sc.append(s); sym.append(p["symbol"]); fwd.append(float(p["fwd"][best]))
        if len(sc) < 2 * N_LONG:
            continue
        order = np.argsort(sc)[::-1]
        long_i = order[:N_LONG]; short_i = order[-N_LONG:]
        # 口径A：Top-N 等额多头，对比沪深300
        r_long = float(np.mean([fwd[i] for i in long_i]))
        cost_A = 2 * COMM + STAMP          # 单边买+卖（含印花税）
        net_A = r_long - cost_A
        # 基准：沪深300 同期收益
        j0 = bidx.get(rb_ts)
        if j0 is None or j0 + HORIZON >= len(bclose):
            continue
        r_bench = bclose[j0 + HORIZON] / bclose[j0] - 1
        # 口径B：多空毛差
        r_short = float(np.mean([fwd[i] for i in short_i]))
        ls = r_long - r_short
        net_ret_A.append(net_A); bench_ret.append(r_bench); ls_gross.append(ls)
        dates_out.append(str(rb))
        holding.append({"reb": str(rb), "long": [sym[i] for i in long_i],
                        "r_long": round(r_long, 4), "net_A": round(net_A, 4),
                        "bench": round(r_bench, 4), "ls_gross": round(ls, 4)})

    net_ret_A = np.array(net_ret_A); bench_ret = np.array(bench_ret); ls_gross = np.array(ls_gross)
    # 净值曲线
    eq_A = np.cumprod(1 + net_ret_A)
    eq_bench = np.cumprod(1 + bench_ret)
    eq_ls = np.cumprod(1 + ls_gross)
    n = len(net_ret_A)
    yrs = n / 12.0
    ann_A = (eq_A[-1]) ** (1 / yrs) - 1 if yrs > 0 else 0
    ann_bench = (eq_bench[-1]) ** (1 / yrs) - 1 if yrs > 0 else 0
    # 超额
    excess = net_ret_A - bench_ret
    sharpe = float(excess.mean() / excess.std() * np.sqrt(12)) if excess.std() > 0 else 0.0
    # 最大回撤
    def mdd(eq):
        peak = np.maximum.accumulate(eq); return float(((eq - peak) / peak).min())
    out = {
        "money_mode": mm, "universe": uni, "days": days, "horizon": HORIZON,
        "n_cross_sections": n, "n_long": N_LONG,
        "comm": COMM, "stamp": STAMP,
        "eq_A_final": float(eq_A[-1]), "eq_bench_final": float(eq_bench[-1]),
        "eq_ls_final": float(eq_ls[-1]),
        "ann_return_A": float(ann_A), "ann_return_bench": float(ann_bench),
        "ann_excess": float((eq_A[-1] / eq_bench[-1]) ** (1 / yrs) - 1) if yrs > 0 else 0,
        "sharpe_excess": sharpe,
        "mdd_A": mdd(eq_A), "mdd_bench": mdd(eq_bench),
        "hit_long_vs_bench": float((net_ret_A > bench_ret).mean()),
        "mean_ls_gross": float(ls_gross.mean()),
        "holding": holding,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "ml_backtest_longshort.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    pd.DataFrame({"date": dates_out, "eq_A": eq_A, "eq_bench": eq_bench, "eq_ls": eq_ls,
                  "net_A": net_ret_A, "bench": bench_ret, "ls_gross": ls_gross}
                 ).to_csv(os.path.join(OUT_DIR, "ml_backtest_longshort.csv"), index=False)

    print(f"\n=== ② 多空实盘扣费回测 (real/core, horizon={HORIZON}, N={N_LONG}) ===")
    print(f"  期数: {n} (~{yrs:.1f}年) | 佣金万2.5 + 印花税千1")
    print(f"  多头组合净值(扣费): {eq_A[-1]:.3f}  年化 {ann_A*100:+.1f}%")
    print(f"  沪深300基准净值   : {eq_bench[-1]:.3f}  年化 {ann_bench*100:+.1f}%")
    print(f"  超额净值(相对HS300): {eq_A[-1]/eq_bench[-1]:.3f}  年化超额 {out['ann_excess']*100:+.1f}%")
    print(f"  超额夏普: {sharpe:.2f} | 多头跑赢基准胜率: {out['hit_long_vs_bench']*100:.1f}%")
    print(f"  最大回撤 多头: {out['mdd_A']*100:.1f}% | 基准: {out['mdd_bench']*100:.1f}%")
    print(f"  理想多空毛净值: {eq_ls[-1]:.3f} | 平均多空毛差: {ls_gross.mean()*100:+.2f}%/期")
    print(f"\n[done] -> D:/WorkBuddy/output/ml_backtest_longshort.json/.csv")


if __name__ == "__main__":
    main()

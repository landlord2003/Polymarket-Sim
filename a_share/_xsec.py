"""深化订单流微观结构（①）：非线性交互特征重测 IC。

对比 walk-forward 横截面下的四种配置（无未来函数）：
  (A) X_old  = 24维（5因子+9extra+10订单流）        —— 旧基线 IC≈0.0845
  (B) X_new  = 32维（+8维订单流非线性交互）          —— ① 主测试
  (C) X_full = 43维（+11维正交因子）                 —— 叠加 alt
  (D) GBT(X_new) = 非线性模型能否更好利用交互        —— 模型对比

主指标：横截面 IC（Spearman）、IC>0 占比、多-空每期收益、多空年化。
另报告 8 个交互特征与 11 个 alt 因子的单因子 IC（定位真 edge）。
"""
import os
import sys
import json
import numpy as np
import pandas as pd
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ml_model as M
from signal_engine import load_moneyflow_full, OF_INTERACT_NAMES
import alt_factors as AF

HERE = os.path.dirname(os.path.abspath(__file__))
HORIZON = 10
TOP_BOT = 0.30
OUT_DIR = "D:/WorkBuddy/output"
OLD_DIM = 24          # 旧版 X 维度（feat_vector14 + orderflow10）
INT_DIM = 8           # 新增交互维度


def spearman(x, y):
    def rank(a):
        a = np.asarray(a, float)
        order = a.argsort()
        ranks = np.empty(len(a), float)
        ranks[order] = np.arange(1, len(a) + 1)
        _, inv, counts = np.unique(a, return_inverse=True, return_counts=True)
        sums = np.zeros(len(counts))
        for i, c in enumerate(counts):
            if c > 1:
                sums[i] = (np.arange(1, c + 1)).sum() / c
        for i in range(len(counts)):
            if counts[i] > 1:
                ranks[inv == i] = sums[i]
        return ranks
    rx, ry = rank(x), rank(y)
    if rx.std() == 0 or ry.std() == 0:
        return 0.0
    return float(np.corrcoef(rx, ry)[0, 1])


def standardize(X, mu=None, sd=None):
    X = np.asarray(X, float)
    if mu is None:
        mu = np.nanmean(X, axis=0)
        sd = np.nanstd(X, axis=0)
    sd = np.where(sd > 0, sd, 1.0)
    return (X - mu) / sd, mu, sd


def build_panel(symbols, money_mode):
    """返回 list of dict：含 X_old(24)/X_new(32)/X_int(8)/X_full_old(35)/X_full_new(43)/alt(11)/fwd。"""
    money_cache = {}
    if money_mode == "real":
        for (s, _, _) in symbols:
            c = load_moneyflow_full(s)
            if c is not None:
                money_cache[s] = c
    bench_hist = M.fetch_benchmark_histories(days=400)
    hs300 = bench_hist.get("沪深300")
    panel = []
    for (s, name, sector) in symbols:
        df = M.load_hist(s)
        n = len(df)
        if n < M.START_DAYS + HORIZON + 1:
            continue
        rows = M.build_rows(s, bench_hist, hs300, HORIZON, money_cache=money_cache)
        if not rows:
            continue
        dates = [r["date"] for r in rows]
        X = np.array([r["X"] for r in rows], float)        # 32维（24+8）
        alt = AF.build(s, dates, df["close"]).values.astype(float)
        idx_map = {pd.Timestamp(d): j for j, d in enumerate(df.index)}
        closes_v = df["close"].values.astype(float)
        fwd = [closes_v[idx_map[pd.Timestamp(r["date"])] + HORIZON] / closes_v[idx_map[pd.Timestamp(r["date"])]] - 1
               for r in rows]
        X_old = X[:, :OLD_DIM]
        X_int = X[:, OLD_DIM:OLD_DIM + INT_DIM]
        X_full_old = np.hstack([X_old, alt])
        X_full_new = np.hstack([X, alt])
        panel.append({"symbol": s, "name": name, "dates": dates,
                      "X_old": X_old, "X_new": X, "X_int": X_int,
                      "X_full_old": X_full_old, "X_full_new": X_full_new,
                      "alt": alt, "fwd": np.array(fwd, float)})
    return panel


def fit_score_lr(Xtr, ytr, Xsc, Xref=None):
    Xtr_s, mu, sd = standardize(Xtr)
    m = M.LogisticRegression()
    m.fit(Xtr_s, ytr)
    if Xref is not None:
        Xsc = (Xsc - mu) / sd
    return np.array([float(m.predict_proba((Xsc[j:j + 1] - mu) / sd)[0]) for j in range(len(Xsc))])


def fit_score_gbt(Xtr, ytr, Xsc):
    m = M.GradientBoosting()
    m.fit(Xtr, ytr)
    return np.array([float(m.predict_proba(Xsc[j:j + 1])[0]) for j in range(len(Xsc))])


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--money", default="real", choices=["proxy", "real"])
    args = ap.parse_args()
    mm = args.money
    print(f"[{datetime.now():%H:%M:%S}] 构建横截面面板 (horizon={HORIZON}, money={mm}) ...")
    symbols = M.build_universe()
    panel = build_panel(symbols, mm)
    if not panel:
        print("  [warn] 无有效标的")
        return
    print(f"  有效标的：{len(panel)} 只 | old={panel[0]['X_old'].shape[1]} "
          f"new={panel[0]['X_new'].shape[1]} int={INT_DIM} alt={len(AF.FACTOR_NAMES)}")

    all_dates = set()
    for p in panel:
        all_dates.update(p["dates"])
    all_dates = sorted(all_dates)
    reb_dates, last_month, prev_d = [], None, None
    for d in all_dates:
        m = str(d)[:7]
        if m != last_month:
            if last_month is not None and prev_d is not None:
                reb_dates.append(prev_d)
            last_month = m
        prev_d = d
    if prev_d is not None:
        reb_dates.append(all_dates[-1])
    print(f"  截面(rebalance)数: {len(reb_dates)}")

    rec_old, rec_new, rec_full, rec_gbt = [], [], [], []
    per_int = {name: [] for name in OF_INTERACT_NAMES}
    per_alt = {name: [] for name in AF.FACTOR_NAMES}

    for k, rb in enumerate(reb_dates):
        rb_ts = pd.Timestamp(rb)
        # 训练集（rb 之前）
        Xtr_o, Xtr_n, Xtr_full, ytr = [], [], [], []
        for p in panel:
            ds = p["dates"]
            for j in range(len(ds)):
                if pd.Timestamp(ds[j]) < rb_ts:
                    Xtr_o.append(p["X_old"][j]); Xtr_n.append(p["X_new"][j])
                    Xtr_full.append(p["X_full_new"][j])
                    ytr.append(1.0 if p["fwd"][j] > 0 else 0.0)
        if len(Xtr_o) < M.MIN_TRAIN:
            continue
        Xtr_o = np.array(Xtr_o); Xtr_n = np.array(Xtr_n)
        Xtr_full = np.array(Xtr_full); ytr = np.array(ytr)

        # 评分集（<=rb 的最新一行）
        so, sn, sf, gi, ga, fwds = [], [], [], [], [], []
        for p in panel:
            best = -1
            for j, d in enumerate(p["dates"]):
                if pd.Timestamp(d) <= rb_ts:
                    best = j
                else:
                    break
            if best < 0:
                continue
            so.append(p["X_old"][best]); sn.append(p["X_new"][best])
            sf.append(p["X_full_new"][best]); gi.append(p["X_int"][best])
            ga.append(p["alt"][best]); fwds.append(float(p["fwd"][best]))
        if len(so) < 10:
            continue
        so = np.array(so); sn = np.array(sn); sf = np.array(sf)
        gi = np.array(gi); ga = np.array(ga); fwds = np.array(fwds)

        sc_old = fit_score_lr(Xtr_o, ytr, so)
        sc_new = fit_score_lr(Xtr_n, ytr, sn)
        sc_full = fit_score_lr(Xtr_full, ytr, sf)
        sc_gbt = fit_score_gbt(Xtr_n, ytr, sn)

        ic_old = spearman(sc_old, fwds)
        ic_new = spearman(sc_new, fwds)
        ic_full = spearman(sc_full, fwds)
        ic_gbt = spearman(sc_gbt, fwds)
        for ki, name in enumerate(OF_INTERACT_NAMES):
            per_int[name].append(spearman(gi[:, ki], fwds))
        for ki, name in enumerate(AF.FACTOR_NAMES):
            per_alt[name].append(spearman(ga[:, ki], fwds))

        rec_old.append({"reb": str(rb), "ic": ic_old, "n": len(so)})
        rec_new.append({"reb": str(rb), "ic": ic_new, "n": len(sn)})
        rec_full.append({"reb": str(rb), "ic": ic_full, "n": len(sf)})
        rec_gbt.append({"reb": str(rb), "ic": ic_gbt, "n": len(sn)})
        if k % 6 == 0:
            print(f"    {rb} n={len(so)} IC_old={ic_old:+.3f} IC_new={ic_new:+.3f} "
                  f"IC_full={ic_full:+.3f} IC_gbt={ic_gbt:+.3f}")

    if not rec_old:
        print("  [warn] 无足够截面")
        return

    def agg(recs):
        a = np.array([r["ic"] for r in recs])
        return float(a.mean()), float((a > 0).mean())

    mo_old, po_old = agg(rec_old)
    mo_new, po_new = agg(rec_new)
    mo_full, po_full = agg(rec_full)
    mo_gbt, po_gbt = agg(rec_gbt)
    pf_int = {n: float(np.mean(v)) for n, v in per_int.items()}
    pp_int = {n: float((np.array(v) > 0).mean()) for n, v in per_int.items()}
    pf_alt = {n: float(np.mean(v)) for n, v in per_alt.items()}
    pf_pos_alt = {n: float((np.array(v) > 0).mean()) for n, v in per_alt.items()}

    print("\n=== 订单流交互特征重测 (horizon=%d) ===" % HORIZON)
    print(f"  IC(A) X_old 24维      : {mo_old:+.4f}  IC>0 {(po_old*100):.1f}%   (旧基线)")
    print(f"  IC(B) X_new 32维(+交互): {mo_new:+.4f}  IC>0 {(po_new*100):.1f}%   Δ={mo_new-mo_old:+.4f}")
    print(f"  IC(C) X_full 43维      : {mo_full:+.4f}  IC>0 {(po_full*100):.1f}%")
    print(f"  IC(D) GBT(X_new)      : {mo_gbt:+.4f}  IC>0 {(po_gbt*100):.1f}%")
    print("  交互特征单因子 IC（按 |IC| 排序）：")
    for name in sorted(pf_int, key=lambda x: -abs(pf_int[x])):
        print(f"    {name:24s} IC={pf_int[name]:+.4f}  IC>0={(pp_int[name]*100):.1f}%")
    print("  alt 因子单因子 IC：")
    for name in sorted(pf_alt, key=lambda x: -abs(pf_alt[x])):
        print(f"    {name:24s} IC={pf_alt[name]:+.4f}  IC>0={(pf_pos_alt[name]*100):.1f}%")

    out = {
        "horizon": HORIZON, "top_bot": TOP_BOT, "money_mode": mm,
        "n_cross_sections": len(rec_old),
        "mean_ic_old_24": mo_old, "ic_pos_old": po_old,
        "mean_ic_new_32": mo_new, "ic_pos_new": po_new,
        "mean_ic_full_43": mo_full, "ic_pos_full": po_full,
        "mean_ic_gbt_32": mo_gbt, "ic_pos_gbt": po_gbt,
        "interaction_contrib": mo_new - mo_old,
        "per_interaction_ic": pf_int, "per_interaction_pos": pp_int,
        "per_alt_ic": pf_alt, "per_alt_pos": pf_pos_alt,
        "records_old": rec_old, "records_new": rec_new,
        "records_full": rec_full, "records_gbt": rec_gbt,
    }
    out_name = "ml_xsec_interact.json" if mm == "real" else "ml_xsec_interact_proxy.json"
    with open(os.path.join(OUT_DIR, out_name), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[done] -> D:/WorkBuddy/output/{out_name}")


if __name__ == "__main__":
    main()

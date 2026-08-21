"""横截面相对排名 + 正交因子增量回测（验证 ②：接独立信息源能否破天花板）。

三类对照（全部 walk-forward，无未来函数）：
  (A) 仅价量（24维 ML 打分）            —— 基线
  (B) 价量 + 11维正交因子（35维 ML 打分）—— 看 alt 是否提 IC / 多空
  (C) 11个 alt 因子的"单因子 IC"         —— 定位哪个渠道真有 edge

主指标：横截面 IC（打分/因子值与 fwd_ret 的 Spearman，按截面平均）、IC>0 占比、
多头-空头每期收益、多空年化。
"""
import os
import sys
import json
import numpy as np
import pandas as pd
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ml_model as M
from signal_engine import load_moneyflow_full
import alt_factors as AF

HERE = os.path.dirname(os.path.abspath(__file__))
HORIZON = 10
TOP_BOT = 0.30
OUT_DIR = "D:/WorkBuddy/output"


def spearman(x, y):
    """朴素 Spearman：rank 后 Pearson（处理重复秩）。"""
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
    """返回 list of dict：{symbol,name,dates,X_base(24),X_full(35),alt(11),fwd}。"""
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
        X = np.array([r["X"] for r in rows], float)            # 价量（24维）
        alt = AF.build(s, dates, df["close"]).values.astype(float)  # 正交（11维）
        Xa = np.hstack([X, alt])
        idx_map = {pd.Timestamp(d): j for j, d in enumerate(df.index)}
        closes_v = df["close"].values.astype(float)
        fwd = [closes_v[idx_map[pd.Timestamp(r["date"])] + HORIZON] / closes_v[idx_map[pd.Timestamp(r["date"])]] - 1
               for r in rows]
        panel.append({"symbol": s, "name": name, "dates": dates,
                      "X_base": X, "X_full": Xa, "alt": alt,
                      "fwd": np.array(fwd, float)})
    return panel


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
    print(f"  有效标的：{len(panel)} 只 | base_dim={panel[0]['X_base'].shape[1]} "
          f"full_dim={panel[0]['X_full'].shape[1]} | alt={len(AF.FACTOR_NAMES)}维")

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

    rec_base, rec_full = [], []
    per_factor = {name: [] for name in AF.FACTOR_NAMES}

    for k, rb in enumerate(reb_dates):
        rb_ts = pd.Timestamp(rb)
        Xtr_b, Xtr_f, ytr = [], [], []
        for p in panel:
            ds = p["dates"]
            for j in range(len(ds)):
                if pd.Timestamp(ds[j]) < rb_ts:
                    Xtr_b.append(p["X_base"][j])
                    Xtr_f.append(p["X_full"][j])
                    ytr.append(1.0 if p["fwd"][j] > 0 else 0.0)
        if len(Xtr_b) < M.MIN_TRAIN:
            continue
        Xtr_b = np.array(Xtr_b); Xtr_f = np.array(Xtr_f); ytr = np.array(ytr)
        Xtr_b_s, mub, sdb = standardize(Xtr_b)
        Xtr_f_s, muf, sdf = standardize(Xtr_f)
        mb = M.LogisticRegression(); mb.fit(Xtr_b_s, ytr)
        mf = M.LogisticRegression(); mf.fit(Xtr_f_s, ytr)

        sb, sf, fwds, alt_mat = [], [], [], []
        for p in panel:
            best = -1
            for j, d in enumerate(p["dates"]):
                if pd.Timestamp(d) <= rb_ts:
                    best = j
                else:
                    break
            if best < 0:
                continue
            sb.append(float(mb.predict_proba(standardize(p["X_base"][best:best + 1], mub, sdb)[0])[0]))
            sf.append(float(mf.predict_proba(standardize(p["X_full"][best:best + 1], muf, sdf)[0])[0]))
            fwds.append(float(p["fwd"][best]))
            alt_mat.append(p["alt"][best])
        sb = np.array(sb); sf = np.array(sf); fwds = np.array(fwds); alt_mat = np.array(alt_mat)
        if len(sb) < 10:
            continue
        ic_b = spearman(sb, fwds)
        ic_f = spearman(sf, fwds)
        for ki, name in enumerate(AF.FACTOR_NAMES):
            per_factor[name].append(spearman(alt_mat[:, ki], fwds))
        order = sf.argsort(); n = len(sf)
        lo = int(n * (1 - TOP_BOT)); hi = int(n * TOP_BOT)
        ls = float(fwds[order[hi:]].mean() - fwds[order[:lo]].mean())
        rec_base.append({"reb": str(rb), "ic": ic_b, "n": n})
        rec_full.append({"reb": str(rb), "ic": ic_f, "long_short": ls, "n": n})
        if k % 6 == 0:
            print(f"    {rb} n={n} IC_base={ic_b:+.3f} IC_full={ic_f:+.3f} L-S={ls:+.2%}")

    if not rec_base:
        print("  [warn] 无足够截面")
        return

    ic_b_all = np.array([r["ic"] for r in rec_base])
    ic_f_all = np.array([r["ic"] for r in rec_full])
    ls_all = np.array([r["long_short"] for r in rec_full])
    ann = (1 + ls_all.mean()) ** (252 / HORIZON) - 1 if ls_all.mean() > -1 else -1
    pf_mean = {name: float(np.mean(v)) for name, v in per_factor.items()}
    pf_pos = {name: float((np.array(v) > 0).mean()) for name, v in per_factor.items()}

    print("\n=== 正交因子增量回测结果 (horizon=%d) ===" % HORIZON)
    print(f"  平均 IC（仅价量）    : {ic_b_all.mean():+.4f}   IC>0占比 {(ic_b_all>0).mean()*100:.1f}%")
    print(f"  平均 IC（价量+alt）  : {ic_f_all.mean():+.4f}   IC>0占比 {(ic_f_all>0).mean()*100:.1f}%")
    print(f"  多-空 每期均收益      : {ls_all.mean()*100:+.2f}%   年化(近似) {ann*100:+.1f}%")
    print("  单因子 IC（按均值排序）：")
    for name in sorted(pf_mean, key=lambda x: -abs(pf_mean[x])):
        print(f"    {name:20s} IC={pf_mean[name]:+.4f}  IC>0={(pf_pos[name]*100):.1f}%")

    out = {
        "horizon": HORIZON, "top_bot": TOP_BOT, "money_mode": mm,
        "n_cross_sections": len(rec_base),
        "mean_ic_base": float(ic_b_all.mean()), "ic_pos_base": float((ic_b_all > 0).mean()),
        "mean_ic_full": float(ic_f_all.mean()), "ic_pos_full": float((ic_f_all > 0).mean()),
        "mean_long_short": float(ls_all.mean()), "annualized_ls": float(ann),
        "per_factor_ic": pf_mean, "per_factor_pos": pf_pos,
        "records_base": rec_base, "records_full": rec_full,
    }
    out_name = "ml_xsec_alt.json" if mm == "real" else "ml_xsec_base_proxy.json"
    with open(os.path.join(OUT_DIR, out_name), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print("\n[done] -> D:/WorkBuddy/output/ml_xsec_alt.json")


if __name__ == "__main__":
    main()

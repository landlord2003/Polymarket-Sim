"""扩池复核（③）：拉长历史 + 扩大股票池，确认 IC 稳健性、是否小样本过拟合。

两种运行（组合）：
  real  + core(39只, 有真实资金流)   -> 检验「订单流 edge」在长历史大截面是否稳健
  proxy + extended(110+只)           -> 检验「基础价量因子」在扩池下是否仍无 edge（稳健）

real 模式仅用 core 池：扩池股票当前无真实资金流数据（Tushare token 未配置 / 东财
资金流接口被 WAF 掐断），故 real 不扩池；core 池 K 线拉长到 ~10 年（days=2400），
并过滤到 moneyflow 起始日(2019)之后，保证标签与订单流对齐。
proxy 模式用 extended 池全历史，价量构造订单流，验证基础因子泛化。

输出：整体 IC / IC>0 占比 / t 值 / 分年度 IC，以及 X_old(24)/X_new(32)/X_full(43)/GBT 对照。
"""
import os
import sys
import json
import glob
import numpy as np
import pandas as pd
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ml_model as M
from signal_engine import load_moneyflow_full, OF_INTERACT_NAMES
import alt_factors as AF
import l2_features as L2

HERE = os.path.dirname(os.path.abspath(__file__))
HORIZON = 10
TOP_BOT = 0.30
OUT_DIR = "D:/WorkBuddy/output"
OLD_DIM = 24          # 旧版 X 维度（feat_vector14 + orderflow10）
INT_DIM = 8           # 新增交互维度

# 扩池：在 CORE_POOL(39只) 之外补充主流蓝筹/成长/各板块代表，去重后约 120 只。
EXTRA_POOL = [
    # 银行
    ("600036", "招商银行", "银行"), ("601166", "兴业银行", "银行"), ("600000", "浦发银行", "银行"),
    ("601398", "工商银行", "银行"), ("601328", "交通银行", "银行"), ("601288", "农业银行", "银行"),
    ("600919", "江苏银行", "银行"), ("002142", "宁波银行", "银行"),
    # 保险
    ("601318", "中国平安", "保险"), ("601628", "中国人寿", "保险"), ("601601", "中国太保", "保险"),
    # 券商
    ("600030", "中信证券", "券商"), ("600837", "海通证券", "券商"), ("601688", "华泰证券", "券商"),
    ("000776", "广发证券", "券商"), ("600999", "招商证券", "券商"), ("601211", "国泰君安", "券商"),
    # 白酒食饮
    ("600519", "贵州茅台", "消费"), ("000858", "五粮液", "消费"), ("000568", "泸州老窖", "消费"),
    ("600809", "山西汾酒", "消费"), ("000596", "古井贡酒", "消费"), ("603369", "今世缘", "消费"),
    ("600887", "伊利股份", "消费"),
    # 家电
    ("000333", "美的集团", "消费"), ("000651", "格力电器", "消费"), ("002032", "苏泊尔", "消费"),
    ("600690", "海尔智家", "消费"),
    # 医药
    ("600276", "恒瑞医药", "医药"), ("300760", "迈瑞医疗", "医药"), ("600196", "复星医药", "医药"),
    ("300347", "泰格医药", "医药"), ("600436", "片仔癀", "医药"), ("002821", "凯莱英", "医药"),
    ("300015", "爱尔眼科", "医药"), ("600763", "通策医疗", "医药"),
    # 汽车
    ("600104", "上汽集团", "汽车"), ("601238", "广汽集团", "汽车"), ("000625", "长安汽车", "汽车"),
    ("601633", "长城汽车", "汽车"), ("601127", "赛力斯", "汽车"),
    # 能源电力
    ("600028", "中国石化", "能源"), ("601857", "中国石油", "能源"), ("600900", "长江电力", "能源"),
    ("601985", "中国核电", "能源"), ("600905", "三峡能源", "能源"), ("600886", "国投电力", "能源"),
    # 化工材料
    ("600585", "海螺水泥", "材料"), ("600346", "恒力石化", "材料"), ("600309", "万华化学", "材料"),
    ("002493", "荣盛石化", "材料"), ("601216", "君正集团", "材料"),
    # 地产建筑
    ("000002", "万科A", "地产"), ("001979", "招商蛇口", "地产"), ("600048", "保利发展", "地产"),
    ("601668", "中国建筑", "地产"), ("601390", "中国中铁", "地产"), ("601186", "中国铁建", "地产"),
    ("601800", "中国交建", "地产"), ("601669", "中国电建", "地产"), ("600153", "建发股份", "地产"),
    # 机械
    ("600031", "三一重工", "机械"), ("000157", "中联重科", "机械"), ("601100", "恒立液压", "机械"),
    ("603338", "浙江鼎力", "机械"),
    # 通信/TMT
    ("000063", "中兴通讯", "TMT"), ("600050", "中国联通", "TMT"), ("600941", "中国移动", "TMT"),
    ("002241", "歌尔股份", "TMT"), ("300433", "蓝思科技", "TMT"), ("002475", "立讯精密", "TMT"),
    ("300782", "卓胜微", "TMT"), ("688981", "中芯国际", "TMT"),
    # 传媒互联
    ("300059", "东方财富", "TMT"), ("600570", "恒生电子", "TMT"), ("600588", "用友网络", "TMT"),
    ("002410", "广联达", "TMT"),
    # 农业
    ("002714", "牧原股份", "农业"), ("300498", "温氏股份", "农业"), ("600598", "北大荒", "农业"),
    # 免税
    ("601888", "中国中免", "消费"),
]


def extended_universe():
    base = M.build_universe()
    seen = {c: (c, n, s) for c, n, s in base}
    for c, n, s in EXTRA_POOL:
        if c not in seen:
            seen[c] = (c, n, s)
    return list(seen.values())


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


def build_panel(symbols, money_mode, days):
    """返回 list of dict：含 X_old(24)/X_new(32)/X_int(8)/X_full_old(35)/X_full_new(43)/alt(11)/fwd。"""
    money_cache, mstart = {}, {}
    if money_mode == "real":
        for (s, _, _) in symbols:
            c = load_moneyflow_full(s)
            if c is not None and len(c) > 0:
                money_cache[s] = c
                mstart[s] = str(c["trade_date"].min())
    bench_hist = M.fetch_benchmark_histories(days=400)
    hs300 = bench_hist.get("沪深300")
    panel = []
    skipped_synth = []
    for (s, name, sector) in symbols:
        df = M.load_hist(s, days)
        if getattr(df, "attrs", {}).get("synthetic"):
            skipped_synth.append(s)
            continue
        n = len(df)
        if n < M.START_DAYS + HORIZON + 1:
            continue
        rows = M.build_rows(s, bench_hist, hs300, HORIZON, money_cache=money_cache)
        if not rows:
            continue
        # real 模式：过滤到 moneyflow 起始日之后，保证订单流/标签对齐
        if money_mode == "real" and s in mstart:
            cut = pd.Timestamp(mstart[s])
            rows = [r for r in rows if pd.Timestamp(r["date"]) >= cut]
        if len(rows) < 5:
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
    if skipped_synth:
        print(f"  [skip-synth] 联网失败降级合成，已跳过 {len(skipped_synth)} 只: {skipped_synth[:12]}")
    return panel


def build_panel_l2(symbols, days, src="synth"):
    """L2 模式面板：特征由 l2_features.build 提供（L2_DIM 维），标签 fwd 由 K 线算。

    X_old = X_new = L2 特征；X_int 置零（L2 不做交互块）；X_full_* = hstack(L2, alt)。
    下游 walk-forward 与 daily 模式共用同一循环（panel 字典 schema 一致）。
    """
    bench_hist = M.fetch_benchmark_histories(days=400)
    hs300 = bench_hist.get("沪深300")
    panel = []
    for (s, name, sector) in symbols:
        df = M.load_hist(s, days)
        if getattr(df, "attrs", {}).get("synthetic"):
            continue
        n = len(df)
        if n < M.START_DAYS + HORIZON + 1:
            continue
        closes_v = df["close"].values.astype(float)
        valid, fwd = [], []
        for j in range(M.START_DAYS, n - HORIZON):
            if j + HORIZON >= n:
                continue
            valid.append(df.index[j])
            fwd.append(closes_v[j + HORIZON] / closes_v[j] - 1)
        if len(valid) < 5:
            continue
        l2 = L2.build(s, valid, df["close"], src=src)
        if l2 is None or len(l2) == 0:
            continue
        l2 = l2.loc[[pd.Timestamp(d) for d in valid]]
        if len(l2) != len(valid):
            continue
        X = l2.values.astype(float)
        if np.any(~np.isfinite(X)):
            X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
        dates = [pd.Timestamp(d) for d in valid]
        alt = AF.build(s, dates, df["close"]).values.astype(float)
        X_int = np.zeros((X.shape[0], INT_DIM))
        X_full_old = np.hstack([X, alt])
        X_full_new = np.hstack([X, alt])
        panel.append({"symbol": s, "name": name, "dates": dates,
                      "X_old": X, "X_new": X, "X_int": X_int,
                      "X_full_old": X_full_old, "X_full_new": X_full_new,
                      "alt": alt, "fwd": np.array(fwd, float)})
    return panel


def report_l2(panel, rec_new, days, src, tag):
    """L2 模式专属报告（与 daily 报告的 old/new/full 结构不同）。"""
    recs = rec_new
    if not recs:
        print("  [warn] L2 无足够截面")
        return
    a = np.array([r["ic"] for r in recs])
    mo = float(a.mean()); po = float((a > 0).mean())
    t_stat = float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a)))) if a.std() > 0 else 0.0
    years = {}
    for r in recs:
        years.setdefault(r["reb"][:4], []).append(r["ic"])
    year_stats = {y: {"mean_ic": float(np.mean(v)), "ic_pos": float((np.array(v) > 0).mean()),
                      "n": len(v)} for y, v in sorted(years.items())}
    Xall = np.vstack([p["X_new"] for p in panel])
    fall = np.concatenate([p["fwd"] for p in panel])
    per_feat = {n: float(spearman(Xall[:, c], fall)) for c, n in enumerate(L2.L2_NAMES)}
    print("\n=== L2 订单流因子 walk-forward IC (horizon=%d, src=%s, days=%d) ===" %
          (HORIZON, src, days))
    print(f"  标的={len(panel)} 截面数={len(recs)} L2维={Xall.shape[1]}")
    print(f"  IC(L2 12维模型) 整体: {mo:+.4f}  IC>0 {(po*100):.1f}%  t={t_stat:+.2f} (|t|>2≈p<0.05)")
    print("  分年度 IC：")
    for y, st in year_stats.items():
        print(f"    {y}: IC={st['mean_ic']:+.4f}  IC>0={(st['ic_pos']*100):.1f}%  n={st['n']}")
    print("  单因子 IC（L2 12 维，按 |IC| 排序）：")
    for name in sorted(per_feat, key=lambda x: -abs(per_feat[x])):
        print(f"    {name:18s} IC={per_feat[name]:+.4f}")
    print("  ⚠️ src=synth 为合成数据(无真实信号)，IC≈0 属预期，本跑仅验证 pipeline 不崩/维度对。")
    print("     src=akshare 仅当日1天，不能跑 walk-forward；真实 edge 验证需付费历史逐笔(财富通 600元/年)。")
    out = {"mode": "l2", "src": src, "horizon": HORIZON, "days": days,
           "n_symbols": len(panel), "n_cross_sections": len(recs),
           "ic_l2": mo, "ic_pos": po, "t_stat": t_stat,
           "year_stats": year_stats, "per_feature_ic": per_feat, "records": recs}
    tg = f"_{tag}" if tag else ""
    out_name = f"ml_xsec_l2_{src}_{days}{tg}.json"
    with open(os.path.join(OUT_DIR, out_name), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[done] -> D:/WorkBuddy/output/{out_name}")


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
    ap.add_argument("--days", type=int, default=2400)
    ap.add_argument("--universe", default="core", choices=["core", "extended"])
    ap.add_argument("--limit", type=int, default=0, help="只取 universe 前 N 只（分批跑用）")
    ap.add_argument("--offset", type=int, default=0, help="universe 切片偏移（分批跑用）")
    ap.add_argument("--tag", default="", help="输出文件名后缀，便于分批区分")
    ap.add_argument("--features", default="daily", choices=["daily", "l2"],
                    help="特征来源: daily=原日频(24+8维) | l2=逐笔订单流(L2_DIM维)")
    ap.add_argument("--l2-src", default="synth", choices=["synth", "akshare"],
                    help="l2 模式数据: synth=合成多日(全链路验证) | akshare=真实当日(仅解析校验)")
    args = ap.parse_args()
    mm, days, uni = args.money, args.days, args.universe

    # real 模式无扩池股票真实资金流 -> 强制 core
    if mm == "real" and uni == "extended":
        print("[warn] real 模式无扩池股票资金流，强制 core 池")
        uni = "core"
    symbols = M.build_universe() if uni == "core" else extended_universe()
    if args.offset:
        symbols = symbols[args.offset:]
    if args.limit:
        symbols = symbols[:args.limit]

    # 注：不再在此处删除 K 线缓存（会触发沙箱安全删除拦截导致中断）。
    # 长历史由独立的 _prefetch_kline.py 直接覆盖写缓存完成，_xsec 只读取。

    print(f"[{datetime.now():%H:%M:%S}] 构建横截面面板 (horizon={HORIZON}, money={mm}, "
          f"universe={uni}, days={days}, features={args.features}) ...")
    if args.features == "l2":
        panel = build_panel_l2(symbols, days, src=args.l2_src)
    else:
        panel = build_panel(symbols, mm, days)
    if not panel:
        print("  [warn] 无有效标的")
        return
    print(f"  有效标的：{len(panel)} 只 | 特征模式={args.features} | "
          f"X维={panel[0]['X_new'].shape[1]} int={INT_DIM} alt={len(AF.FACTOR_NAMES)}")

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
        if k % 12 == 0:
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

    # 分年度 IC（X_new）
    years = {}
    for r in rec_new:
        years.setdefault(r["reb"][:4], []).append(r["ic"])
    year_stats = {y: {"mean_ic": float(np.mean(v)), "ic_pos": float((np.array(v) > 0).mean()),
                      "n": len(v)} for y, v in sorted(years.items())}
    # 整体 t 值
    a = np.array([r["ic"] for r in rec_new])
    t_stat = float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a)))) if a.std() > 0 else 0.0

    if args.features == "l2":
        report_l2(panel, rec_new, days, args.l2_src, args.tag)
        return

    print("\n=== 扩池复核 (horizon=%d, money=%s, universe=%s, days=%d) ===" %
          (HORIZON, mm, uni, days))
    print(f"  截面数: {len(rec_old)}  |  IC_new 整体 t 值: {t_stat:+.2f} (|t|>2≈p<0.05)")
    print(f"  IC(A) X_old 24维      : {mo_old:+.4f}  IC>0 {(po_old*100):.1f}%   (旧基线)")
    print(f"  IC(B) X_new 32维(+交互): {mo_new:+.4f}  IC>0 {(po_new*100):.1f}%   Δ={mo_new-mo_old:+.4f}")
    print(f"  IC(C) X_full 43维      : {mo_full:+.4f}  IC>0 {(po_full*100):.1f}%")
    print(f"  IC(D) GBT(X_new)      : {mo_gbt:+.4f}  IC>0 {(po_gbt*100):.1f}%")
    print("  分年度 IC(B) X_new：")
    for y, st in year_stats.items():
        print(f"    {y}: IC={st['mean_ic']:+.4f}  IC>0={(st['ic_pos']*100):.1f}%  n={st['n']}")
    print("  交互特征单因子 IC（按 |IC| 排序）：")
    for name in sorted(pf_int, key=lambda x: -abs(pf_int[x])):
        print(f"    {name:24s} IC={pf_int[name]:+.4f}  IC>0={(pp_int[name]*100):.1f}%")
    print("  alt 因子单因子 IC：")
    for name in sorted(pf_alt, key=lambda x: -abs(pf_alt[x])):
        print(f"    {name:24s} IC={pf_alt[name]:+.4f}  IC>0={(pf_pos_alt[name]*100):.1f}%")

    out = {
        "horizon": HORIZON, "top_bot": TOP_BOT, "money_mode": mm,
        "universe": uni, "days": days,
        "n_symbols": len(panel), "n_cross_sections": len(rec_old),
        "t_stat_new": t_stat,
        "mean_ic_old_24": mo_old, "ic_pos_old": po_old,
        "mean_ic_new_32": mo_new, "ic_pos_new": po_new,
        "mean_ic_full_43": mo_full, "ic_pos_full": po_full,
        "mean_ic_gbt_32": mo_gbt, "ic_pos_gbt": po_gbt,
        "interaction_contrib": mo_new - mo_old,
        "per_interaction_ic": pf_int, "per_interaction_pos": pp_int,
        "per_alt_ic": pf_alt, "per_alt_pos": pf_pos_alt,
        "year_stats": year_stats,
        "records_old": rec_old, "records_new": rec_new,
        "records_full": rec_full, "records_gbt": rec_gbt,
    }
    tag = f"_{args.tag}" if args.tag else ""
    out_name = f"ml_xsec_expand_{mm}_{uni}_{days}{tag}.json"
    with open(os.path.join(OUT_DIR, out_name), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[done] -> D:/WorkBuddy/output/{out_name}")


if __name__ == "__main__":
    main()

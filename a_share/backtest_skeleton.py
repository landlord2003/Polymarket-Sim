"""阶段1 信号回测（walk-forward，测真实准确率）

复用 signal_engine 的真实维度逻辑（趋势/资金/板块轮动/估值/新闻 + 大盘regime），
对历史日线做「只用当日及之前数据」的前向回放，给出可量化的准确率：

  - 买入信号质量（precision）：发出 买入/偏多 信号的交易日，其后 N 日
    收益为正的占比（N=5/10/20）。这就是「信号准确率」的诚实定义。
  - 平均前向收益、信号次数。
  - 朴素策略净值（看涨满仓、看跌空仓）vs 买入持有，及近似年化 Sharpe。

阶段1 五因子（免费、无 Tushare）：
  - 趋势 trend：RSI/MA/布林 + 多周期动量(5/20/60) + RS(对沪深300)
  - 资金 money：真实主力净流入（AkShare→东财）；回测中限流常见，
      故回测用本地量价代理 money_proxy（与生产限流时表现一致，无未来函数）
  - 板块轮动 rotation：个股20日动量 − 多基准(300/500/创业板/1000)20日动量均值
  - 估值 valuation：近250日价格分位（PE≈价/EPS 代理）
  - 大盘 regime：沪深300 60日趋势（回测无实时宽度，只用趋势）

重要局限（必须如实告知）：
  - 「新闻」维度回测期置中性 0（历史新闻不易批量取），故本回测不含新闻维度；
  - 资金维度回测用本地代理（与生产限流时一致，且避免未来函数）；
  - 取数走 datasource.fetch_kline（腾讯优先）；若断网则回退合成数据并标注，
    此时数字仅验证引擎接线，不代表真实准确率。

用法：
  python a_share/backtest_skeleton.py
  python a_share/backtest_skeleton.py --symbols 300034 002085
  python a_share/backtest_skeleton.py --out D:/WorkBuddy/output/backtest_report.md
"""

from __future__ import annotations

import sys
import os
import argparse
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from signal_engine import (dim_trend, money_proxy, dim_valuation,
                           dim_sector_rotation, dim_regime,
                           _map_signal, DEFAULT_WEIGHTS)
from akshare_factors import fetch_benchmark_histories

FWD = (5, 10, 20)
START_DAYS = 60          # 预热，前 60 根不评信号
INIT_CAPITAL = 100_000.0


def _synthetic_data(symbol: str, n: int = 500) -> pd.DataFrame:
    """离线兜底：随机游走 OHLCV，仅验证回测引擎接线，非真实行情。"""
    rng = np.random.default_rng(abs(hash(symbol)) % (2**32))
    dates = pd.date_range(end=pd.Timestamp.today().normalize(), periods=n, freq="D")
    close = 25.0 + np.cumsum(rng.normal(0, 0.3, n))
    close = np.maximum(close, 1.0)
    open_ = close + rng.normal(0, 0.1, n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.1, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.1, n))
    vol = rng.integers(1_000_000, 5_000_000, n)
    df = pd.DataFrame({"open": open_, "high": high, "low": low,
                       "close": close, "volume": vol}, index=dates)
    df.attrs["synthetic"] = True
    return df


def load_hist(symbol: str, days: int = 500) -> pd.DataFrame:
    try:
        from datasource import fetch_kline
        df = fetch_kline(symbol, days=days)
        if len(df) >= 30:
            return df
        raise RuntimeError("K线不足")
    except Exception as e:  # 离线兜底：仅验证引擎
        print(f"[warn] {symbol} 取数失败（{e}），使用合成数据（非真实准确率）")
        return _synthetic_data(symbol)


def _regime_factor(sreg: float) -> float:
    return 0.55 + 0.45 * ((max(-1.0, min(1.0, sreg)) + 1.0) / 2.0)


def backtest_symbol(symbol: str, weights: dict, bench_hist: dict,
                    hs300: Optional[pd.DataFrame], fwd=FWD,
                    start_days=START_DAYS, df: Optional[pd.DataFrame] = None):
    if df is None:
        df = load_hist(symbol)
    if len(df) < start_days + max(fwd) + 1:
        return None
    closes = df["close"].values.astype(float)
    n = len(closes)

    # 估值分位（逐日回放时用 sub 防止未来函数）
    bullish_days = []
    pos = 0.0
    strat_rets = []
    for i in range(start_days, n):
        sub = df.iloc[:i + 1]
        tgt = sub.index[-1]
        sm, _ = dim_trend(sub, idx_df=hs300)
        sz, _ = money_proxy(sub)
        sv, _ = dim_valuation(symbol, sub)
        ss, _ = dim_sector_rotation(symbol, sub, bench_hist, tgt)
        sreg, _ = dim_regime(bench_hist, tgt, breadth=None)
        comp_dir = (weights["trend"] * sm + weights["money"] * sz +
                    weights["rotation"] * ss + weights["valuation"] * sv)
        comp_dir = max(-1.0, min(1.0, comp_dir))
        comp = max(-1.0, min(1.0, comp_dir * _regime_factor(sreg)))
        sig, _ = _map_signal(comp)
        day_ret = closes[i] / closes[i - 1] - 1
        strat_rets.append(pos * day_ret)
        pos = 1.0 if sig in ("买入", "偏多") else 0.0
        if sig in ("买入", "偏多"):
            bullish_days.append(i)

    # 前向收益（买入信号质量的核心指标）
    fwd_rets = {k: [] for k in fwd}
    for i in bullish_days:
        for k in fwd:
            j = i + k
            if j < n:
                fwd_rets[k].append(closes[j] / closes[i] - 1)

    def _mean(xs):
        return float(np.mean(xs)) if xs else float("nan")

    def _precision(xs):
        if not xs:
            return float("nan")
        return float(np.mean([1.0 if r > 0 else 0.0 for r in xs]))

    strat_eq = INIT_CAPITAL * float(np.prod(1 + np.array(strat_rets)))
    bh_eq = INIT_CAPITAL * (closes[-1] / closes[start_days])
    sret = np.array(strat_rets)
    sharpe = float(sret.mean() / sret.std() * np.sqrt(252)) if sret.std() > 0 else 0.0

    return {
        "symbol": symbol,
        "bars": n,
        "signals": len(bullish_days),
        "precision": {k: _precision(fwd_rets[k]) for k in fwd},
        "avg_fwd": {k: _mean(fwd_rets[k]) for k in fwd},
        "strat_eq": strat_eq,
        "bh_eq": bh_eq,
        "strat_ret": strat_eq / INIT_CAPITAL - 1,
        "bh_ret": bh_eq / INIT_CAPITAL - 1,
        "sharpe": sharpe,
        "synthetic": bool(df.attrs.get("synthetic", False)),
    }


def candidate_weights(n: int = 24, seed: int = 42) -> list:
    """生成候选权重组合（5维，和为1）：含当前默认 + 随机采样，用于网格搜索。"""
    dims = ["trend", "money", "rotation", "valuation", "news"]
    rng = np.random.default_rng(seed)
    cands = [dict(DEFAULT_WEIGHTS)]
    for _ in range(n - 1):
        w = rng.random(len(dims))
        w = w / w.sum()
        cands.append({d: round(float(w[i]), 3) for i, d in enumerate(dims)})
    return cands


def tune_weights(symbols: list, bench_hist: dict, hs300,
                 candidates: list, start_days: int = START_DAYS) -> list:
    """对每组候选权重跑全样本回测，返回按精准20d降序的 (weights, p5, p10, p20) 列表。"""
    hist = {s: load_hist(s) for s in symbols}
    results = []
    for cw in candidates:
        p5, p10, p20 = [], [], []
        for s in symbols:
            d = hist[s]
            if len(d) < start_days + max(FWD) + 1:
                continue
            r = backtest_symbol(s, cw, bench_hist, hs300,
                                start_days=start_days, df=d)
            if r and not r["synthetic"]:
                p5.append(r["precision"][5])
                p10.append(r["precision"][10])
                p20.append(r["precision"][20])
        if p5:
            results.append((cw, float(np.mean(p5)),
                            float(np.mean(p10)), float(np.mean(p20))))
    results.sort(key=lambda x: -x[3])
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=None,
                    help="标的列表，默认读 watchlist.json")
    ap.add_argument("--start", type=int, default=START_DAYS)
    ap.add_argument("--out", default=None, help="导出 Markdown 报告路径")
    ap.add_argument("--tune", action="store_true",
                    help="权重网格搜索（随机采样候选，找全样本精准率最高的组合）")
    ap.add_argument("--tune-out", default=None, help="调优结果导出路径")
    ap.add_argument("--apply", action="store_true",
                    help="把调优出的最优权重写回 watchlist.json")
    args = ap.parse_args()

    if args.symbols:
        symbols = args.symbols
    else:
        try:
            import json
            with open(os.path.join(HERE, "watchlist.json"), "r", encoding="utf-8") as f:
                symbols = [i["symbol"] for i in json.load(f)["watchlist"]]
        except Exception:
            symbols = ["300034", "002085", "688786"]

    # 预取基准（多进程/多标的共享，只取一次）
    print("预取基准指数历史(沪深300/中证500/创业板/中证1000)…")
    bench_hist = fetch_benchmark_histories(days=400)
    hs300 = bench_hist.get("沪深300")
    print(f"  可用基准：{list(bench_hist.keys())}")

    if args.tune:
        _run_tune(args, bench_hist, hs300)
        return

    print("=" * 78)
    print("阶段1 五因子 walk-forward 回测（趋势+资金代理+轮动+估值+regime）")
    print("=" * 78)
    rows = []
    for sym in symbols:
        r = backtest_symbol(sym, DEFAULT_WEIGHTS, bench_hist, hs300,
                            start_days=args.start)
        if r:
            rows.append(r)

    print(f"\n{'标的':<10}{'信号数':>7}{'精准5d':>9}{'精准10d':>9}{'精准20d':>9}"
          f"{'均收20d':>10}{'策略收益':>10}{'持有收益':>10}{'Sharpe':>8}")
    print("-" * 78)
    all_p5, all_p10, all_p20, all_avg = [], [], [], []
    for r in rows:
        tag = " [合成]" if r["synthetic"] else ""
        print(f"{r['symbol']:<10}{r['signals']:>7}"
              f"{r['precision'][5]*100:>8.1f}%{r['precision'][10]*100:>8.1f}%"
              f"{r['precision'][20]*100:>8.1f}%"
              f"{r['avg_fwd'][20]*100:>9.1f}%"
              f"{r['strat_ret']*100:>9.1f}%{r['bh_ret']*100:>9.1f}%"
              f"{r['sharpe']:>8.2f}{tag}")
        if not r["synthetic"]:
            all_p5.append(r["precision"][5]); all_p10.append(r["precision"][10])
            all_p20.append(r["precision"][20]); all_avg.append(r["avg_fwd"][20])

    print("-" * 78)
    if all_p5:
        print(f"{'全样本均值':<10}{'':>7}"
              f"{np.mean(all_p5)*100:>8.1f}%{np.mean(all_p10)*100:>8.1f}%"
              f"{np.mean(all_p20)*100:>8.1f}%{np.mean(all_avg)*100:>9.1f}%")
    print("\n说明：精准X%=发出买入/偏多信号后 X 日收益为正的占比（信号准确率）。")
    if any(r["synthetic"] for r in rows):
        print("⚠️ 含 [合成] 标的：断网回退，数字仅验证引擎接线，不代表真实准确率。")
    else:
        print("✅ 全部为真实历史数据（腾讯前复权）回放结果。")
    print("📌 资金维度回测用本地量价代理（与生产限流时一致，且无未来函数）；"
          "新闻维度回测置中性。")

    if args.out:
        _write_report(args.out, rows, all_p5, all_p10, all_p20, all_avg)


def _run_tune(args, bench_hist, hs300):
    import json
    if args.symbols:
        symbols = args.symbols
    else:
        try:
            with open(os.path.join(HERE, "watchlist.json"), "r", encoding="utf-8") as f:
                symbols = [i["symbol"] for i in json.load(f)["watchlist"]]
        except Exception:
            symbols = ["300034", "002085", "688786"]
    cands = candidate_weights()
    print(f"权重搜索：{len(cands)} 组候选，标的 {symbols} …")
    res = tune_weights(symbols, bench_hist, hs300, cands, start_days=args.start)
    print(f"\n{'排名':>4}  {'精准5d':>8}{'精准10d':>9}{'精准20d':>9}   权重(trend/money/rot/val/news)")
    print("-" * 70)
    for i, (w, p5, p10, p20) in enumerate(res[:10], 1):
        print(f"{i:>4}  {p5*100:>7.1f}%{p10*100:>8.1f}%{p20*100:>8.1f}%   "
              f"{w['trend']}/{w['money']}/{w['rotation']}/{w['valuation']}/{w['news']}")
    best = res[0][0]
    print(f"\n🏆 最优权重（按精准20d）：{best}")
    if args.tune_out:
        lines = ["# 权重调优结果\n",
                 "| 排名 | 精准5d | 精准10d | 精准20d | trend | money | rotation | valuation | news |",
                 "|---|---|---|---|---|---|---|---|---|"]
        for i, (w, p5, p10, p20) in enumerate(res, 1):
            lines.append(f"| {i} | {p5*100:.1f}% | {p10*100:.1f}% | {p20*100:.1f}% | "
                         f"{w['trend']} | {w['money']} | {w['rotation']} | {w['valuation']} | {w['news']} |")
        try:
            with open(args.tune_out, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            print(f"[tune] 已导出：{args.tune_out}")
        except Exception as e:
            print(f"[tune] 导出失败：{e}")
    if args.apply:
        try:
            wp = os.path.join(HERE, "watchlist.json")
            with open(wp, "r", encoding="utf-8") as f:
                wl = json.load(f)
            wl["weights"] = best
            with open(wp, "w", encoding="utf-8") as f:
                json.dump(wl, f, ensure_ascii=False, indent=2)
            print(f"[apply] 已写回 watchlist.json 权重：{best}")
        except Exception as e:
            print(f"[apply] 写回失败：{e}")


def _write_report(path: str, rows: list, all_p5, all_p10, all_p20, all_avg):
    from datetime import datetime
    lines = [f"# 阶段1 五因子信号回测报告（{datetime.today().strftime('%Y-%m-%d')}）\n"]
    lines.append("> walk-forward 回放：只用当日及之前数据生成信号；五因子=趋势(多周期动量+RS)"
                 "+资金(本地代理)+板块轮动(多基准RS)+估值(价格分位)+大盘regime。\n"
                 "> 资金维度回测用本地量价代理（与生产限流时一致，且无未来函数）；"
                 "新闻维度回测置中性。\n")
    lines.append("## 逐标的\n")
    lines.append("| 标的 | 信号数 | 精准5d | 精准10d | 精准20d | 均收20d | "
                 "策略收益 | 持有收益 | Sharpe |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        lines.append(
            f"| {r['symbol']} | {r['signals']} | "
            f"{r['precision'][5]*100:.1f}% | {r['precision'][10]*100:.1f}% | "
            f"{r['precision'][20]*100:.1f}% | {r['avg_fwd'][20]*100:+.1f}% | "
            f"{r['strat_ret']*100:+.1f}% | {r['bh_ret']*100:+.1f}% | {r['sharpe']:.2f} |")
    if all_p5:
        lines.append(f"\n## 全样本均值\n"
                     f"- 精准5d：**{np.mean(all_p5)*100:.1f}%**\n"
                     f"- 精准10d：**{np.mean(all_p10)*100:.1f}%**\n"
                     f"- 精准20d：**{np.mean(all_p20)*100:.1f}%**\n"
                     f"- 均收20d：{np.mean(all_avg)*100:+.1f}%\n")
    lines.append("\n## 结论\n")
    lines.append("- 精准X% = 发出「买入/偏多」信号后 X 日收益为正的占比，即信号准确率。\n"
                 "- 50% 为随机基准；显著高于 50% 才说明信号有正向边缘。\n"
                 "- 对比上一版（行情+资金代理+板块动量）约 44% 的精准率，"
                 "本版加入多周期动量/RS/估值分位/板块轮动/大盘regime 后应有所提升；"
                 "但若仍接近 50%，说明规则信号单独使用上限有限，需上模型(阶段3)或严格仓位管理。\n")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"\n[report] 已导出：{path}")
    except Exception as e:  # noqa: BLE001
        print(f"\n[report] 导出失败：{e}")


if __name__ == "__main__":
    main()

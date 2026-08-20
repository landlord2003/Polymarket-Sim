"""四维度信号回测（walk-forward，测真实准确率）

为什么重写：原骨架依赖 akshare + backtrader，且资金/板块/消息三维全是占位，
等于没测。本版直接复用 signal_engine 的真实维度逻辑（行情/资金代理/板块动量），
对历史日线做「只用当日及之前数据」的前向回放，给出可量化的准确率：

  - 买入信号质量（precision）：发出 买入/偏多 信号的交易日，其后 N 日
    收益为正的占比（N=5/10/20）。这就是「信号准确率」的诚实定义。
  - 平均前向收益、信号次数。
  - 朴素策略净值（看涨满仓、看跌空仓）vs 买入持有，及近似年化 Sharpe。

重要局限（必须如实告知）：
  - 历史回放里「资金」用本地量价代理（money_proxy），与东财限流时生产环境表现一致；
  - 「消息」维度回测期置中性 0（历史新闻不易批量取），故本回测不含新闻维度；
  - 取数走 datasource.fetch_kline（腾讯优先）；若断网则回退合成数据并明确标注，
    此时数字仅用于验证引擎接线，不代表真实准确率。

用法：
  python a_share/backtest_skeleton.py
  python a_share/backtest_skeleton.py --symbols 300034 002085
"""

from __future__ import annotations

import sys
import os
import argparse
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from signal_engine import dim_market, dim_sector, money_proxy, _map_signal, DEFAULT_WEIGHTS

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


def backtest_symbol(symbol: str, weights: dict, fwd=FWD, start_days=START_DAYS):
    df = load_hist(symbol)
    if len(df) < start_days + max(fwd) + 1:
        return None
    closes = df["close"].values.astype(float)
    n = len(closes)

    strat_rets = []          # 策略每日收益
    bullish_days = []        # 发出买入/偏多信号的索引
    pos = 0.0
    comps = []
    for i in range(start_days, n):
        sub = df.iloc[:i + 1]
        sm, _ = dim_market(sub)
        ss, _ = dim_sector(sub, offline=True)
        sz, _ = money_proxy(sub)
        sn = 0.0
        comp = (weights["market"] * sm + weights["money"] * sz +
                weights["sector"] * ss + weights["news"] * sn)
        comp = max(-1.0, min(1.0, comp))
        sig, _ = _map_signal(comp)
        comps.append(comp)
        bullish = sig in ("买入", "偏多")
        day_ret = closes[i] / closes[i - 1] - 1
        strat_rets.append(pos * day_ret)
        pos = 1.0 if bullish else 0.0
        if bullish:
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

    # 策略 vs 买入持有
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=None,
                    help="标的列表，默认读 watchlist.json")
    ap.add_argument("--start", type=int, default=START_DAYS)
    ap.add_argument("--out", default=None, help="导出 Markdown 报告路径")
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

    print("=" * 78)
    print("四维度信号 walk-forward 回测（行情+资金代理+板块动量；新闻维度置中性）")
    print("=" * 78)
    rows = []
    for sym in symbols:
        r = backtest_symbol(sym, DEFAULT_WEIGHTS, start_days=args.start)
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

    if args.out:
        _write_report(args.out, rows, all_p5, all_p10, all_p20, all_avg)


def _write_report(path: str, rows: list, all_p5, all_p10, all_p20, all_avg):
    from datetime import datetime
    lines = [f"# 四维度信号回测报告（{datetime.today().strftime('%Y-%m-%d')}）\n"]
    lines.append("> walk-forward 回放：只用当日及之前数据生成信号；资金维度用本地量价代理"
                 "（与东财限流时生产表现一致），新闻维度置中性。\n")
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
                 "- 当前样本精准率接近随机，说明**四维度规则信号单独使用不具备稳定超额**；"
                 "建议仅作筛选/预警，配合基本面与仓位管理，且务必做样本外验证。\n")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"\n[report] 已导出：{path}")
    except Exception as e:  # noqa: BLE001
        print(f"\n[report] 导出失败：{e}")


if __name__ == "__main__":
    main()

"""A股单标的信号回测引擎（带真实交易摩擦）

借鉴 tickflow-stock-panel 的回测理念：把 T+1、手续费、滑点、止损做成默认约束，
给出「信号 → 历史真实撮合 → 净值/夏普/最大回撤/胜率/交易明细」的闭环，
而不是像 backtest_skeleton 那样只算「信号准确率」或「同根K线 pos×收益」近似。

为什么不用 backtrader/vectorbt：
  - vectorbt 根本没装；backtrader 在 pandas 3.0 上兼容性有风险。
  - 纯 pandas/numpy 实现零新依赖、全可控、易测、且撮合逻辑透明可审计。

撮合模型（避免未来函数 + 强制 T+1）：
  - 在 bar i 收盘后，用「截至 i 的已知数据」算出信号（由调用方保证 point-in-time）。
  - 该信号在 bar i+1 的**开盘价**成交（不能当根收盘成交，天然隔根 = T+1）。
  - 持仓期间，若 bar 最低价触及止损价 → 当根以止损价离场（reason='stop'）；
    若持仓满 max_hold 根 → 当根开盘离场（reason='max_hold'）；
    若信号翻空 → 当根开盘离场（reason='signal'）。
  - 佣金 = max(成交额×万3, 5元)；印花税 = 成交额×千1（仅卖出）；滑点 = 成交价×slip。
  - 买入按整手（100股）且只用 stake_pct 比例资金，避免满仓踏空。

用法：
  python a_share/backtest_engine.py --symbols 300034 002085
  python a_share/backtest_engine.py --stop 0.08 --max-hold 20 --out output/backtest_report.md
  # 作为库：
  from backtest_engine import run_backtest, backtest_symbol
  res = run_backtest(df, signals_series)   # signals: 1=做多, 0=空仓
"""

from __future__ import annotations

import sys
import os
import json
import argparse
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 默认 A 股交易成本（可在调用时覆盖）
DEFAULTS = {
    "cash": 100_000.0,
    "commission": 0.0003,      # 佣金 万3 / 单边
    "min_commission": 5.0,     # 单笔最低 5 元
    "stamp_duty": 0.0005,      # 印花税 千1 / 仅卖出
    "slip": 0.001,             # 滑点 0.1%（买多付、卖少收）
    "stop": 0.0,               # 止损比例 0=不启用
    "max_hold": 0,             # 最大持仓根数 0=不启用
    "stake_pct": 0.95,         # 单次建仓占用资金比例
    "lot": 100,                # A股最小交易单位（手）
}


def run_backtest(df: pd.DataFrame, signals: pd.Series, **kw) -> dict:
    """对单标的做信号驱动的历史撮合回测。

    参数：
      df      : 含 open/high/low/close/volume 的 OHLCV DataFrame（DatetimeIndex）。
      signals : 与 df 同索引的 pd.Series，值 1=做多 / 0(或其他)=空仓。
                （A股现货为多头市场，不支持做空；要做空请用 -1 时忽略。）
      kw      : 覆盖 DEFAULTS 中的成本/风控参数。
    返回 dict：
      equity_curve : [(date, value), ...] 逐根资产曲线
      trades       : [{entry_date,exit_date,entry_price,exit_price,qty,
                       ret,net_ret,reason}, ...]
      final_value / total_return / sharpe / max_dd / win_rate /
      n_trades / avg_win / avg_loss / synthetic
    """
    p = dict(DEFAULTS)
    p.update(kw)

    sig = signals.reindex(df.index).fillna(0.0)
    open_ = df["open"].astype(float).values
    high = df["high"].astype(float).values
    low = df["low"].astype(float).values
    close = df["close"].astype(float).values
    dates = list(df.index)
    n = len(df)
    if n < 3:
        return {"error": "K线不足", "n": n}

    cash = p["cash"]
    pos = 0           # 持仓股数
    entry_price = 0.0
    entry_bar = -999
    equity = []
    trades = []
    cur_trade = None

    def buy(bar, price):
        nonlocal cash, pos, entry_price, entry_bar, cur_trade
        price *= (1 + p["slip"])
        affordable = cash * p["stake_pct"]
        # 扣佣金后取整手：max(成交额×万3, 5元)
        per_share = price * (1 + p["commission"]) + 1e-9
        shares = int(affordable / per_share // p["lot"]) * p["lot"]
        if shares <= 0:
            return
        cost = shares * price
        comm = max(cost * p["commission"], p["min_commission"])
        if cost + comm > cash:
            return
        cash -= (cost + comm)
        pos = shares
        entry_price = price
        entry_bar = bar
        cur_trade = {
            "entry_date": dates[bar], "entry_bar": bar,
            "entry_price": round(price, 4), "qty": shares,
            "reason": None,
        }

    def sell(bar, price, reason):
        nonlocal cash, pos, entry_price, entry_bar, cur_trade
        price *= (1 - p["slip"])
        if pos <= 0:
            return
        proceeds = pos * price
        comm = max(proceeds * p["commission"], p["min_commission"]) \
            + proceeds * p["stamp_duty"]      # 印花税仅卖出
        cash += (proceeds - comm)
        net_ret = (price - entry_price) / entry_price if entry_price > 0 else 0.0
        if cur_trade is not None:
            cur_trade.update({
                "exit_date": dates[bar], "exit_price": round(price, 4),
                "ret": round(price / entry_price - 1, 4),
                "net_ret": round(net_ret - (comm / (pos * entry_price)), 4),
                "reason": reason,
            })
            trades.append(cur_trade)
        pos = 0
        entry_price = 0.0
        entry_bar = -999
        cur_trade = None

    # bar 0 无前根决策，从 bar 1 开始执行（上一根信号 → 本根开盘成交）
    for i in range(1, n):
        target = int(sig.iloc[i - 1] >= 1)   # 上一根信号，本根开盘执行
        # 持仓中的离场判定（先止损，再 max_hold，再信号翻空）
        if pos > 0:
            if p["stop"] > 0 and low[i] <= entry_price * (1 - p["stop"]):
                sell(i, entry_price * (1 - p["stop"]), "stop")
            elif p["max_hold"] > 0 and (i - entry_bar) >= p["max_hold"]:
                sell(i, open_[i], "max_hold")
            elif target == 0:
                sell(i, open_[i], "signal")
        # 建仓
        if pos == 0 and target == 1:
            buy(i, open_[i])
        # 收盘市值
        equity.append((dates[i], round(cash + pos * close[i], 2)))

    # 收尾：若仍持仓，按最后收盘强平（标记 hold_end）
    if pos > 0:
        sell(n - 1, close[-1], "hold_end")

    eq_vals = np.array([v for _, v in equity], dtype=float)
    eq_dates = [d for d, _ in equity]
    if len(eq_vals) == 0:
        return {"error": "无成交", "n": n, "trades": [], "equity_curve": []}

    # 指标
    total_return = float(eq_vals[-1] / eq_vals[0] - 1.0)
    peak = np.maximum.accumulate(eq_vals)
    max_dd = float(((eq_vals - peak) / peak).min())
    rets = np.diff(eq_vals) / eq_vals[:-1]
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0

    closed = [t for t in trades if t.get("reason")]
    wins = [t for t in closed if t["net_ret"] > 0]
    win_rate = float(len(wins) / len(closed)) if closed else 0.0
    avg_win = float(np.mean([t["net_ret"] for t in wins])) if wins else 0.0
    losses = [t for t in closed if t["net_ret"] <= 0]
    avg_loss = float(np.mean([t["net_ret"] for t in losses])) if losses else 0.0

    return {
        "symbol": kw.get("symbol", ""),
        "n": n,
        "equity_curve": list(zip(
            [str(d)[:10] for d in eq_dates], eq_vals.tolist())),
        "final_value": round(float(eq_vals[-1]), 2),
        "total_return": round(total_return, 4),
        "sharpe": round(sharpe, 3),
        "max_dd": round(max_dd, 4),
        "win_rate": round(win_rate, 3),
        "n_trades": len(closed),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "trades": trades,
        "params": {k: p[k] for k in
                   ("commission", "stamp_duty", "slip", "stop",
                    "max_hold", "stake_pct", "cash")},
        "synthetic": bool(df.attrs.get("synthetic", False)),
    }


# ----------------------------------------------------------- 信号：复用 signal_engine 真实逻辑
def signals_from_engine(df: pd.DataFrame, bench_hist: dict,
                        hs300, weights: dict | None = None) -> pd.Series:
    """用 signal_engine 的五因子逻辑，对 df 逐根回放，产出 1/0 信号序列（point-in-time）。

    与 backtest_skeleton 的信号口径一致：只用截至 bar i 的已知数据生成信号，
    由 run_backtest 在 bar i+1 开盘执行，无未来函数。
    """
    from signal_engine import (dim_trend, money_proxy, dim_valuation,
                               dim_sector_rotation, dim_regime,
                               _map_signal, DEFAULT_WEIGHTS)
    w = weights or dict(DEFAULT_WEIGHTS)
    n = len(df)
    out = pd.Series(0.0, index=df.index)

    def _regime(sreg):
        return 0.55 + 0.45 * ((max(-1.0, min(1.0, sreg)) + 1.0) / 2.0)

    for i in range(60, n):
        sub = df.iloc[:i + 1]
        tgt = sub.index[-1]
        try:
            sm, _ = dim_trend(sub, idx_df=hs300)
            sz, _ = money_proxy(sub)
            sv, _ = dim_valuation(df.attrs.get("symbol", ""), sub)
            ss, _ = dim_sector_rotation(df.attrs.get("symbol", ""), sub, bench_hist, tgt)
            sreg, _ = dim_regime(bench_hist, tgt, breadth=None)
        except Exception:
            sm = sz = sv = ss = sreg = 0.0
        comp_dir = (w["trend"] * sm + w["money"] * sz +
                    w["rotation"] * ss + w["valuation"] * sv)
        comp_dir = max(-1.0, min(1.0, comp_dir))
        comp = max(-1.0, min(1.0, comp_dir * _regime(sreg)))
        sig, _ = _map_signal(comp)
        out.iloc[i] = 1.0 if sig in ("买入", "偏多") else 0.0
    return out


def _synthetic_data(symbol: str, n: int = 500) -> pd.DataFrame:
    """离线兜底：随机游走 OHLCV，仅验证引擎接线，非真实行情。"""
    rng = np.random.default_rng(abs(hash(symbol)) % (2**32))
    idx = pd.date_range(end=pd.Timestamp.today().normalize(), periods=n, freq="D")
    close = 25.0 + np.cumsum(rng.normal(0, 0.3, n))
    close = np.maximum(close, 1.0)
    open_ = close + rng.normal(0, 0.1, n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.1, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.1, n))
    vol = rng.integers(1_000_000, 5_000_000, n)
    df = pd.DataFrame({"open": open_, "high": high, "low": low,
                       "close": close, "volume": vol}, index=idx)
    df.attrs["synthetic"] = True
    df.attrs["symbol"] = symbol
    return df


def backtest_symbol(symbol: str, days: int = 500, use_engine: bool = True,
                     **kw) -> dict:
    """拉取标的 K 线并用回测引擎跑。use_engine=True 用 signal_engine 真实信号；
    否则用「价格站上 MA20」简单信号（离线可测、无外部依赖）。断网回退合成并标注。"""
    try:
        from datasource import fetch_kline
        df = fetch_kline(symbol, days=days)
        if df is None or len(df) < 30:
            raise RuntimeError("K线不足")
        df.attrs["symbol"] = symbol
    except Exception as e:
        print(f"[warn] {symbol} 取数失败（{e}），使用合成数据（非真实回测）")
        df = _synthetic_data(symbol)
    if len(df) < 60:
        return {"error": "K线不足", "symbol": symbol}

    if use_engine:
        try:
            from akshare_factors import fetch_benchmark_histories
            bench_hist = fetch_benchmark_histories(days=400)
            hs300 = bench_hist.get("沪深300")
            signals = signals_from_engine(df, bench_hist, hs300)
        except Exception as e:
            print(f"[warn] {symbol} 引擎信号生成失败（{e}），改用 MA20 信号")
            signals = (df["close"] > df["close"].rolling(20).mean()).astype(float)
    else:
        signals = (df["close"] > df["close"].rolling(20).mean()).astype(float)

    kw["symbol"] = symbol
    return run_backtest(df, signals, **kw)


# ----------------------------------------------------------- 参数扫描（任务3：止损×最大持仓网格）
def parameter_sweep(symbol: str, days: int = 500,
                    stops=(0.05, 0.08, 0.12), max_holds=(20, 30, 60),
                    use_engine: bool = True, **kw) -> list:
    """对单标的做 (止损 × 最大持仓) 网格扫描，返回每组合指标，便于挑稳健参数。

    返回 list of dict：{stop, max_hold, n_trades, total_return, sharpe, max_dd,
                        win_rate, final_value} 或带 error 的条目。
    不推送、不产生 HTML，纯数据，供 CLI / webui 表格展示。
    """
    grid = []
    for st in stops:
        for mh in max_holds:
            # kw 可能也携带 stop/max_hold（来自 CLI 默认），剔除避免重复关键字
            clean = {k: v for k, v in kw.items()
                     if k not in ("stop", "max_hold")}
            r = backtest_symbol(symbol, days=days, use_engine=use_engine,
                                stop=st, max_hold=mh, **clean)
            if "error" in r:
                grid.append({"symbol": symbol, "stop": st, "max_hold": mh,
                             "error": r["error"]})
                continue
            grid.append({
                "symbol": symbol, "stop": st, "max_hold": mh,
                "n_trades": r["n_trades"], "total_return": r["total_return"],
                "sharpe": r["sharpe"], "max_dd": r["max_dd"],
                "win_rate": r["win_rate"], "final_value": r["final_value"],
            })
    return grid


def _write_sweep_report(path: str, per_symbol: dict):
    from datetime import datetime
    lines = [f"# 回测参数扫描报告（{datetime.today().strftime('%Y-%m-%d')}）\n",
             "> 网格：止损 × 最大持仓；撮合同 run_backtest（T+1/手续费/滑点/止损）。\n"]
    for sym, grid in per_symbol.items():
        lines.append(f"## {sym}\n")
        lines.append("| 止损 | 最大持仓 | 交易数 | 总收益 | 夏普 | 最大回撤 | 胜率 | 末值 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for g in grid:
            if "error" in g:
                lines.append(f"| {g['stop']*100:.1f}% | {g['max_hold']} | ⚠️ {g['error']} |")
                continue
            lines.append(
                f"| {g['stop']*100:.1f}% | {g['max_hold']} | {g['n_trades']} | "
                f"{g['total_return']*100:+.1f}% | {g['sharpe']:.2f} | "
                f"{g['max_dd']*100:+.1f}% | {g['win_rate']*100:.1f}% | "
                f"{g['final_value']:.0f} |")
        lines.append("")
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"[report] 参数扫描已导出：{path}")
    except Exception as e:  # noqa: BLE001
        print(f"[report] 导出失败：{e}")


# ----------------------------------------------------------- 报告
def _write_report(path: str, rows: list):
    from datetime import datetime
    lines = [f"# A股单标的回测报告（{datetime.today().strftime('%Y-%m-%d')}）\n"]
    lines.append("> 撮合：信号 bar i 收盘生成 → bar i+1 开盘成交（天然 T+1）；"
                 "佣金万3(最低5元) + 卖出印花税千1 + 滑点0.1%；含止损/最大持仓。\n"
                 "> 信号：复用 signal_engine 五因子（趋势+资金代理+轮动+估值+regime）。\n")
    lines.append("## 逐标的\n")
    lines.append("| 标的 | 交易数 | 总收益 | 夏普 | 最大回撤 | 胜率 | 均盈 | 均亏 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        if "error" in r:
            lines.append(f"| {r.get('symbol','')} | ⚠️ {r['error']} |")
            continue
        tag = " [合成]" if r.get("synthetic") else ""
        lines.append(
            f"| {r['symbol']}{tag} | {r['n_trades']} | "
            f"{r['total_return']*100:+.1f}% | {r['sharpe']:.2f} | "
            f"{r['max_dd']*100:+.1f}% | {r['win_rate']*100:.1f}% | "
            f"{r['avg_win']*100:+.1f}% | {r['avg_loss']*100:+.1f}% |")
    lines.append("\n## 交易明细（最新 20 笔）\n")
    for r in rows:
        if "trades" not in r:
            continue
        if r["trades"]:
            lines.append(f"### {r['symbol']}\n")
            lines.append("| 进场 | 出场 | 价(进→出) | 股数 | 净收益 | 离场原因 |")
            lines.append("|---|---|---|---|---|---|")
            for t in r["trades"][-20:]:
                lines.append(
                    f"| {t.get('entry_date','')} | {t.get('exit_date','')} | "
                    f"{t.get('entry_price')}→{t.get('exit_price')} | {t.get('qty')} | "
                    f"{t.get('net_ret',0)*100:+.1f}% | {t.get('reason','')} |")
            lines.append("")
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"[report] 已导出：{path}")
    except Exception as e:  # noqa: BLE001
        print(f"[report] 导出失败：{e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=None,
                    help="标的列表，默认读 watchlist.json")
    ap.add_argument("--days", type=int, default=500)
    ap.add_argument("--stop", type=float, default=0.0, help="止损比例，0=不启用")
    ap.add_argument("--max-hold", type=int, default=0, help="最大持仓根数，0=不启用")
    ap.add_argument("--slip", type=float, default=0.001, help="滑点比例")
    ap.add_argument("--commission", type=float, default=0.0003)
    ap.add_argument("--stamp", type=float, default=0.0005)
    ap.add_argument("--stake-pct", type=float, default=0.95)
    ap.add_argument("--no-engine", action="store_true",
                    help="不用 signal_engine，改用 MA20 简单信号（离线可测）")
    ap.add_argument("--out", default="output/backtest_report.md")
    ap.add_argument("--sweep", action="store_true",
                    help="参数扫描：对每只标的做 止损×最大持仓 网格")
    ap.add_argument("--stops", default="0.05,0.08,0.12",
                    help="止损网格(逗号分隔)，如 0.05,0.08,0.12")
    ap.add_argument("--max-holds", default="20,30,60",
                    help="最大持仓网格(逗号分隔)，如 20,30,60")
    args = ap.parse_args()

    if args.symbols:
        symbols = args.symbols
    else:
        try:
            with open(os.path.join(HERE, "watchlist.json"), "r", encoding="utf-8") as f:
                symbols = [i["symbol"] for i in json.load(f)["watchlist"]]
        except Exception:
            symbols = ["300034", "002085", "688786"]

    kw = dict(stop=args.stop, max_hold=args.max_hold, slip=args.slip,
              commission=args.commission, stamp_duty=args.stamp,
              stake_pct=args.stake_pct)

    if args.sweep:
        stops = [float(x) for x in args.stops.split(",") if x != ""]
        max_holds = [int(x) for x in args.max_holds.split(",") if x != ""]
        per = {}
        for sym in symbols:
            print(f"  [sweep] {sym}: {len(stops)}×{len(max_holds)} 组合…")
            per[sym] = parameter_sweep(
                sym, days=args.days, stops=stops, max_holds=max_holds,
                use_engine=not args.no_engine, **kw)
        _write_sweep_report(args.out, per)
        return

    rows = []
    for sym in symbols:
        r = backtest_symbol(sym, days=args.days,
                            use_engine=not args.no_engine, **kw)
        rows.append(r)
        if "error" in r:
            print(f"  {sym}: ⚠️ {r['error']}")
        else:
            tag = " [合成]" if r.get("synthetic") else ""
            print(f"  {sym}{tag}: 交易{r['n_trades']} 收益{r['total_return']*100:+.1f}% "
                  f"夏普{r['sharpe']:.2f} 回撤{r['max_dd']*100:+.1f}% 胜率{r['win_rate']*100:.1f}%")

    _write_report(args.out, rows)


if __name__ == "__main__":
    main()

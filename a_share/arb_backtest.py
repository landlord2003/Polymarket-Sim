# -*- coding: utf-8 -*-
"""Polymarket 做市策略历史回测（虚拟、只读公开历史价格，不碰真实资金）。

模型：用 CLOB prices-history 的真实 mid 序列，假设固定半价差构造双边盘口
(bid = p*(1-half), ask = p*(1+half))，按固定频率做「建多@bid → 下期@ask 对冲」
的成对做市，扣单边手续费。

回测刻意保留两个真实世界的约束，让结论可信而非美化：
  1. 价差必须大于手续费才有油水——窄价差市场扣费后必亏；
  2. 持仓期的价格方向性风险——价格反向则该笔亏损。
"""
from __future__ import annotations

import time

import polymarket as _pm


def _resample(series, every_min):
    """按 every_min 分钟间隔重采样（历史点本就近似等间隔）。"""
    if not series:
        return []
    step = max(1, int(every_min))
    return series[::step]


def run_backtest(market_id, days=30, every_min=1440, size=100,
                 half_spread=None, fee_rate=0.01, series=None):
    """对指定市场跑历史回测。

    参数：
      days        回看天数（默认 30）
      every_min   交易频率（分钟，默认 1440=每日一笔）
      size        每笔份额
      half_spread 半价差（默认取当前实时价差一半；构造历史双边用）
      fee_rate    单边手续费率
      series      预取的历史 mid 序列；为 None 时内部拉取（sweep 复用单次拉取结果用）
    返回 dict：统计指标 + equity 曲线（供前端画折线）。
    """
    if series is None:
        series = _pm.fetch_price_history(market_id)
    if not series:
        return {"ok": False, "msg": "无历史价格数据"}
    if isinstance(series[0], dict) and "error" in series[0]:
        return {"ok": False, "msg": "获取历史价格失败：%s"
                % series[0].get("error", "未知")}

    now = time.time()
    cutoff = now - days * 86400
    sel = [x for x in series if x[0] >= cutoff]
    if len(sel) < 2:
        sel = series  # 历史不足 days 天则用全部
    pts = _resample(sel, every_min)
    if len(pts) < 2:
        return {"ok": False,
                "msg": "样本不足（需 >=2 个回测点），请增大天数或减小频率"}

    if half_spread is None:
        try:
            pq = _pm.fetch_poly_quotes(50)
            for q in pq:
                if q.get("id") == market_id:
                    b = q.get("yes_bid") or 0
                    a = q.get("yes_ask") or 0
                    if b > 0 and a > b:
                        half_spread = (a - b) / 2.0
                    break
        except Exception:
            pass
    if not half_spread or half_spread <= 0:
        half_spread = 0.005

    bankroll = 10000.0
    equity = bankroll
    curve = []
    trades = wins = losses = 0
    peak = equity
    max_dd = 0.0
    prev = None
    for (t, p) in pts:
        bid = p * (1 - half_spread)
        ask = p * (1 + half_spread)
        if prev is None:
            prev = (bid, ask, p)
            continue
        enter_bid = prev[0]
        exit_ask = ask
        fee = (enter_bid * size + exit_ask * size) * fee_rate
        profit = (exit_ask - enter_bid) * size - fee
        equity += profit
        trades += 1
        if profit >= 0:
            wins += 1
        else:
            losses += 1
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        curve.append({"t": t, "equity": round(equity, 2),
                      "pnl": round(profit, 2)})
        prev = (bid, ask, p)

    return {
        "ok": True,
        "market_id": market_id,
        "days": days,
        "every_min": every_min,
        "size": size,
        "half_spread": round(half_spread, 5),
        "fee_rate": fee_rate,
        "points": len(pts),
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / trades, 3) if trades else 0,
        "net_pnl": round(equity - bankroll, 2),
        "final_equity": round(equity, 2),
        "max_drawdown": round(max_dd, 2),
        "curve": curve,
        "note": "基于真实历史 mid + 假设固定半价差(默认取当前实时价差一半)构造双边；"
                "扣单边手续费。纯模拟，非真实成交。",
    }


def sweep_backtest(market_id, days=30, every_min=1440, size=100,
                   half_spreads=(0.002, 0.005, 0.01, 0.02, 0.03),
                   fee_rates=(0.0, 0.005, 0.01, 0.02, 0.03)):
    """对 (half_spread × fee_rate) 网格跑回测，返回净盈亏矩阵。

    只拉一次历史 mid（series），逐格调用 run_backtest(series=series)，
    避免重复联网。matrix[i][j] = 净盈亏 @ (fee_rates[i], half_spreads[j])。
    """
    series = _pm.fetch_price_history(market_id)
    if not series:
        return {"ok": False, "msg": "无历史价格数据"}
    if isinstance(series[0], dict) and "error" in series[0]:
        return {"ok": False,
                "msg": "获取历史价格失败：%s" % series[0].get("error", "未知")}
    matrix = []
    for fr in fee_rates:
        row = []
        for hs in half_spreads:
            r = run_backtest(market_id, days=days, every_min=every_min,
                             size=size, half_spread=hs, fee_rate=fr,
                             series=series)
            row.append(round(r.get("net_pnl", 0.0), 2)
                       if r.get("ok") else None)
        matrix.append(row)
    return {
        "ok": True,
        "market_id": market_id,
        "days": days,
        "every_min": every_min,
        "size": size,
        "half_spreads": [round(x, 5) for x in half_spreads],
        "fee_rates": [round(x, 5) for x in fee_rates],
        "matrix": matrix,  # matrix[i][j] = net_pnl @ (fee_rates[i], half_spreads[j])
        "note": "行=费率，列=半价差；单元格=净盈亏($)。只拉一次历史价后网格计算。",
    }


if __name__ == "__main__":
    import json as _j
    import sys as _sys
    _mid = _sys.argv[1] if len(_sys.argv) > 1 else "71321045675222837381289723557373135510915114294489920799786438437746741372444"
    print(_j.dumps(run_backtest(_mid, days=30, every_min=1440,
                                size=100, fee_rate=0.01),
                   ensure_ascii=False, indent=2, default=str))

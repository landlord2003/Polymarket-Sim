"""A股模拟买卖账本（零资金 · 本地 JSON · 与加密模拟盘完全独立）

设计：
  - 账本落地 a_share/sim_book.json（已被 .gitignore 忽略，不进仓库）。
  - 初始模拟资金 ¥100,000（用户 2026-08-20 拍板）。
  - 纯本地，不接任何券商/交易所，绝不自动下单；仅记录你手动"模拟"的下单意图与盈亏。
  - 浮动盈亏用 datasource.fetch_realtime 的实时价计算，使面板持仓盈亏随行情跳动。
  - 线程安全：所有读写加锁。

合规：本模块仅用于策略演练与教学，不代表真实交易，不构成投资建议。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime

from datasource import fetch_realtime, DataSourceError
from core.strategy import weighted_avg_cost, realized_pnl, unrealized_pnl

HERE = os.path.dirname(os.path.abspath(__file__))
BOOK_PATH = os.path.join(HERE, "sim_book.json")
LOCK = threading.Lock()
INIT_CASH = 100000.0
LOT = 100  # A股最小交易单位（手）


def _default_book() -> dict:
    return {"cash": INIT_CASH, "positions": [], "trades": []}


def get_book() -> dict:
    if not os.path.exists(BOOK_PATH):
        return _default_book()
    try:
        with open(BOOK_PATH, "r", encoding="utf-8") as f:
            b = json.load(f)
        b.setdefault("cash", INIT_CASH)
        b.setdefault("positions", [])
        b.setdefault("trades", [])
        return b
    except Exception:
        return _default_book()


def _save(b: dict) -> None:
    tmp = BOOK_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(b, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BOOK_PATH)


def _find_pos(b: dict, symbol: str):
    for p in b["positions"]:
        if p["symbol"] == symbol:
            return p
    return None


def buy(symbol: str, name: str, price: float, qty: int) -> dict:
    """模拟买入。qty 按股计，必须是 LOT 整数倍。返回 {ok, msg, book}。"""
    if qty <= 0 or qty % LOT != 0:
        return {"ok": False, "msg": f"买入数量须为 {LOT} 股的整数倍", "book": get_book()}
    if price <= 0:
        return {"ok": False, "msg": "价格非法", "book": get_book()}
    cost = round(price * qty, 2)
    with LOCK:
        b = get_book()
        if b["cash"] < cost:
            return {"ok": False, "msg": f"可用资金不足：需 ¥{cost:,.2f}，剩 ¥{b['cash']:,.2f}",
                    "book": b}
        b["cash"] = round(b["cash"] - cost, 2)
        pos = _find_pos(b, symbol)
        if pos:
            tot_qty = pos["qty"] + qty
            pos["cost_price"] = round(
                weighted_avg_cost(pos["cost_price"], pos["qty"], price, qty), 4)
            pos["qty"] = tot_qty
            pos["buy_time"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        else:
            b["positions"].append({
                "symbol": symbol, "name": name, "qty": qty,
                "cost_price": round(price, 4),
                "buy_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            })
        b["trades"].append({
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol, "name": name, "side": "buy",
            "price": round(price, 4), "qty": qty, "amount": cost,
            "realized_pnl": 0.0,
        })
        _save(b)
        return {"ok": True, "msg": f"已模拟买入 {name} {qty}股 @¥{price:.2f}", "book": b}


def sell(symbol: str, price: float, qty: int) -> dict:
    """模拟卖出。返回 {ok, msg, book, realized_pnl}。"""
    if qty <= 0 or qty % LOT != 0:
        return {"ok": False, "msg": f"卖出数量须为 {LOT} 股的整数倍",
                "book": get_book(), "realized_pnl": 0.0}
    if price <= 0:
        return {"ok": False, "msg": "价格非法", "book": get_book(),
                "realized_pnl": 0.0}
    with LOCK:
        b = get_book()
        pos = _find_pos(b, symbol)
        if not pos or pos["qty"] < qty:
            held = pos["qty"] if pos else 0
            return {"ok": False, "msg": f"可卖数量不足：持有 {held}股，欲卖 {qty}股",
                    "book": b, "realized_pnl": 0.0}
        proceeds = round(price * qty, 2)
        realized = round(realized_pnl(pos["cost_price"], price, qty), 2)
        b["cash"] = round(b["cash"] + proceeds, 2)
        pos["qty"] -= qty
        if pos["qty"] == 0:
            b["positions"].remove(pos)
        b["trades"].append({
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol, "name": pos["name"], "side": "sell",
            "price": round(price, 4), "qty": qty, "amount": proceeds,
            "realized_pnl": realized,
        })
        _save(b)
        return {"ok": True, "msg": f"已模拟卖出 {pos['name']} {qty}股 @¥{price:.2f}，"
                                   f"已实现盈亏 ¥{realized:,.2f}",
                "book": b, "realized_pnl": realized}


def mark_to_market() -> dict:
    """用实时价计算持仓浮动盈亏、总资产、总收益率。

    实时价取不到时（限流/休市）沿用最近成本/上一笔成交价估算，不阻断。
    """
    with LOCK:
        b = get_book()
    pos = b["positions"]
    if pos:
        syms = [p["symbol"] for p in pos]
        try:
            rt = fetch_realtime(syms)
        except Exception:  # noqa: BLE001
            rt = {}
        for p in pos:
            v = rt.get(p["symbol"])
            cur = v["price"] if v and v.get("price") else p["cost_price"]
            p["current"] = round(cur, 2)
            p["market_value"] = round(cur * p["qty"], 2)
            p["float_pnl"] = round(unrealized_pnl(p["cost_price"], cur, p["qty"]), 2)
            p["float_pct"] = (round((cur / p["cost_price"] - 1) * 100, 2)
                              if p["cost_price"] > 0 else 0.0)
    else:
        rt = {}
    mv = sum(p.get("market_value", 0.0) for p in pos)
    total_asset = round(b["cash"] + mv, 2)
    total_pnl = round(total_asset - INIT_CASH, 2)
    total_pct = round(total_pnl / INIT_CASH * 100, 2)
    realized = sum(t.get("realized_pnl", 0.0)
                   for t in b["trades"] if t["side"] == "sell")
    return {
        "cash": round(b["cash"], 2),
        "positions": pos,
        "market_value": round(mv, 2),
        "total_asset": total_asset,
        "total_pnl": total_pnl,
        "total_pct": total_pct,
        "realized_pnl": round(realized, 2),
        "trade_count": len(b["trades"]),
        "ts": datetime.now().strftime("%H:%M:%S"),
    }


def summary() -> dict:
    return mark_to_market()


if __name__ == "__main__":
    print("默认账本:", json.dumps(get_book(), ensure_ascii=False, indent=2))
    print("市值快照:", json.dumps(mark_to_market(), ensure_ascii=False, indent=2))

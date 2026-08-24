# -*- coding: utf-8 -*-
"""跨资产 Portfolio 层（改进计划 Phase 3）。

把分散在多个子模块的**虚拟持仓/权益**统一聚合成一个账户视图，
便于「整合多数据源/多项目」的总览需求。

设计原则：
  - 不改动各子模块既有逻辑，只读取它们已有的 view/summary 状态做聚合；
  - 各资产类别独立核算（币种、权益、盈亏、持仓数），再汇总总权益与占比；
  - 加密货币当前仅为「观察自选」（无虚拟账本），权益记 0 并显式标注，
    不混入记账口径，避免误导。
  - 全部为虚拟数据，不构成任何投资建议。
"""
from __future__ import annotations

import datetime
from typing import Optional


def _ashare() -> dict:
    """A股模拟盘（sim_engine 虚拟账本）。"""
    try:
        import sim_engine
        s = sim_engine.summary()
        pos = s.get("positions") or []
        equity = s.get("total_asset", 0.0)
        pnl = s.get("total_pnl", 0.0)
        return {
            "label": "A股模拟盘",
            "currency": "CNY",
            "equity": round(equity, 2),
            "cost": round(equity - pnl, 2),
            "pnl": round(pnl, 2),
            "realized": round(s.get("realized_pnl", 0.0), 2),
            "positions": len(pos),
            "trade_count": s.get("trade_count", 0),
        }
    except Exception as e:  # noqa: BLE001
        return {"label": "A股模拟盘", "currency": "CNY", "error": str(e)[:120]}


def _polymarket() -> dict:
    """Polymarket 预测市场虚拟账本（arb_book）。"""
    try:
        import arb_book
        v = arb_book.get_book().view()
        inv = v.get("inventory") or []
        return {
            "label": "Polymarket 预测市场",
            "currency": "USD",
            "equity": round(v.get("equity", 0.0), 2),
            "cash": round(v.get("cash", 0.0), 2),
            "realized": round(v.get("realized_pnl", 0.0), 2),
            "unrealized": round(v.get("unrealized_pnl", 0.0), 2),
            "open_positions": v.get("open_positions", 0),
            "inventory": len(inv),
        }
    except Exception as e:  # noqa: BLE001
        return {"label": "Polymarket 预测市场", "currency": "USD",
                "error": str(e)[:120]}


def _crypto() -> dict:
    """加密货币：当前仅观察自选（无虚拟账本），不记账。"""
    try:
        import datasource
        syms = datasource.load_crypto_watchlist()
        return {
            "label": "加密货币(观察)",
            "currency": "USDT",
            "equity": 0.0,
            "watch": len(syms),
            "note": "仅观察自选，未记账",
        }
    except Exception as e:  # noqa: BLE001
        return {"label": "加密货币(观察)", "currency": "USDT",
                "error": str(e)[:120]}


def cross_summary() -> dict:
    """聚合所有资产类别，返回统一的跨资产总览。"""
    classes = {
        "ashare": _ashare(),
        "polymarket": _polymarket(),
        "crypto": _crypto(),
    }
    # 仅汇总有记账权益的类（crypto 记 0 不影响，但显式标注）
    total = sum(c.get("equity", 0.0) for c in classes.values()
                if "equity" in c and "error" not in c)
    total = round(total, 2)
    weights = {}
    for k, c in classes.items():
        eq = c.get("equity", 0.0) if "equity" in c else 0.0
        weights[k] = round(eq / total * 100, 2) if total > 0 else 0.0
    return {
        "ok": True,
        "as_of": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "classes": classes,
        "total_equity": total,
        "weights": weights,
        "note": "跨资产总览为虚拟数据聚合，不构成投资建议；"
                "加密货币仅观察未记账。",
    }


if __name__ == "__main__":
    import json
    print(json.dumps(cross_summary(), ensure_ascii=False, indent=2))

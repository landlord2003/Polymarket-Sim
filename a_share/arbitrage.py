# -*- coding: utf-8 -*-
"""Polymarket 单源套利匹配与价差计算。

背景：跨平台 Kalshi↔Poly 因 Kalshi 要求美国身份/IP（CFTC 合规）不可得，
故本模块转向 **Polymarket 单源** 模拟交易。

scan_poly(quotes): 接收 polymarket.fetch_poly_quotes() 的统一报价列表，返回两类机会：
  1) marketmaking —— 单边做市价差：在高流动性市场买 bid / 卖 ask，
     库存中性假设下每轮锁定 spread（真实可演示）。
  2) event_arb     —— 同事件互斥完备集扫描（实验性，需人工确认完备性）：
     同 event 下多个互斥候选结果的概率和偏离 1 时报告，但 Polymarket 已消除
     大部分无风险免费钱，故标 need_confirm=True、confidence=0.5，仅供人工复核。

纯模拟：仅计算价差，不碰任何真实下单接口。

demo_pairs(): 保留「跨平台演示对」概念（明确 demo=True，因 Kalshi 不可得，
仅供端到端试跑模拟器流程，价格为例示例、非实时行情）。
"""
from __future__ import annotations

import re
from collections import defaultdict

_MIN_SPREAD = 0.003          # 做市：最小价差门槛（每单位）
_MIN_EVENT_PROFIT = 0.004    # 事件套利：最小无风险利润门槛
_WIN_RE = re.compile(r"\b(win|beats?|defeat|draw|tie|home|away)\b", re.I)


def _norm(text):
    t = (text or "").lower()
    t = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", t)
    return t


# ---------- 单边做市价差 ----------
def scan_poly_marketmaking(quotes, top_n=20, min_spread=_MIN_SPREAD,
                           min_liquidity=0):
    """对每个有真实双边流动性的市场，模拟买 bid / 卖 ask 的价差收益。
    返回按 (价差降序, 流动性降序) 排序的机会列表（含供模拟成交的字段）。"""
    out = []
    for q in quotes:
        if "error" in q:
            continue
        bid, ask = q.get("yes_bid"), q.get("yes_ask")
        if not bid or not ask or bid <= 0 or ask <= 0 or ask <= bid:
            continue
        liq = float(q.get("liquidity", 0) or 0)
        if liq < min_liquidity:
            continue
        mid = (bid + ask) / 2
        spread = round(ask - bid, 4)
        if spread < min_spread:
            continue
        spread_pct = round((ask - bid) / mid * 100, 2) if mid > 0 else 0.0
        unit = round((ask - bid) / 2, 4)   # 库存中性下每单位锁定毛利
        out.append({
            "type": "mm",
            "demo": False,
            "need_confirm": False,
            "confidence": 1.0,
            "question": q["question"],
            "event_id": q.get("event_id"),
            "liquidity": liq,
            "bid": bid, "ask": ask, "mid": round(mid, 4),
            "spread": spread, "spread_pct": spread_pct,
            "unit_profit": unit,
            "size_hint": 100,
            # 供 arb_book.market_make 使用：buy_ask=买价(bid), sell_bid=卖价(ask)
            "buy_venue": "poly", "buy_id": q["id"], "buy_ask": bid,
            "sell_venue": "poly", "sell_id": q["id"], "sell_bid": ask,
            "outcome_label": "主侧",
            "action": "在 %.4f 买 / %.4f 卖，每单位锁定价差 %.4f" % (bid, ask, unit),
            "edge": spread,
        })
    out.sort(key=lambda o: (o["spread"], o.get("liquidity", 0)), reverse=True)
    return out[:top_n]


# ---------- 同事件互斥完备集（实验性） ----------
def scan_poly_event_arb(quotes, top_n=10, min_profit=_MIN_EVENT_PROFIT):
    """同 event 下寻找互斥结果概率和偏离 1 的机会（实验性，需人工确认完备性）。

    仅当同 event 子市场能构成互斥候选（标题含 win/away/home/draw 等语义）
    且 1 - sum(ask) > 阈值时报告。由于通用识别难以确认「互斥且完备」
    （缺平局市场则非完备），一律标 need_confirm=True、confidence=0.5。
    """
    groups = defaultdict(list)
    for q in quotes:
        if "error" in q or not q.get("event_id"):
            continue
        groups[q["event_id"]].append(q)
    out = []
    for ev, items in groups.items():
        cands = [i for i in items if _WIN_RE.search(i.get("question") or "")]
        # 仅当存在平局候选(draw/tie) 或 候选数>=3 时，才可能构成互斥完备集，
        # 避免「两队赢缺平局」这类 sum<1 的正常现象被误报为无风险套利。
        has_draw = any(re.search(r"\b(draw|tie)\b", i.get("question") or "", re.I)
                       for i in cands)
        if len(cands) < 2 or (not has_draw and len(cands) < 3):
            continue
        asks = [i["yes_ask"] for i in cands if i["yes_ask"] > 0]
        if len(asks) < 2:
            continue
        s_ask = round(sum(asks), 4)
        if s_ask >= 1 - min_profit:   # 买齐成本 >= 1，无无风险利润
            continue
        profit = round(1 - s_ask, 4)
        out.append({
            "type": "event",
            "demo": False,
            "need_confirm": True,
            "confidence": 0.5,
            "question": "同事件 %s：%d 个互斥候选" % (ev, len(cands)),
            "event_id": ev,
            "submarkets": [{"q": i["question"], "bid": i["yes_bid"],
                            "ask": i["yes_ask"]} for i in cands],
            "sum_ask": s_ask,
            "profit_if_complete": profit,
            "size_hint": 100,
            "action": "买齐所有结果(ask)成本 %.4f，理论无风险利润 %.4f（需人工确认是否互斥完备）"
                      % (s_ask, profit),
            "edge": profit,
        })
    out.sort(key=lambda o: o["profit_if_complete"], reverse=True)
    return out[:top_n]


def scan_poly(quotes, top_mm=20, top_ev=10):
    """统一入口：返回 {marketmaking:[...], event_arb:[...]}。"""
    return {
        "marketmaking": scan_poly_marketmaking(quotes, top_mm),
        "event_arb": scan_poly_event_arb(quotes, top_ev),
    }


def demo_pairs():
    """演示对（明确 demo=True）。因 Kalshi 受美国身份/IP 限制不可得，跨平台
    实时匹配暂不可用；本函数仅供端到端试跑模拟器流程，价格为例示例、非实时行情。"""
    return [
        {
            "demo": True,
            "confidence": 1.0,
            "question": "美联储下次会议降息？(演示对·跨平台概念)",
            "edge": 0.030,
            "size_hint": 100,
            "buy_venue": "kalshi", "buy_id": "KFED-DEMO-1", "buy_ask": 0.660,
            "sell_venue": "poly", "sell_id": "poly-demo-fed", "sell_bid": 0.690,
            "outcome_label": "primary",
            "action": "买 kalshi YES @0.6600 / 卖 poly YES @0.6900（演示）",
        },
        {
            "demo": True,
            "confidence": 1.0,
            "question": "比特币本季收盘高于$10万？(演示对·跨平台概念)",
            "edge": 0.022,
            "size_hint": 100,
            "buy_venue": "poly", "buy_id": "poly-demo-btc", "buy_ask": 0.710,
            "sell_venue": "kalshi", "sell_id": "KXBTC-DEMO-1", "sell_bid": 0.732,
            "outcome_label": "primary",
            "action": "买 poly YES @0.7100 / 卖 kalshi YES @0.7320（演示）",
        },
        {
            "demo": True,
            "confidence": 1.0,
            "question": "下届 NFL 超级碗某队夺冠？(演示对·跨平台概念)",
            "edge": 0.018,
            "size_hint": 100,
            "buy_venue": "kalshi", "buy_id": "KNFL-DEMO-1", "buy_ask": 0.240,
            "sell_venue": "poly", "sell_id": "poly-demo-nfl", "sell_bid": 0.258,
            "outcome_label": "primary",
            "action": "买 kalshi YES @0.2400 / 卖 poly YES @0.2580（演示）",
        },
    ]


if __name__ == "__main__":
    import json
    print("DEMO PAIRS:")
    print(json.dumps(demo_pairs(), ensure_ascii=False, indent=2))

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

_DEFAULT_MAX_SKEW = 300       # 与 arb_book.DEFAULT_MAX_SKEW 保持一致
_MIN_SPREAD = 0.003          # 做市：最小价差门槛（每单位）
_MIN_EVENT_PROFIT = 0.004    # 事件套利：最小无风险利润门槛
_WIN_RE = re.compile(r"\b(win|beats?|defeat|draw|tie|home|away)\b", re.I)


def _norm(text):
    t = (text or "").lower()
    t = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", t)
    return t


# ---------- 单边做市价差 ----------
def scan_poly_marketmaking(quotes, top_n=20, min_spread=_MIN_SPREAD,
                           min_liquidity=0, inventory=None, skip_skewed=False,
                           max_skew=_DEFAULT_MAX_SKEW, skew_threshold=0.8):
    """对每个有真实双边流动性的市场，模拟买 bid / 卖 ask 的价差收益。
    返回按 (价差降序, 流动性降序) 排序的机会列表（含供模拟成交的字段）。

    inventory / skip_skewed / max_skew / skew_threshold：智能选股参数。
    当 skip_skewed=True 时，若某市场当前净库存绝对值已 >= skew_threshold*max_skew，
    则跳过该市场（避免轮动越做越偏、触顶后被拒绝浪费一次轮动名额）。
    cur_inv 字段暴露该市场当前净库存，供前端展示「本仓」。
    """
    out = []
    inv = inventory or {}
    for q in quotes:
        if "error" in q:
            continue
        bid, ask = q.get("yes_bid"), q.get("yes_ask")
        if not bid or not ask or bid <= 0 or ask <= 0 or ask <= bid:
            continue
        liq = float(q.get("liquidity", 0) or 0)
        if liq < min_liquidity:
            continue
        # 智能选股：轮动时跳过已接近偏斜上限的市场
        mkt_id = q.get("id")
        cur_inv = int(inv.get(mkt_id, 0)) if mkt_id else 0
        if skip_skewed and abs(cur_inv) >= skew_threshold * max_skew:
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
            "end_date": q.get("end_date"),   # 透传到期时间，供时间衰减门控
            "cur_inv": cur_inv,
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


def scan_poly_pure_arb(quotes, top_n=20, fee_rate=0.01, buffer=0.002,
                       min_liquidity=0, min_outcomes=2):
    """同事件多结果完备集 Dutch Book（Polymarket 上真实存在的无风险套利）。

    关键认知：单二元市场内 YES/NO 严格互补（yes_ask+no_ask = 1+spread >= 1 恒成立），
    故**单市场不存在** yes_ask+no_ask<1 的瞬时套利；无风险套利只存在于**同事件多结果
    （>2 且互斥完备）**：买齐所有结果 YES 的 ask，若 sum(ask) < 1 - 2*fee - buffer，
    则到期必有一个结果兑付 $1/份额，净锁定 1 - sum(ask) - 费用。
    need_confirm 标记完备性待确认（2 结果可能缺平局/第三结果）。
    """
    from collections import defaultdict
    groups = defaultdict(list)
    for q in quotes:
        if "error" in q or not q.get("event_id"):
            continue
        liq = float(q.get("liquidity", 0) or 0)
        if liq < min_liquidity:
            continue
        ya = q.get("yes_ask")
        if not isinstance(ya, (int, float)) or ya <= 0 or ya >= 1:
            continue
        groups[q["event_id"]].append(q)
    out = []
    for ev, items in groups.items():
        if len(items) < min_outcomes:
            continue
        asks = [i["yes_ask"] for i in items]
        s = sum(asks)
        if s >= 1 - 2 * fee_rate - buffer:
            continue
        edge = round(1 - s - 2 * fee_rate, 4)
        if edge <= 0:
            continue
        conf = min(1.0, 0.5 + 0.15 * (len(items) - 2))
        out.append({
            "type": "pure", "demo": False,
            "need_confirm": True,   # 完备性无法自动验证，始终需人工确认后再执行
            "confidence": round(conf, 2),
            "question": "同事件 %s：%d 结果买齐" % (ev, len(items)),
            "event_id": ev,
            "liquidity": max(float(i.get("liquidity", 0) or 0) for i in items),
            "submarkets": [{"q": i["question"], "ask": i["yes_ask"], "id": i["id"]}
                           for i in items],
            "sum_ask": round(s, 4), "edge": edge, "size_hint": 100,
            "buy_venue": "poly", "buy_id": ev,
            "action": "买齐 %d 个结果(YES ask)成本 %.4f，到期兑付$1，净锁定 $%.4f/份（完备性%s）"
                      % (len(items), s, edge, "待确认" if len(items) < 3 else "较高"),
        })
    out.sort(key=lambda o: o["edge"], reverse=True)
    return out[:top_n]


def scan_poly(quotes, top_mm=20, top_ev=10, top_pure=20, inventory=None,
              max_skew=_DEFAULT_MAX_SKEW, skip_skewed=False,
              fee_rate=0.01, pure_buffer=0.002, min_liquidity=0):
    """统一入口：返回 {marketmaking, event_arb, pure_arb}。"""
    return {
        "marketmaking": scan_poly_marketmaking(
            quotes, top_mm, inventory=inventory, max_skew=max_skew,
            skip_skewed=skip_skewed, min_liquidity=min_liquidity),
        "event_arb": scan_poly_event_arb(quotes, top_ev),
        "pure_arb": scan_poly_pure_arb(
            quotes, top_n=top_pure, fee_rate=fee_rate,
            buffer=pure_buffer, min_liquidity=min_liquidity),
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

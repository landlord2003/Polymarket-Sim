# -*- coding: utf-8 -*-
"""跨平台 Kalshi↔Poly 套利匹配与价差计算。

scan(): 读取两边统一报价，按归一化题目文本做相似度匹配（difflib），
        对同事件计算 4 种跨平台腿组合的每份额外收益(edge)，挑最大正收益。
        纯模拟：只算价差，不碰任何下单接口。

demo_pairs(): 当本环境无实时跨平台同事件匹配时，返回若干「演示对」
        （明确标注 demo=True，价格为合理示例），供端到端试跑模拟器。
"""
from __future__ import annotations

import difflib
import re

_MATCH_THRESHOLD = 0.80
_MIN_EDGE = 0.004  # 每份额外收益下限（扣费前），避免噪声


def _norm(text):
    t = (text or "").lower()
    t = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", t)  # 去标点空格，保留中英文数字
    return t


def _edges(k, p):
    """返回 4 种跨平台腿组合的 (edge, buy_venue, buy_id, buy_ask,
    sell_venue, sell_id, sell_bid, action)。"""
    out = []
    # 1) 买 Kalshi primary / 卖 Poly primary
    out.append((p["yes_bid"] - k["yes_ask"],
                k["platform"], k["id"], k["yes_ask"],
                p["platform"], p["id"], p["yes_bid"],
                "买 %s YES @%.4f / 卖 %s YES @%.4f"
                % (k["platform"], k["yes_ask"], p["platform"], p["yes_bid"])))
    # 2) 买 Poly primary / 卖 Kalshi primary
    out.append((k["yes_bid"] - p["yes_ask"],
                p["platform"], p["id"], p["yes_ask"],
                k["platform"], k["id"], k["yes_bid"],
                "买 %s YES @%.4f / 卖 %s YES @%.4f"
                % (p["platform"], p["yes_ask"], k["platform"], k["yes_bid"])))
    # 3) 买 Kalshi NO / 卖 Poly NO
    out.append((p["no_bid"] - k["no_ask"],
                k["platform"], k["id"], k["no_ask"],
                p["platform"], p["id"], p["no_bid"],
                "买 %s NO @%.4f / 卖 %s NO @%.4f"
                % (k["platform"], k["no_ask"], p["platform"], p["no_bid"])))
    # 4) 买 Poly NO / 卖 Kalshi NO
    out.append((k["no_bid"] - p["no_ask"],
                p["platform"], p["id"], p["no_ask"],
                k["platform"], k["id"], k["no_bid"],
                "买 %s NO @%.4f / 卖 %s NO @%.4f"
                % (p["platform"], p["no_ask"], k["platform"], k["no_bid"])))
    return out


def scan(kalshi_quotes, poly_quotes, min_edge=_MIN_EDGE):
    """返回实时跨平台套利机会列表（仅基于真实行情）。无匹配则空列表。"""
    opps = []
    if not kalshi_quotes or not poly_quotes:
        return opps
    for k in kalshi_quotes:
        if "error" in k:
            continue
        best_p, best_r = None, 0.0
        for p in poly_quotes:
            if "error" in p:
                continue
            r = difflib.SequenceMatcher(None, _norm(k["question"]),
                                        _norm(p["question"])).ratio()
            if r > best_r:
                best_r, best_p = r, p
        if best_p is None or best_r < _MATCH_THRESHOLD:
            continue
        best = None
        for e in _edges(k, best_p):
            if e[0] > min_edge and (best is None or e[0] > best[0]):
                best = e
        if best is None:
            continue
        (edge, bv, bid, bask, sv, sid, sbid, action) = best
        opps.append({
            "demo": False,
            "confidence": round(best_r, 3),
            "question": k["question"],
            "edge": round(edge, 4),
            "size_hint": 100,
            "buy_venue": bv, "buy_id": bid, "buy_ask": round(bask, 4),
            "sell_venue": sv, "sell_id": sid, "sell_bid": round(sbid, 4),
            "outcome_label": "primary",
            "action": action,
        })
    opps.sort(key=lambda o: o["edge"], reverse=True)
    return opps


def demo_pairs():
    """演示对（明确 demo=True）。当本环境无实时跨平台同事件匹配时使用，
    仅供试跑模拟器流程，价格为例示例、非实时行情。"""
    return [
        {
            "demo": True,
            "confidence": 1.0,
            "question": "美联储下次会议降息？(演示对)",
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
            "question": "比特币本季收盘高于$10万？(演示对)",
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
            "question": "下届 NFL 超级碗某队夺冠？(演示对)",
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

# -*- coding: utf-8 -*-
"""合规红线过滤（中国部署，必须过滤政治/地缘/军事等敏感市场）。

单一事实来源：sim_server 的 select_mm / 看板 / 离线回测(backtest_quotes) 共用本模块，
避免各处过滤词表漂移。与 sim_server 原 BLOCK_EXTRA/BLOCK_SPORTS 行为保持一致。

体育对抗赛（A vs B / O/U 大小球）中的国家名不算政治敏感，故 sports 类别只套用
「真正政治/军事」词表，避免误杀（如 New Zealand vs. Syria）。
"""
from __future__ import annotations
import re
import polymarket as P

# 非体育语境下的完整屏蔽词（含国家主体/地缘/组织/军事）
BLOCK_EXTRA = ["iran", "invade", "invasion", "russia", "ukraine", "israel",
               "taiwan", "geopolit", "nuclear", "sanction", "election",
               "president", "putin", "trump", "biden", "xi ", "kremlin",
               "nato", "missile", "military", "war", "army", "gaza",
               "palestine", "china", "ccp", "communist",
               # 中东航运咽喉（涉伊朗/胡塞冲突，地缘敏感）
               "hormuz", "mandeb", "bab el-mandeb", "red sea", "yemen",
               "houthis", "houthi", "suez", "gulf", "opec",
               # 其他地缘/国家主体（非体育语境下屏蔽）
               "syria", "north korea", "korea", "lebanon", "hezbollah",
               "afghanistan", "iraq", "venezuela", "cuba", "belarus"]

# 体育赛事专用：只屏蔽真正的政治/军事/选举词，放行国家名
BLOCK_SPORTS = ["invade", "invasion", "geopolit", "nuclear", "sanction",
                "election", "president", "putin", "trump", "biden", "kremlin",
                "nato", "missile", "military", "army", "gaza", "palestine",
                "ccp", "communist", "war ", "world war", " houthis", "houthi"]


def classify(q):
    """按 polymarket 的类别关键词把题目分到 crypto/economy/sports/.../other。"""
    ql = (q or "").lower()
    for tag, re_ in P._CAT_RE.items():
        if re_.search(ql):
            return tag
    return "other"


def is_blocked(q, tag=None):
    """返回 True 表示该市场触碰合规红线，应被过滤。"""
    if P._is_blocked(q, None):
        return True
    ql = (q or "").lower()
    if tag is None:
        tag = classify(q)
    # 对抗赛句式（A vs B / O/U 大小球）视为体育赛事，其中的国家名不敏感
    is_match = (" vs " in ql) or (" vs. " in ql) or (" o/u " in ql) or (" over/under" in ql)
    words = BLOCK_SPORTS if (tag == "sports" or is_match) else BLOCK_EXTRA
    return any(k in ql for k in words)


def filter_markets(markets):
    """从行情列表里剔除敏感市场，返回合规子集。"""
    out = []
    for m in markets:
        if not isinstance(m, dict):
            continue
        if is_blocked(m.get("question", ""), m.get("tag")):
            continue
        out.append(m)
    return out

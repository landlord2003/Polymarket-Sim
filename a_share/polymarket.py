"""Polymarket 只读行情（事件概率）数据层。

定位：把 Poly Market 当作「事件概率」另类数据来源，只读拉取 Gamma 公开 API
（gamma-api.polymarket.com），无需 API key、无需钱包、不触碰资金。

与 datasource 的约定保持一致：全程用标准库 urllib，不引入 requests。

合规红线：Poly Market 上存在涉及政治人物/地缘冲突的敏感市场。本项目在中国
部署，必须过滤掉 politics / geopolitics / war 等类别以及含敏感关键词的题目，
只保留 crypto / economy / finance / tech / science / sports / entertainment 等
中性类别。fetch_polymarket_odds 在服务端强制过滤，前端无法绕过。
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

_GAMMA = "https://gamma-api.polymarket.com/markets"

# 允许拉取的类别（白名单）。任何不在其中的 tag 都不会被请求。
ALLOWED_TAGS = ["crypto", "economy", "finance", "business",
                "tech", "science", "sports", "entertainment", "culture"]

# 即使漏过白名单也要拦掉的标签（防御层）。
_BLOCK_TAGS = {"politics", "geopolitics", "world", "war", "elections",
               "russia", "ukraine", "israel", "palestine", "china",
               "military", "terrorism", "proxy-war", "ir", "north-korea"}

# 题目级关键词拦截（中英文），大小写不敏感。
_BLOCK_KW = [
    "xi jinping", "putin", "trump", "biden", "election", "president",
    "war", "ukraine", "russia", "israel", "gaza", "palestine", "taiwan",
    "china", "ccp", "communist", "nuclear", "sanction", "geopolit",
    "military", "army", "missile", "invasion", "genocide",
    "政治", "大选", "战争", "台湾", "普京", "特朗普", "习", "中国",
]

_CACHE_TTL = 120  # 秒；行情变化慢，缓存降低请求频率、避免被限流
_cache = {}  # tag -> (ts, data)


def _is_blocked(question: str, tags) -> bool:
    q = (question or "").lower()
    for kw in _BLOCK_KW:
        if kw in q:
            return True
    for t in (tags or []):
        if str(t).lower() in _BLOCK_TAGS:
            return True
    return False


def _http_get(url: str, timeout: int = 12) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; QuantTrading/1.0)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def _parse_outcomes(market: dict):
    """返回 [{label, price}] 列表。price 为 0~1 的隐含概率。"""
    raw_prices = market.get("outcomePrices")
    raw_labels = market.get("outcomes")
    try:
        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else (raw_prices or [])
    except Exception:  # noqa: BLE001
        prices = []
    try:
        labels = json.loads(raw_labels) if isinstance(raw_labels, str) else (raw_labels or [])
    except Exception:  # noqa: BLE001
        labels = []
    if not labels:
        labels = ["选项%s" % (i + 1) for i in range(len(prices))]
    out = []
    for i, p in enumerate(prices):
        try:
            price = float(p)
        except (TypeError, ValueError):
            price = None
        out.append({"label": labels[i] if i < len(labels) else "选项%s" % (i + 1),
                    "price": price})
    return out


def fetch_polymarket_odds(tag: str = "crypto", limit: int = 30,
                          ignore_cache: bool = False) -> dict:
    """拉取某类别下的活跃市场隐含概率。

    返回 {ok, tag, ts, markets:[{question, slug, endDate, volume24hr,
    liquidity, outcomes:[{label, price}]}], msg}。
    服务端强制过滤政治/敏感类别与题目。
    """
    tag = tag if tag in ALLOWED_TAGS else "crypto"
    now = time.time()
    if not ignore_cache and tag in _cache and (now - _cache[tag][0]) < _CACHE_TTL:
        cached = dict(_cache[tag][1])
        cached["cached"] = True
        return cached

    params = urllib.parse.urlencode({
        "limit": max(1, min(limit, 50)),
        "active": "true",
        "order": "volume24hr",
        "ascending": "false",
        "tag": tag,
    })
    url = "%s?%s" % (_GAMMA, params)
    try:
        raw = _http_get(url)
        rows = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "tag": tag, "msg": "获取失败：%s" % str(e), "markets": []}

    markets = []
    for m in rows:
        try:
            question = m.get("question") or ""
            tags = m.get("tags") or []
            if _is_blocked(question, tags):
                continue
            outcomes = _parse_outcomes(m)
            if not outcomes:
                continue
            markets.append({
                "question": question,
                "slug": m.get("slug"),
                "endDate": m.get("endDate"),
                "volume24hr": _to_num(m.get("volume24hr")),
                "volume": _to_num(m.get("volume")),
                "liquidity": _to_num(m.get("liquidity")),
                "outcomes": outcomes,
            })
        except Exception:  # noqa: BLE001
            continue
    if not markets:
        return {"ok": False, "tag": tag,
                "msg": "该类别暂无可用市场（或已被合规过滤）", "markets": []}
    res = {"ok": True, "tag": tag, "markets": markets}
    _cache[tag] = (now, res)
    return res


def _to_num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

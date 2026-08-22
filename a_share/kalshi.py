# -*- coding: utf-8 -*-
"""Kalshi public market-data client (read-only, no auth).

Reads https://external-api.kalshi.com/trade-api/v2/markets and normalizes each
outcome's yes/no bid/ask into a unified Quote dict for the arbitrage engine.
Sensitive (political/geopolitical) markets are filtered out per project policy.
"""
from __future__ import annotations

import json
import time
import urllib.request
import ssl

BASE = "https://external-api.kalshi.com/trade-api/v2"
CTX = ssl.create_default_context()
HEADERS = {"User-Agent": "quant-trading-probe/1.0"}

# Content-compliance blocklist: any market whose text hits these is dropped.
_SENSITIVE = (
    "president", "election", "xi jinping", "xi ", "trump", "biden", "putin",
    "senate", "congress", "parliament", "prime minister", "referendum",
    "geopolit", "invasion", "sanction", "nato", "military", "war ", "vote",
    "communist", "kremlin", "white house", "campaign", "ballot", "impeach",
)

_POOL_TTL = 60.0
_cache = {"ts": 0.0, "data": None}


def _is_sensitive(text):
    t = (text or "").lower()
    return any(w in t for w in _SENSITIVE)


def _http_json(url, params=None):
    if params:
        url += ("&" if "?" in url else "?") + "&".join(
            "%s=%s" % (k, v) for k, v in params.items())
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
        return json.loads(r.read().decode())


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def fetch_quotes(limit=200, force=False):
    """Return list of unified Quote dicts from Kalshi open markets.

    Quote = {platform,id,question,yes_bid,yes_ask,no_bid,no_ask,ts}
    """
    now = time.time()
    if not force and _cache["data"] is not None and now - _cache["ts"] < _POOL_TTL:
        return _cache["data"]
    out = []
    try:
        data = _http_json(BASE + "/markets",
                           {"status": "open", "limit": str(limit)})
        for m in data.get("markets", []):
            title = m.get("title") or ""
            if _is_sensitive(title):
                continue
            yb, ya = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
            nb, na = _f(m.get("no_bid_dollars")), _f(m.get("no_ask_dollars"))
            liq = _f(m.get("liquidity_dollars"))
            # 只保留有真实双边流动性且非分片零流动性市场
            if yb <= 0 or ya <= 0 or na <= 0 or ya >= 1 or liq < 500:
                continue
            out.append({
                "platform": "kalshi",
                "id": m.get("ticker"),
                "question": title,
                "yes_bid": yb, "yes_ask": ya,
                "no_bid": nb, "no_ask": na,
                "ts": now,
            })
    except Exception as e:
        # 网络/解析失败时返回上次缓存或空，不崩溃
        if _cache["data"] is not None:
            return _cache["data"]
        return [{"error": str(e)}]
    _cache["ts"] = now
    _cache["data"] = out
    return out


if __name__ == "__main__":
    import pprint
    q = fetch_quotes(30)
    pprint.pprint(q[:5])
    print("total liquid non-sensitive:", len(q))

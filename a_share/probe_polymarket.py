# -*- coding: utf-8 -*-
"""P3-5 只读探针：拉取 Polymarket 实时概率 → 低空/宏观情报供给（北京零合规风险）。

仅用 Gamma 公开 API 只读行情，不接 L2、不下单、不碰私钥。
输出 JSON 喂入低空情报 / 宏观监测工作流（a_share/data/probe_feed.json）。

用法:
  python probe_polymarket.py            # 抓一次，写 feed + 打印摘要
  python probe_polymarket.py --loop 300 # 每 300s 刷新一次
  python probe_polymarket.py --limit 200 --top 30
"""
import os
import sys
import json
import time
import argparse
import urllib.request
import urllib.parse
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, "data", "probe_feed.json")
GAMMA = "https://gamma-api.polymarket.com/markets"

# 情报聚焦类别（其余忽略）。低空/宏观监测偏经济、利率、加密、产业、科技。
ALLOW_CATEGORIES = {"economy", "economics", "finance", "business", "markets",
                    "crypto", "technology", "science", "weather", "energy"}
# 命中任一关键词即纳入（放宽到宏观/产业/地缘经济相关事件，便于低空情报交叉参考）
MACRO_KEYWORDS = ["fed ", "interest rate", "rate", "inflation", "gdp", "recession",
                  "unemployment", "bitcoin", "ethereum", "crypto", "oil", "energy",
                  "aviation", "airline", "aircraft", "defense", "budget", "tariff",
                  "supply chain", "chip", "semiconductor", "election", "gdp"]

# 敏感词（与合规红线词表同源意图，仅作情报去噪，不用于交易过滤）
SENSITIVE = {"iran", "invade", "invasion", "russia", "ukraine", "israel", "war",
             "military", "nuclear", "missile", "putin", "gaza", "palestine",
             "hormuz", "houthis", "houthi", "sanction"}

UA = {"User-Agent": "quant-trading-probe/1.0"}


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_markets(limit=100, closed=False):
    """Gamma 公开市场列表（无需鉴权）。带简单重试与限频友好。"""
    q = {"limit": limit, "closed": "true" if closed else "false",
         "active": "true", "_sort": "volumeNum:-1"}
    url = GAMMA + "?" + urllib.parse.urlencode(q)
    for attempt in range(3):
        try:
            return _get(url)
        except Exception as e:
            wait = 2 ** attempt
            print("[probe] fetch 失败(%s)，%ss 后重试" % (e, wait), file=sys.stderr)
            time.sleep(wait)
    return []


def _price_of(m, side_idx):
    """从 outcomePrices('0.65,0.35') 取指定 side 价格。"""
    raw = m.get("outcomePrices") or m.get("prices")
    if not raw:
        return None
    parts = [p.strip() for p in str(raw).split(",")]
    if 0 <= side_idx < len(parts):
        try:
            return round(float(parts[side_idx]), 4)
        except ValueError:
            return None
    return None


def is_relevant(m):
    cat = (m.get("category") or m.get("subCategory") or "").lower()
    if cat in ALLOW_CATEGORIES:
        return True
    q = (m.get("question") or "").lower()
    return any(k in q for k in MACRO_KEYWORDS)


def is_sensitive(m):
    q = (m.get("question") or "").lower()
    return any(s in q for s in SENSITIVE)


def parse(m):
    q = m.get("question", "")
    yes = _price_of(m, 0)
    no = _price_of(m, 1)
    try:
        vol = float(m.get("volumeNum") or m.get("volume") or 0)
    except (TypeError, ValueError):
        vol = 0.0
    try:
        liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
    except (TypeError, ValueError):
        liq = 0.0
    return {
        "slug": m.get("slug") or m.get("id"),
        "question": q,
        "yes_prob": yes,
        "no_prob": no,
        "volume": vol,
        "liquidity": liq,
        "end_date": m.get("endDate"),
        "url": "https://polymarket.com/event/%s" % (m.get("slug") or m.get("id") or ""),
    }


def run_once(limit, top):
    markets = fetch_markets(limit=limit)
    if not markets:
        print("[probe] 未取到行情", file=sys.stderr)
        return None
    items = []
    for m in markets:
        if not isinstance(m, dict) or "error" in m:
            continue
        if not is_relevant(m):
            continue
        if is_sensitive(m):
            continue
        items.append(parse(m))
    items.sort(key=lambda x: x["volume"] or 0, reverse=True)
    feed = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "gamma-api.polymarket.com (read-only)",
        "count": len(items),
        "markets": items[:top],
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)
    print("[probe] 抓到 %d 个相关市场，写 %s" % (len(items), OUT_PATH))
    for it in items[:min(top, 15)]:
        print("  %.0f%%  %s  (vol=%.0f)" % ((it["yes_prob"] or 0) * 100,
                                             it["question"][:70], it["volume"] or 0))
    return feed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=150)
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--loop", type=int, default=0, help="0=单次；>0=每 N 秒循环")
    args = ap.parse_args()
    if args.loop > 0:
        print("[probe] 循环模式，每 %ds 刷新（Ctrl+C 退出）" % args.loop)
        try:
            while True:
                run_once(args.limit, args.top)
                time.sleep(args.loop)
        except KeyboardInterrupt:
            print("[probe] 停止")
    else:
        run_once(args.limit, args.top)


if __name__ == "__main__":
    main()

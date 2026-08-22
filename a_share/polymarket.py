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
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

# 强制 IPv4 解析：gamma-api.polymarket.com 同时有 A/AAAA 记录，urllib 默认优先
# 解析 AAAA(IPv6)；若本机 IPv6 出口不通，连接会挂起直到超时（curl 因自动回落
# IPv4 正常）。全局改为仅解析 IPv4，避免「页面获取失败 / timed out」。
_orig_getaddrinfo = socket.getaddrinfo
def _getaddrinfo_ipv4(host, port, family=socket.AF_UNSPEC, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _getaddrinfo_ipv4

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

# 行情池缓存：Gamma 的 tag 服务端过滤实测无效（crypto/tech/science 返回同一批
# 全站热门市场，且 markets 的 tags 字段为 None），故改为「拉取大盘池 + 本地按
# 关键词分类」的方式，保证切换类别能拿到真正不同的内容。
_POOL_TTL = 120  # 秒；行情变化慢，缓存降低请求频率、避免被限流
_pool_cache = None  # (ts, rows)

# 各类别关键词（小写，匹配 question + outcomes 文本；\b 词边界避免误命中，
# 如 "ai" 不会命中 "again"、"eth" 不会命中 "ethereum"）。
CATEGORY_KEYWORDS = {
    "crypto": ["bitcoin","btc","ethereum","eth","solana","sol","crypto","cryptocurrency",
               "defi","altcoin","dogecoin","doge","litecoin","ltc","ripple","xrp",
               "stablecoin","usdc","tether","usdt","blockchain","web3","nft","coinbase",
               "binance","cardano","ada","avalanche","polkadot","chainlink","uniswap","aave"],
    "economy": ["federal reserve","fed","interest rate","rate cut","rate hike","inflation",
                "gdp","recession","economy","economic","unemployment","cpi","pce","treasury",
                "dollar","yuan","renminbi","tariff","trade war","gross domestic","macro",
                "fomc","quantitative","bond yield","debt ceiling"],
    "finance": ["federal reserve","interest rate","stock","stocks","equity","equities","etf",
                "bond","bonds","forex","currency","finance","financial","s&p","sp500","nasdaq",
                "dow","ipo","bull","bear","market cap","bankruptcy","credit","mortgage"],
    "business": ["company","ceo","earnings","revenue","merger","acquisition","apple","google",
                 "amazon","microsoft","meta","tesla","nvidia","alphabet","layoff","startup",
                 "business","profit","sales","subscriber"],
    "tech": ["ai","artificial intelligence","openai","chatgpt","gpt","google","apple",
             "microsoft","meta","tesla","nvidia","semiconductor","chip","chips","software",
             "tech","cyber","data center","cloud","robotics","quantum","smartphone","android","ios"],
    "science": ["science","research","scientist","space","nasa","spacex","spacex","mars","moon",
                "climate","physics","medicine","medical","vaccine","health","disease","cancer",
                "telescope","astronomy","biology","genetic","climate change","black hole",
                "particle","quantum","virus","covid","brain","earthquake","volcano","ocean",
                "species","extinct","rocket","satellite","shuttle","probe","telescope"],
    "sports": ["nba","nfl","football","soccer","fifa","world cup","champions league",
               "premier league","tennis","cricket","formula 1","f1","boxing","ufc","wwe","dota",
               "league of legends","esports","super bowl","olympics","golf","hockey","mlb"],
    "entertainment": ["movie","film","oscar","grammy","music","album","netflix","tv show",
                      "box office","celebrity","emmy","billboard","disney","marvel","star wars",
                      "taylor swift","concert","streaming","video game","game of thrones","hbo",
                      "song","tour","award","premiere","sequel","franchise","anime","k-pop","pop",
                      "rapper","spotify","youtube","box-office","cinema"],
}
_CAT_RE = {k: re.compile(r"(?:\b" + "|".join(re.escape(w) for w in v) + r"\b)", re.I)
           for k, v in CATEGORY_KEYWORDS.items()}
_POOL_PAGES = 10  # 每页100(Gamma上限)，共约1000条大盘池；多页确保科技/科学等小类也能露出


def _is_blocked(question: str, tags) -> bool:
    q = (question or "").lower()
    for kw in _BLOCK_KW:
        if kw in q:
            return True
    for t in (tags or []):
        if str(t).lower() in _BLOCK_TAGS:
            return True
    return False


def _http_get(url: str, timeout: int = 15) -> str:
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


def _fetch_pool() -> list:
    """翻页拉取全站活跃市场大盘池（按 24h 成交量排序，多页拼接去重），
    本地再做类别过滤。返回原始 market 列表；命中缓存则复用，失败且有旧缓存
    则降级用旧数据。"""
    global _pool_cache
    now = time.time()
    if _pool_cache and (now - _pool_cache[0]) < _POOL_TTL:
        return _pool_cache[1]
    rows = []
    seen = set()
    for off in range(0, _POOL_PAGES * 100, 100):
        params = urllib.parse.urlencode({
            "limit": 100,
            "active": "true",
            "order": "volume24hr",
            "ascending": "false",
            "offset": off,
        })
        url = "%s?%s" % (_GAMMA, params)
        try:
            page = json.loads(_http_get(url))
        except Exception:  # noqa: BLE001
            break
        if not page:
            break
        for m in page:
            k = m.get("question")
            if k and k not in seen:
                seen.add(k)
                rows.append(m)
        if len(page) < 100:
            break
        time.sleep(0.2)  # 降低突发请求频率，避免被 Gamma 限流
    if not rows and _pool_cache:
        return _pool_cache[1]
    _pool_cache = (now, rows)
    return rows


def fetch_polymarket_odds(tag: str = "crypto", limit: int = 30,
                          ignore_cache: bool = False) -> dict:
    """拉取某类别下的活跃市场隐含概率（本地按关键词分类，规避 Gamma tag 过滤失效）。

    返回 {ok, tag, ts, markets:[{question, slug, endDate, volume24hr,
    liquidity, outcomes:[{label, price}]}], msg}。
    服务端强制过滤政治/敏感类别与题目。
    """
    tag = tag if tag in ALLOWED_TAGS else "crypto"
    rows = _fetch_pool()
    if not rows:
        return {"ok": False, "tag": tag, "msg": "获取失败：无法拉取行情池", "markets": []}
    cat_re = _CAT_RE.get(tag)
    markets = []
    for m in rows:
        try:
            question = m.get("question") or ""
            tags = m.get("tags") or []
            if _is_blocked(question, tags):
                continue
            blob = (question + " " + str(m.get("outcomes") or "")).lower()
            if cat_re and not cat_re.search(blob):
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
    return {"ok": True, "tag": tag, "markets": markets[:limit]}


def _to_num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

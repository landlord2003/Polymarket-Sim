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
import os
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


class GammaRateLimited(Exception):
    """Gamma 命中 429 冷却中；调用方应降级用旧缓存而非继续打 Gamma。"""
    pass


_GAP_COOLDOWN_UNTIL = 0.0    # 429 触发的全局冷却截止时间（秒）
_GAP_BACKOFF_BASE = 2.0      # 指数退避基数（秒）
_GAP_MAX_RETRY = 3           # 单次请求最大重试次数（5xx / 网络抖动）
_GAP_COOLDOWN_SEC = 30.0     # 命中 429 后的冷却时长（秒）


def gamma_cooldown_remaining() -> float:
    """距离 Gamma 限流冷却结束还剩多少秒（0=未冷却），供看板可观测。"""
    return max(0.0, _GAP_COOLDOWN_UNTIL - time.time())


def _http_get(url: str, timeout: int = 15, max_retry: int = None) -> str:
    """带限流退避的 Gamma HTTP GET（P2-4）。

    - 429：进入全局冷却（默认 30s），期间所有调用直接抛 GammaRateLimited，避免反复打 Gamma。
    - 5xx / 网络抖动：指数退避重试（2/4/8s），最多 _GAP_MAX_RETRY 次（可用 max_retry 覆盖）。
    - 其他 4xx：直接抛出（非限流，不重试）。
    - 代理/网关硬失败（502 Bad Gateway / Tunnel connection failed）：重试无意义，立即抛出走降级。
    """
    global _GAP_COOLDOWN_UNTIL
    if time.time() < _GAP_COOLDOWN_UNTIL:
        raise GammaRateLimited("Gamma 冷却中（剩 %.1fs）" % gamma_cooldown_remaining())
    last_err = None
    retries = _GAP_MAX_RETRY if max_retry is None else max_retry
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; QuantTrading/1.0)",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                _GAP_COOLDOWN_UNTIL = time.time() + _GAP_COOLDOWN_SEC
                print("[gamma] 429 限流 -> 进入冷却 %.0fs" % _GAP_COOLDOWN_SEC)
                raise GammaRateLimited("HTTP 429 rate limited; cooldown %.0fs" % _GAP_COOLDOWN_SEC)
            if 500 <= e.code < 600:
                # 代理/网关硬失败（502）重试无意义，立即抛出走离线降级
                if e.code == 502 or "Bad Gateway" in str(getattr(e, "reason", "")):
                    raise
                wait = _GAP_BACKOFF_BASE * (2 ** attempt)
                print("[gamma] %d 服务端错误，退避 %.1fs 重试" % (e.code, wait))
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, OSError) as e:
            last_err = e
            # 代理隧道失败（Tunnel connection failed / 502）属硬失败，重试无意义
            if "502" in str(e) or "Bad Gateway" in str(e) or "Tunnel connection failed" in str(e):
                raise
            wait = _GAP_BACKOFF_BASE * (2 ** attempt)
            print("[gamma] 网络错误(%s)，退避 %.1fs 重试" % (e, wait))
            time.sleep(wait)
            continue
    raise last_err or GammaRateLimited("Gamma 请求失败")


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
            page = json.loads(_http_get(url, timeout=8, max_retry=1))
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


# ---- 跨平台套利：Polymarket 二元市场统一报价（Yes/No 双边盘口） ----
_POLY_QUOTES_TTL = 60.0
_poly_quotes_cache = {"ts": 0.0, "data": None}

# P1-D 盘口冗余数据源：主源 Gamma 全失败时，降级顺序 = CLOB /markets 冗余源 -> 持久化 last-good 缓存。
# 保证模拟盘在 Gamma 抖动/限流时不停摆（用略旧但有效的盘口续跑）。
_FETCH_QUOTES_SOURCE = "gamma"   # gamma | clob | cache | error
_QUOTES_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "quotes_cache.json")


def quotes_source() -> str:
    """当前盘口数据来源（供看板可观测 P1-D）。"""
    return _FETCH_QUOTES_SOURCE


def _persist_quotes(quotes):
    """把成功拉到的盘口持久化为 last-good 缓存（供 Gamma 全失败兜底）。"""
    try:
        os.makedirs(os.path.dirname(_QUOTES_CACHE_PATH), exist_ok=True)
        with open(_QUOTES_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "data": quotes}, f)
    except Exception:
        pass


def _derive_quotes_from_snapshot(snap_markets, limit=300):
    """把快照的 markets 数组（{token_id,question,yes_bid,yes_ask,liquidity,...}）转换为
    统一 Quote 列表（推导 no_bid/no_ask），并做合规/有效性过滤。"""
    out = []
    for m in snap_markets:
        if not isinstance(m, dict):
            continue
        q = m.get("question") or ""
        if _is_blocked(q, m.get("tags")):
            continue
        yb = _to_num(m.get("yes_bid"))
        ya = _to_num(m.get("yes_ask"))
        if not yb or not ya or yb <= 0 or ya <= 0 or ya >= 1:
            continue
        out.append({
            "platform": "poly",
            "id": m.get("token_id"),
            "token_id": m.get("token_id"),
            "event_id": m.get("event_id"),
            "question": q,
            "yes_bid": round(yb, 4), "yes_ask": round(ya, 4),
            "no_bid": round(1 - ya, 4), "no_ask": round(1 - yb, 4),
            "end_date": m.get("end_date"),
            "liquidity": round(_to_num(m.get("liquidity")) or 0.0, 2),
            "ts": float(m.get("ts") or time.time()),
        })
        if len(out) >= limit:
            break
    return out


def _load_persisted_quotes():
    """加载 last-good 盘口（Gamma/CLOB 全失败时的离线兜底）。

    优先级：
      1) quotes_cache.json 的 data（最近一次成功拉取写回，最快）
      2) quotes_ts/quotes_*.jsonl 中「有效市场数最多」的快照（本机曾成功抓过的真实盘口）
    两者皆空才返回 []。选市场数最多的一份，让离线模拟盘尽可能活跃（如 300 个全量盘口），
    而不是卡在 18 个市场的稀疏快照。
    """
    try:
        with open(_QUOTES_CACHE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        cached = d.get("data") or []
        if cached:
            return cached
    except Exception:
        pass
    # 兜底：扫描 quotes_ts 全部快照，挑有效市场数最多的一份
    best = []
    try:
        import glob
        ts_dir = os.path.join(os.path.dirname(_QUOTES_CACHE_PATH), "quotes_ts")
        for fp in sorted(glob.glob(os.path.join(ts_dir, "quotes_*.jsonl"))):
            try:
                snap_markets = []
                with open(fp, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except Exception:
                            continue
                        markets = obj.get("markets") if isinstance(obj, dict) else None
                        if isinstance(markets, list):
                            snap_markets = markets
                        elif isinstance(obj, dict) and "token_id" in obj:
                            snap_markets = [obj]
                out = _derive_quotes_from_snapshot(snap_markets)
                if len(out) > len(best):
                    best = out
            except Exception:
                continue
    except Exception:
        pass
    return best


def fetch_poly_quotes_clob(limit: int = 300) -> list:
    """P1-D 冗余源：CLOB /markets 顶层盘口（token 级 bestBid/bestAsk 直接对应 YES 盘口）。
    地域受限环境可能 404/超时，仅作 Gamma 失败时的兜底，绝不用于主路径；映射失败返回 []。
    """
    url = "https://clob.polymarket.com/markets?limit=%d" % limit
    try:
        txt = _http_get(url, timeout=8, max_retry=1)
    except Exception as e:  # noqa: BLE001
        print("[quotes] CLOB 冗余源不可达: %s" % e)
        return []
    try:
        data = json.loads(txt)
    except Exception:
        return []
    rows = data if isinstance(data, list) else (data.get("data") or [])
    out = []
    for m in rows:
        if not isinstance(m, dict):
            continue
        q = m.get("question") or ""
        if _is_blocked(q, m.get("tags")):
            continue
        # CLOB markets 每条含 tokens 列表（token_id/outcome/bestBid/bestAsk）
        toks = m.get("tokens") or []
        yes = None
        if isinstance(toks, list):
            for t in toks:
                if isinstance(t, dict) and str(t.get("outcome", "")).lower() in ("yes", "1", "true"):
                    yes = t
                    break
            if yes is None and toks:
                yes = toks[0] if isinstance(toks[0], dict) else None
        if not yes:
            continue
        ob = _to_num(yes.get("bestBid"))
        oa = _to_num(yes.get("bestAsk"))
        if not ob or not oa or ob <= 0 or oa <= 0 or oa >= 1:
            continue
        out.append({
            "platform": "poly", "id": m.get("id"),
            "token_id": yes.get("token_id"), "event_id": m.get("eventId"),
            "question": q,
            "yes_bid": round(ob, 4), "yes_ask": round(oa, 4),
            "no_bid": round(1 - oa, 4), "no_ask": round(1 - ob, 4),
            "end_date": m.get("endDate"),
            "liquidity": round(_to_num(m.get("liquidity")) or 0.0, 2),
            "ts": time.time(),
        })
        if len(out) >= limit:
            break
    return out


def fetch_poly_quotes(limit: int = 300, force: bool = False) -> list:
    """返回二元 Polymarket 市场的统一 Quote 列表，供套利引擎使用。

    复用 _fetch_pool() 的深翻页大池（10 页 × 100，按 24h 成交量排序）拉取全站活跃
    市场原始数据（保留 bestBid/bestAsk/clobTokenIds/events），再本地过滤：
      合规(_is_blocked) → 二元结果 → 主侧盘口有真实买卖价。
    主侧(outcomes[0]) 视作 YES 等价；补侧 = 1 - 补价推导。
    额外保留 event_id（来自 m["events"][0]["id"]），供同事件多子市场分组套利。
    不调用 CLOB（本环境 CLOB /book 被地域限制 404），Gamma 顶层盘口更稳定。
    Quote = {platform,id,token_id,event_id,question,category,yes_bid,yes_ask,no_bid,no_ask,end_date,liquidity,ts}
    其中 category 取自 Gamma 原生类目字段（治本：用 Polymarket 真实分类，不再自造关键词表），
    缺失时由调用方 classify() 回退。
    """
    global _FETCH_QUOTES_SOURCE
    now = time.time()
    if not force and _poly_quotes_cache["data"] is not None \
            and now - _poly_quotes_cache["ts"] < _POLY_QUOTES_TTL:
        return _poly_quotes_cache["data"]
    out = []
    err = None
    try:
        rows = _fetch_pool()  # 原始 market 列表（含 bestBid/bestAsk/clobTokenIds）
        for m in rows:
            if not isinstance(m, dict):
                continue
            q = m.get("question") or ""
            if _is_blocked(q, m.get("tags")):
                continue
            outcomes = _parse_outcomes(m)
            # 接受任意二元市场（Yes/No、Up/Down、两队名等）；outcomes[0] 视为主侧
            if not outcomes or len(outcomes) != 2:
                continue
            ob = _to_num(m.get("bestBid"))
            oa = _to_num(m.get("bestAsk"))
            if not ob or not oa or ob <= 0 or oa <= 0 or oa >= 1:
                continue
            toks = m.get("clobTokenIds")
            yes_token = None
            if toks:
                try:
                    yes_token = json.loads(toks)[0]
                except Exception:
                    yes_token = None
            # 提取 event_id 用于同事件子市场分组
            ev = m.get("events")
            event_id = None
            if isinstance(ev, list) and ev and isinstance(ev[0], dict):
                event_id = ev[0].get("id")
            out.append({
                "platform": "poly",
                "id": m.get("id"),
                "token_id": yes_token,
                "event_id": event_id,
                "question": q,
                "yes_bid": round(ob, 4), "yes_ask": round(oa, 4),
                "no_bid": round(1 - oa, 4), "no_ask": round(1 - ob, 4),
                "end_date": m.get("endDate"),   # 到期时间，供时间衰减门控使用
                # 治本：直接取 Gamma 原生类目（真实分类，如 politics/world/crypto/economy…）
                # 缺失/空串时留空，由 sim_server.market_cat() 回退关键词 classify
                "category": (m.get("category") or "").strip().lower(),
                "liquidity": round(_to_num(m.get("liquidityNum"))
                                   or _to_num(m.get("liquidity")) or 0.0, 2),
                "ts": now,
            })
            if len(out) >= limit:
                break
    except Exception as e:  # noqa: BLE001
        err = e
    # Gamma 拉到有效盘口 -> 直接采用并缓存
    if out:
        _persist_quotes(out)
        _FETCH_QUOTES_SOURCE = "gamma"
        _poly_quotes_cache["ts"] = now
        _poly_quotes_cache["data"] = out
        return out
    # Gamma 拉空（无网/限流/异常）-> 降级链：内存缓存 -> CLOB -> 持久化快照
    if _poly_quotes_cache["data"] is not None:
        _FETCH_QUOTES_SOURCE = "gamma-cache"
        return _poly_quotes_cache["data"]
    try:
        clob_out = fetch_poly_quotes_clob(limit)
        if clob_out:
            _persist_quotes(clob_out)
            _FETCH_QUOTES_SOURCE = "clob"
            _poly_quotes_cache["ts"] = time.time()
            _poly_quotes_cache["data"] = clob_out
            print("[quotes] Gamma 为空，已切换 CLOB 冗余源(%d 个)" % len(clob_out))
            return clob_out
    except Exception as ce:  # noqa: BLE001
        print("[quotes] CLOB 冗余源失败: %s" % ce)
    # CLOB 也失败 -> 用持久化 last-good 缓存/快照（降级，保证模拟不中断）
    cached = _load_persisted_quotes()
    if cached:
        _persist_quotes(cached)  # 写回 quotes_cache.json，后续刷新秒级读取不再扫 37MB
        _FETCH_QUOTES_SOURCE = "cache"
        print("[quotes] Gamma 为空，降级使用持久化 last-good 盘口(%d 个)" % len(cached))
        return cached
    _FETCH_QUOTES_SOURCE = "error"
    return [{"error": str(err) if err else "empty"}]


def fetch_price_history(market_id=None, token_id=None, interval="max"):
    """返回 [(t, p), ...] 升序历史价格序列（CLOB prices-history），用于回测。

    CLOB prices-history 原生接受 **clob token id**（即 fetch_poly_quotes 的
    buy_id/sell_id）。故优先把 token_id 或 market_id 直接当 token id 直查；
    仅当直查返回空时，才回退把 market_id 当 Gamma 市场 id 去 markets/{id}
    解析 clobTokenIds[0]。失败返回 [{"error": "..."}]。
    """
    try:
        tid = token_id or market_id
        if not tid:
            return [{"error": "未提供 token_id"}]
        out = _parse_price_history(tid, interval)
        if out:
            return out
        # 直查无数据：回退把 market_id 当 Gamma 市场 id 解析
        if (not token_id) and market_id:
            try:
                detail = json.loads(_http_get("%s/%s" % (_GAMMA, market_id),
                                              timeout=20))
                toks = detail.get("clobTokenIds")
                if toks:
                    try:
                        gtok = json.loads(toks)[0]
                    except Exception:
                        gtok = toks
                    out = _parse_price_history(gtok, interval)
                    if out:
                        return out
            except Exception:
                pass
        return out or [{"error": "无历史价格数据"}]
    except Exception as e:  # noqa: BLE001
        return [{"error": str(e)}]


def _parse_price_history(token_id, interval="max"):
    """用 clob token id 直查 CLOB prices-history，返回 [(t,p),...] 升序。"""
    url = "https://clob.polymarket.com/prices-history?market=%s&interval=%s" \
          % (token_id, interval)
    d = json.loads(_http_get(url, timeout=25))
    hist = (d.get("history") or []) if isinstance(d, dict) else []
    out = []
    for it in hist:
        t = it.get("t")
        p = it.get("p")
        if t is None or p is None:
            continue
        out.append((int(t), float(p)))
    out.sort(key=lambda x: x[0])
    return out


def fetch_resolution_price(token_id, timeout=20):
    """返回该 clob token 的结算价（0/1 或最终概率）。

    用 Gamma markets 详情：clobTokenIds 定位 token 下标，取对应 outcomePrices。
    市场尚未结算（resolved 字段为空 / outcomePrices 仍为非 0/1 概率）时返回 None，
    调用方应以最近中间价作暂估、标 pending，待真正结算后再复核。
    网络/解析失败返回 None。
    """
    try:
        url = "https://gamma-api.polymarket.com/markets?clobTokenIds=%s" % token_id
        d = json.loads(_http_get(url, timeout=timeout))
        if not isinstance(d, list):
            return None
        for m in d:
            toks = m.get("clobTokenIds")
            if not toks:
                continue
            try:
                toks = json.loads(toks) if isinstance(toks, str) else toks
            except Exception:
                toks = []
            if not isinstance(toks, list) or token_id not in toks:
                continue
            idx = toks.index(token_id)
            prices = m.get("outcomePrices")
            if not prices:
                return None
            try:
                prices = json.loads(prices) if isinstance(prices, str) else prices
            except Exception:
                return None
            if 0 <= idx < len(prices):
                return float(prices[idx])
    except Exception:  # noqa: BLE001
        return None
    return None


def clear_cache():
    """清空盘口缓存，强制下一次 fetch_poly_quotes 走真实网络请求。

    背景：fetch_poly_quotes(force=True) 只绕过 _poly_quotes_cache 这一层，
    底层 _fetch_pool() 还有 _POOL_TTL(120s) 缓存，force 管不到它。结果是
    连续多次 force 拉取拿到的是同一批报价（实测 6 秒内 300 个市场价格变化为 0）。
    做市模拟里库存跨轮持有，价格必须真实演化才有风险可言，因此需要真正的清缓存。
    """
    global _pool_cache
    _pool_cache = None
    _poly_quotes_cache["ts"] = 0.0
    _poly_quotes_cache["data"] = None


def fetch_quotes_fresh(limit: int = 300):
    """强制走网络的实时盘口（= clear_cache + fetch_poly_quotes）。"""
    clear_cache()
    return fetch_poly_quotes(limit=limit, force=True)

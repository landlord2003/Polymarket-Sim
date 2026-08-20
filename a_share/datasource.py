"""A股行情多源直连模块：绕过 akshare 的兼容性/限流问题，直接打公开接口。

背景（2026-08-19 实测）：
    akshare 1.18.92 的 stock_zh_a_hist 在本机会被 push2his.eastmoney.com 以
    RemoteDisconnected 掐断（服务端限流/WAF），而腾讯 web.ifzq 与新浪
    getKLineData 均秒回。原引擎 `except Exception: pass` 静默回退合成数据，
    导致用户取消「离线验证」后看到的仍是随机游走假数据且毫无提示。

设计原则：
    1. 多源兜底链，任一源成功即返回，并在 df.attrs['source'] 标注真实来源。
    2. 全部失败时抛出 DataSourceError，由上层决定是否回退合成 —— 绝不静默伪装成真实数据。
    3. 仅用标准库 urllib，不引入 requests，避免依赖地狱。
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time as dtime
from typing import Optional

import pandas as pd

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 12
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


class DataSourceError(RuntimeError):
    """所有数据源均失败。message 内含每个源的失败原因，便于用户排查。"""


def _http_get(url: str, headers: Optional[dict] = None,
              encoding: str = "utf-8", retries: int = 3) -> str:
    """带重试的 GET。东财系接口对同一 IP 有间歇性 WAF 限流
    （表现为 RemoteDisconnected），重试 + 退避可显著提高成功率。"""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                                       "Accept": "*/*",
                                                       **(headers or {})})
            with urllib.request.urlopen(req, timeout=TIMEOUT,
                                        context=_SSL_CTX) as resp:
                raw = resp.read()
            return raw.decode(encoding, errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries - 1:
                time.sleep(0.6 * (attempt + 1))
    raise last  # type: ignore[misc]


def _secid(symbol: str) -> str:
    """东财 secid：沪市(6/5/9开头) 前缀 1.，深市/创业板 0.，北交所 0.。"""
    return ("1." if symbol.startswith(("6", "5", "9")) else "0.") + symbol


def _qt_code(symbol: str) -> str:
    """腾讯/新浪代码：sh600000 / sz300034 / bj430047。"""
    if symbol.startswith(("6", "5", "9")):
        return "sh" + symbol
    if symbol.startswith(("4", "8")):
        return "bj" + symbol
    return "sz" + symbol


def _finalize(df: pd.DataFrame, source: str, adjusted: bool) -> pd.DataFrame:
    df = df.dropna(subset=["close"])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.attrs["source"] = source
    df.attrs["adjusted"] = adjusted
    df.attrs["synthetic"] = False
    return df


# ---------------------------------------------------------------- 日K线 三源

def _kline_tencent(symbol: str, days: int = 320) -> pd.DataFrame:
    """腾讯 web.ifzq 前复权日K（实测最稳，交易时段内含当日实时价）。"""
    code = _qt_code(symbol)
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={code},day,,,{days},qfq")
    data = json.loads(_http_get(url))
    node = (data.get("data") or {}).get(code) or {}
    rows = node.get("qfqday") or node.get("day") or []
    if not rows:
        raise ValueError("腾讯返回空K线")
    recs = []
    for r in rows:
        # [日期, 开, 收, 高, 低, 成交量(手)]
        recs.append({
            "date": pd.to_datetime(r[0]),
            "open": float(r[1]), "close": float(r[2]),
            "high": float(r[3]), "low": float(r[4]),
            "volume": float(r[5]) * 100,
        })
    df = pd.DataFrame(recs).set_index("date")
    return _finalize(df[["open", "high", "low", "close", "volume"]],
                     "腾讯财经(前复权)", True)


def _kline_eastmoney(symbol: str, days: int = 320) -> pd.DataFrame:
    """东财 push2his 前复权日K（本机常被限流，仍作为一路备选）。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={_secid(symbol)}&fields1=f1,f2,f3,f4,f5"
           "&fields2=f51,f52,f53,f54,f55,f56,f57"
           f"&klt=101&fqt=1&beg=0&end=20500101&lmt={days}")
    data = json.loads(_http_get(url))
    rows = ((data.get("data") or {}).get("klines")) or []
    if not rows:
        raise ValueError("东财返回空K线")
    recs = []
    for line in rows:
        p = line.split(",")
        recs.append({
            "date": pd.to_datetime(p[0]),
            "open": float(p[1]), "close": float(p[2]),
            "high": float(p[3]), "low": float(p[4]),
            "volume": float(p[5]),
        })
    df = pd.DataFrame(recs).set_index("date")
    return _finalize(df[["open", "high", "low", "close", "volume"]],
                     "东方财富(前复权)", True)


def _kline_sina(symbol: str, days: int = 300) -> pd.DataFrame:
    """新浪 getKLineData 日K（不复权，作为最后一路兜底并明确标注）。"""
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={_qt_code(symbol)}"
           f"&scale=240&ma=no&datalen={days}")
    text = _http_get(url).strip()
    if not text or text in ("null", "[]"):
        raise ValueError("新浪返回空K线")
    arr = json.loads(re.sub(r"(\w+):", r'"\1":', text))
    recs = [{
        "date": pd.to_datetime(r["day"]),
        "open": float(r["open"]), "high": float(r["high"]),
        "low": float(r["low"]), "close": float(r["close"]),
        "volume": float(r["volume"]),
    } for r in arr]
    df = pd.DataFrame(recs).set_index("date")
    return _finalize(df[["open", "high", "low", "close", "volume"]],
                     "新浪财经(不复权)", False)


def fetch_kline(symbol: str, days: int = 320) -> pd.DataFrame:
    """多源兜底取日K。全败则抛 DataSourceError（附各源失败原因）。"""
    errors = []
    for name, fn in (("腾讯", _kline_tencent),
                     ("东财", _kline_eastmoney),
                     ("新浪", _kline_sina)):
        try:
            df = fn(symbol, days)
            if len(df) >= 30:
                return df
            errors.append(f"{name}:仅{len(df)}根不足30")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}:{type(e).__name__}")
    raise DataSourceError(f"{symbol} 全部数据源失败 → " + " / ".join(errors))


# ------------------------------------------------------------ 实时快照(批量)

def fetch_realtime(symbols: list[str]) -> dict:
    """新浪批量实时快照 → {symbol: {name, open, prev_close, price, pct}}。

    一次请求可拿全部自选股当前价，用于面板实时刷新与「现价」列。
    """
    if not symbols:
        return {}
    codes = ",".join(_qt_code(s) for s in symbols)
    out = {}
    try:
        text = _http_get(f"https://hq.sinajs.cn/list={codes}",
                         headers={"Referer": "https://finance.sina.com.cn"},
                         encoding="gbk")
    except Exception:  # noqa: BLE001
        return {}
    for line in text.strip().split("\n"):
        if '"' not in line:
            continue
        try:
            code = line.split("=")[0].split("_")[-1]
            sym = re.sub(r"^(sh|sz|bj)", "", code)
            p = line.split('"')[1].split(",")
            if len(p) < 4 or not p[3]:
                continue
            price = float(p[3])
            prev = float(p[2]) if p[2] else 0.0
            # 集合竞价前 price 可能为 0，用昨收兜底
            if price <= 0:
                price = prev
            pct = (price / prev - 1) * 100 if prev > 0 else 0.0
            out[sym] = {"name": p[0], "open": float(p[1] or 0),
                        "prev_close": prev, "price": price, "pct": pct}
        except Exception:  # noqa: BLE001
            continue
    return out


# ------------------------------------------------------------------ 资金流向

def fetch_money_flow(symbol: str, limit: int = 10) -> list[float]:
    """东财个股资金流：返回最近 limit 日主力净流入(元)，最新在最后。"""
    url = ("https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get"
           f"?secid={_secid(symbol)}&fields1=f1,f2,f3,f7"
           "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
           f"&klt=101&lmt={limit}")
    data = json.loads(_http_get(url))
    rows = ((data.get("data") or {}).get("klines")) or []
    if not rows:
        raise DataSourceError(f"{symbol} 资金流返回空")
    # 格式：日期,主力净流入,小单,中单,大单,超大单,...
    return [float(line.split(",")[1]) for line in rows]


# -------------------------------------------------------------------- 指数

def fetch_index_kline(index_code: str = "sh000300", days: int = 60) -> pd.DataFrame:
    """沪深300等指数日K（腾讯源）。"""
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={index_code},day,,,{days},qfq")
    data = json.loads(_http_get(url))
    node = (data.get("data") or {}).get(index_code) or {}
    rows = node.get("qfqday") or node.get("day") or []
    if not rows:
        raise DataSourceError(f"{index_code} 指数K线为空")
    recs = [{"date": pd.to_datetime(r[0]), "close": float(r[2])} for r in rows]
    return pd.DataFrame(recs).set_index("date").sort_index()


# -------------------------------------------------------------------- 新闻

def fetch_news_titles(symbol: str, limit: int = 10) -> list[str]:
    """东财个股新闻标题（搜索接口）。失败抛异常，由上层置中性。"""
    url = ("https://search-api-web.eastmoney.com/search/jsonp"
           "?cb=cb&param=" + urllib.parse.quote(json.dumps({
               "uid": "", "keyword": symbol,
               "type": ["cmsArticleWebOld"],
               "client": "web", "clientType": "web", "clientVersion": "curr",
               "param": {"cmsArticleWebOld": {
                   "searchScope": "default", "sort": "default",
                   "pageIndex": 1, "pageSize": limit,
                   "preTag": "", "postTag": ""}},
           }, ensure_ascii=False)))
    text = _http_get(url)
    m = re.search(r"cb\((.*)\)", text, re.S)
    if not m:
        raise DataSourceError("新闻接口返回格式异常")
    data = json.loads(m.group(1))
    items = (((data.get("result") or {}).get("cmsArticleWebOld")) or [])
    titles = [str(it.get("title", "")) for it in items if it.get("title")]
    if not titles:
        raise DataSourceError("新闻结果为空")
    return titles


# ------------------------------------------------------- 个股快照(估值/市值)

# 东方财富 push2 qt/stock/get 字段码（按公开文档经验映射，取数失败即降级，绝不乱填）
_FIELDS_SNAPSHOT = (
    "f43,f44,f45,f46,f47,f48,f49,f50,f51,f52,f55,f57,f58,"
    "f59,f116,f117,f162,f167,f168,f169"
)


def fetch_snapshot(symbol: str) -> dict:
    """东财个股快照：现价/涨跌幅/最高/最低/今开/昨收/成交量/成交额/振幅/
    量比/总市值/流通市值/市盈率(动)/市净率/换手率。

    返回 dict（金额单位：元）。任一核心字段缺失置 None，由上层显示「—」。
    失败抛 DataSourceError。
    """
    url = ("https://push2.eastmoney.com/api/qt/stock/get"
           f"?secid={_secid(symbol)}&fields={_FIELDS_SNAPSHOT}&invt=2&fltt=2")
    data = json.loads(_http_get(url))
    d = (data.get("data") or {})
    if not d or d.get("f57") is None:
        raise DataSourceError(f"{symbol} 快照返回空")
    g = lambda k, t=float: _safe(d.get(k), t)  # noqa: E731

    def _safe(v, t):
        if v in (None, "", "-"):
            return None
        try:
            return t(v)
        except Exception:
            return None

    snap = {
        "symbol": symbol,
        "name": d.get("f58"),
        "price": _safe(d.get("f43")),
        "pct": _safe(d.get("f44")),
        "change": _safe(d.get("f45")),
        "high": _safe(d.get("f46")),
        "low": _safe(d.get("f47")),
        "open": _safe(d.get("f48")),
        "prev_close": _safe(d.get("f49")),
        "volume": _safe(d.get("f50")),
        "amount": _safe(d.get("f51")),
        "amplitude": _safe(d.get("f52")),
        "vol_ratio": _safe(d.get("f59")),
        "mktcap": _safe(d.get("f116")),
        "float_mktcap": _safe(d.get("f117")),
        "pe": _safe(d.get("f167")),
        "pb": _safe(d.get("f162")),
        "turnover": _safe(d.get("f168")),
    }
    return snap


# ----------------------------------------------------- 资金流向分项(主/大/中/小单)

def fetch_fund_flow_breakdown(symbol: str) -> dict:
    """东财个股资金流：返回最新一日的 主力/超大单/大单/中单/小单 净流入(元)。"""
    url = ("https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get"
           f"?secid={_secid(symbol)}&fields1=f1,f2,f3,f7"
           "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
           "&klt=101&lmt=1")
    data = json.loads(_http_get(url))
    rows = ((data.get("data") or {}).get("klines")) or []
    if not rows:
        raise DataSourceError(f"{symbol} 资金流分项返回空")
    p = rows[0].split(",")
    # 标准列序：日期,主力,小单,中单,大单,超大单,主占比,小占比,中占比,大占比,超大占比,收盘,涨跌幅
    def _safe(v):
        try:
            return float(v)
        except Exception:
            return 0.0
    return {
        "main": _safe(p[1]), "retail": _safe(p[2]), "mid": _safe(p[3]),
        "big": _safe(p[4]), "huge": _safe(p[5]),
    }


# ------------------------------------------------------------- F10 主营财务

def fetch_financials(symbol: str) -> dict:
    """东财 F10 主营财务（最新一期）：营收/归母净利润/ROE/毛利率/净利同比。"""
    flt = urllib.parse.quote(f'(SECURITY_CODE="{symbol}")')
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
           "?reportName=RPT_F10_FINANCE_MAIN&columns=ALL"
           f"&filter={flt}&pageSize=1&sortColumns=REPORT_DATE&sortTypes=-1"
           "&source=WEB&client=WEB&v=0.1")
    text = _http_get(url, encoding="utf-8")
    data = json.loads(text)
    arr = (((data.get("result") or {}).get("data")) or [])
    if not arr:
        raise DataSourceError(f"{symbol} 财务数据为空")
    it = arr[0]

    def _safe(k, t=float):
        v = it.get(k)
        if v in (None, "", "-"):
            return None
        try:
            return t(v)
        except Exception:
            return None

    return {
        "report_date": str(it.get("REPORT_DATE") or "")[:10],
        "revenue": _safe("TOTAL_OPERATE_INCOME"),
        "net_profit": _safe("PARENT_NETPROFIT"),
        "roe": _safe("WEIGHTAVG_ROE"),
        "gross_margin": _safe("XSMLL"),
        "profit_yoy": _safe("PARENT_NETPROFIT_YOY"),
    }


# ------------------------------------------------------- 加密货币公开行情(无需密钥)

def fetch_crypto_quotes(symbols: Optional[list[str]] = None) -> dict:
    """通过 ccxt 公共 ticker 拉取主流加密货币行情（无需 API 密钥）。

    默认覆盖 BTC/ETH/BNB/SOL/XRP/DOGE/ADA/TON。失败返回 ok=False（不抛），
    由上层显示「加密行情不可用」。
    """
    if symbols is None:
        symbols = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT",
                   "XRP/USDT", "DOGE/USDT", "ADA/USDT", "TON/USDT"]
    try:
        import ccxt
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"ccxt 未安装: {e}", "quotes": []}
    try:
        ex = ccxt.binance()
        ex.enableRateLimit = True
        tickers = ex.fetch_tickers(symbols)
        out = []
        for s in symbols:
            t = tickers.get(s)
            if not t:
                continue
            out.append({
                "symbol": s,
                "price": t.get("last"),
                "pct": t.get("percentage"),
                "quote_volume": t.get("quoteVolume"),
            })
        return {"ok": True, "quotes": out}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": str(e), "quotes": []}


# ---------------------------------------------------------------- 交易时段

def market_phase(now: Optional[datetime] = None) -> tuple[str, bool]:
    """返回 (描述, 是否盘中)。周末/节假日不做日历校验，仅按工作日+时段粗判。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return "休市（周末）", False
    t = now.time()
    if dtime(9, 15) <= t < dtime(9, 30):
        return "集合竞价", True
    if dtime(9, 30) <= t <= dtime(11, 30):
        return "盘中（上午）", True
    if dtime(11, 30) < t < dtime(13, 0):
        return "午间休市", False
    if dtime(13, 0) <= t <= dtime(15, 0):
        return "盘中（下午）", True
    if t < dtime(9, 15):
        return "开盘前", False
    return "已收盘", False


if __name__ == "__main__":  # 自检
    import sys
    syms = sys.argv[1:] or ["300034", "688786", "300174", "002085"]
    print("时段:", market_phase())
    print("\n--- 实时快照 ---")
    for s, v in fetch_realtime(syms).items():
        print(f"  {s} {v['name']} 现价{v['price']:.2f} ({v['pct']:+.2f}%)")
    print("\n--- 日K多源 ---")
    for s in syms:
        try:
            df = fetch_kline(s)
            print(f"  {s}: {len(df)}根 源={df.attrs['source']} "
                  f"最新={df['close'].iloc[-1]:.2f} 日期={df.index[-1].date()}")
        except DataSourceError as e:
            print(f"  {s}: FAIL {e}")

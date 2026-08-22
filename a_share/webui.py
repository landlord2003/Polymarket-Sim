"""本地 Web 可视面板（零额外依赖，仅 Python 标准库）

启动：  python a_share/webui.py
打开：  http://127.0.0.1:8787   （端口可改环境变量 QT_WEB_PORT）

功能：
  - 顶部控制条：日常盯盘 / 板块选股 / 全部运行 / 离线验证 / 推送钉钉
  - 下方 iframe 实时显示信号看板（与 run_daily 同一引擎、同一 HTML 模板）
  - 扫描在后台线程跑（AkShare 联网取数可能要 30–90 秒），页面每 2 秒轮询状态
  - 无需敲命令行：扫描由面板按钮启动，结果自动刷新到页面

说明：
  - A股仍只给信号不自动下单；「推送钉钉」勾选后，联网实跑且配置了 .env 时推送手机。
  - 离线验证：用合成数据跑通链路、不触网，适合先把引擎/界面跑顺。
"""

from __future__ import annotations

import os
import sys
import json
import traceback
import webbrowser
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import akshare_factors as af  # 阶段1 免费多源因子（市场宽度等）

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import run_daily
from run_daily import load_watchlist, build_report, WATCHLIST
from signal_engine import (analyze_stock, StockResult, load_signal_state,
                           save_signal_state, state_fresh)
from screener import run_screener, load_sectors
import re
from html import escape
from notify import send_markdown, send_wecom
from datasource import (fetch_realtime, market_phase, fetch_snapshot,
                       fetch_snapshot_tencent, fetch_fund_flow_breakdown,
                       fetch_financials, fetch_news_titles,
                       fetch_crypto_quotes, fetch_kline, DataSourceError)


# ----------------------------------------------------- 信号持久化读取（每日一次）
def _fetch_live_price(symbol: str):
    """轻量实时价（新浪批量），仅用于显示与盘中提示；失败返回 None。"""
    try:
        d = fetch_realtime([symbol])
        return d.get(symbol)
    except Exception:  # noqa: BLE001
        return None


def _intraday_alert(item: dict, live_price):
    """盘中实时价相对决策带（建仓区/趋势线/阻力位）的提示，仅信息不改信号。"""
    rules = item.get("rules") or {}
    if live_price is None:
        return ""
    br = rules.get("buy_range")
    sl = rules.get("stop_loss")
    rs = rules.get("resistance")
    if sl is not None and live_price <= sl:
        return f"⚠️ 盘中跌破趋势线 {sl}"
    if isinstance(br, (list, tuple)) and len(br) == 2 and br[0] <= live_price <= br[1]:
        return f"✅ 盘中处于建仓区 {br[0]}~{br[1]}"
    if rs is not None and live_price >= rs:
        return f"📈 盘中触及阻力位 {rs}"
    return ""


def get_watchlist_signals(watch: dict, offline: bool = False,
                          max_age_hours: int = 24):
    """返回 (results, as_of)。

    - 状态缺失或超 24h：实时重算四维度信号并持久化（每日一次，代价可控）。
    - 状态新鲜：直接复用持久化信号（稳定，不再随盘中 tick 秒翻），
      仅拉轻量实时价更新「现价」与「盘中提示」。

    根治「万丰奥威上午买入、下午观望」：信号是每日收盘决策，盘中价只做提示。
    """
    weights = watch.get("weights")
    holding = watch.get("holding", False)
    state = load_signal_state()
    state_as_of = state.get("as_of", "")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    if offline or not state_fresh(state, max_age_hours):
        # 每轮只取一次市场宽度（东财接口慢且易限流），避免每只股票重复调用
        breadth = None
        if not offline:
            try:
                breadth = af.fetch_market_breadth()
            except Exception:
                breadth = None
        results = []
        for item in watch["watchlist"]:
            r = analyze_stock(item["symbol"], item.get("name", ""),
                              rules=item.get("rules"), weights=weights,
                              holding=holding, force_offline=offline,
                              breadth=breadth)
            results.append(r)
        as_of = save_signal_state(results) if not offline else now
        return results, as_of

    # 状态新鲜：用持久化信号重建，避免每条都重算导致秒翻
    results = []
    forced = False
    for item in watch["watchlist"]:
        sym = item["symbol"]
        name = item.get("name", "")
        s = state.get("symbols", {}).get(sym)
        if not s:
            r = analyze_stock(sym, name, rules=item.get("rules"), weights=weights,
                              holding=holding, force_offline=offline,
                              breadth=None)
            results.append(r)
            forced = True
            continue
        # 自愈：缓存为离线合成、但本次联网 → 强制重算该标的，
        # 使其摆脱「永久合成」状态（根治万丰奥威一直显示合成）。
        if (not offline) and s.get("offline", False):
            r = analyze_stock(sym, name, rules=item.get("rules"), weights=weights,
                              holding=holding, force_offline=offline,
                              breadth=None)
            results.append(r)
            forced = True
            continue
        r = StockResult(
            symbol=sym, name=s.get("name", name),
            market_score=s.get("market_score", 0.0),
            money_score=s.get("money_score", 0.0),
            sector_score=s.get("sector_score", 0.0),
            news_score=s.get("news_score", 0.0),
            composite=s.get("composite", 0.0),
            signal=s.get("signal", ""), signal_emoji=s.get("emoji", ""),
            notes=s.get("notes", []), source=s.get("source", ""),
            data_date=s.get("data_date", ""), offline=s.get("offline", False),
            as_of=s.get("as_of", state_as_of),
        )
        live = _fetch_live_price(sym)
        if live:
            r.last_price = live.get("price")
            r.pct_change = live.get("pct")
            r.intraday_alert = _intraday_alert(item, live.get("price"))
        else:
            r.intraday_alert = _intraday_alert(item, r.last_price)
        results.append(r)
    as_of = save_signal_state(results) if (forced and not offline) else state_as_of
    return results, as_of
import sim_engine
from dashboard import (render_dashboard, render_stock_detail, render_portfolio)

PORT = int(os.getenv("QT_WEB_PORT", "8787"))

_state = {
    "running": False,
    "mode": None,
    "offline": False,
    "push": False,
    "started_at": None,
    "finished_at": None,
    "html": None,
    "error": None,
    "log": [],
    "progress": None,  # 扫描进度: {"label":..., "done":..., "total":...}
}
_lock = threading.Lock()


def _placeholder_html(progress: dict = None) -> str:
    if progress:
        label = progress.get("label", "")
        done = progress.get("done", 0)
        total = progress.get("total", 0)
        pct = int(done / total * 100) if total else 0
        bar = ("<div style='width:240px;height:8px;background:#1c2530;"
               "border-radius:4px;margin:14px auto 6px;overflow:hidden'>"
               f"<div style='width:{pct}%;height:100%;background:#1f6feb'></div></div>")
        body = (f"<h2>🔍 扫描中…</h2>"
                f"<p>{label}　{done}/{total}</p>{bar}"
                "<p style='color:#6b7888;font-size:12px'>并发取数中，页面会自动刷新</p>")
    else:
        body = ("<h2>📡 尚未运行</h2>"
                "<p>点击上方「日常盯盘」或「板块选股」开始扫描</p>"
                "<p style='color:#6b7888;font-size:12px'>"
                "首次联网取数约需 30–90 秒，请耐心等待，页面会自动刷新</p>")
    return (
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<style>body{background:#0f1419;color:#9fb0c0;font-family:-apple-system,"
        "'Microsoft YaHei',sans-serif;display:flex;align-items:center;"
        "justify-content:center;height:60vh;text-align:center;margin:0}"
        "h2{color:#e6e6e6} p{font-size:13px}</style></head><body>"
        f"<div>{body}</div></body></html>"
    )




# -------------------------------------------------- 股票详情聚合 + 30s 内存缓存
_DETAIL_CACHE = {}
_DETAIL_CACHE_T = {}


def _resolve_symbol(text):
    """6位代码直接用；否则按持仓名称反查代码；都失败则原样返回。"""
    text = (text or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{6}", text):
        return text
    try:
        for it in load_watchlist().get("watchlist", []):
            if it.get("name") == text:
                return it["symbol"]
    except Exception:
        pass
    return text


def _save_watchlist(wl: dict):
    """把自选股写回 watchlist.json（新增/删除用）。"""
    try:
        with open(WATCHLIST, "w", encoding="utf-8") as _f:
            json.dump(wl, _f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:  # noqa: BLE001
        print("save watchlist failed:", e)
        return False


def _safe_json(obj):
    """递归把任意对象转成 JSON 原生类型，杜绝 json.dumps 因 tuple 键/numpy 类型
    等抛错（曾导致连接被静默掐断）。"""
    if isinstance(obj, dict):
        return {str(k) if not isinstance(k, (str, int, float, bool)) else k:
                _safe_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_safe_json(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    try:
        if hasattr(obj, "item"):  # numpy scalar
            return obj.item()
    except Exception:  # noqa: BLE001
        pass
    try:
        return float(obj)
    except Exception:  # noqa: BLE001
        pass
    try:
        return str(obj)
    except Exception:  # noqa: BLE001
        return "<unserializable>"


def _log_err(msg):
    """把详情接口的真实异常落盘，便于连接异常时排查（位于 quant-trading/ 同级）。"""
    try:
        with open(os.path.join(os.path.dirname(HERE), "webui_error.log"),
                  "a", encoding="utf-8") as f:
            f.write("[" + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "] "
                    + msg + "\n")
    except Exception:  # noqa: BLE001
        pass


def _fetch_snapshot_best(sym):
    """快照优先腾讯(稳)，失败/缺价回退东财；估值字段优先用东财补全。"""
    snap_t = snap_e = None
    try:
        snap_t = fetch_snapshot_tencent(sym, fast=True)
    except Exception:  # noqa: BLE001
        snap_t = None
    try:
        snap_e = fetch_snapshot(sym, fast=True)
    except Exception:  # noqa: BLE001
        snap_e = None
    if not snap_t and not snap_e:
        raise DataSourceError("快照(腾讯/东财)均不可用")
    snap = snap_t or snap_e
    # 用东财补全腾讯缺失的估值字段（pe/pb/市值/换手等）
    if snap is snap_t and snap_e:
        for k in ("pe", "pb", "mktcap", "float_mktcap", "turnover",
                  "amplitude", "vol_ratio", "amount"):
            if not snap.get(k) and snap_e.get(k) is not None:
                snap[k] = snap_e[k]
    return snap


def _fetch_financials_best(sym):
    try:
        return fetch_financials(sym, fast=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        from tushare_adapter import fetch_financials_tushare
        r = fetch_financials_tushare(sym)
        if r:
            return r
    except Exception:  # noqa: BLE001
        pass
    return DataSourceError("财务(东财/Tushare)均不可用")


def _fetch_flow_best(sym):
    try:
        return fetch_fund_flow_breakdown(sym, fast=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        from tushare_adapter import fetch_money_flow_tushare
        r = fetch_money_flow_tushare(sym)
        if r:
            return r
    except Exception:  # noqa: BLE001
        pass
    return DataSourceError("资金流(东财/Tushare)均不可用")


def _build_stock_detail(sym):
    """并发抓取详情数据，整体 8s 硬上限。

    此前是同步串行取数（快照/资金流/财务/K线/新闻），单只最慢可阻塞 200+ 秒，
    期间 HTTP 连接空闲，被浏览器/杀毒软件掐断 → 浏览器收到
    "Remote end closed connection without response"。

    现改为 fast 模式并发（单源 6s/1 重试）+ 整体 8s 上限：超时的源直接丢弃，
    用 K 线等兜底数据回填，保证连接存活、详情页秒开。

    返回值说明：
        - error: 仅当完全拿不到价（快照+K线均失败）时才置错误文本。
        - warnings: 部分源失败的提示列表（如东财快照被限流），
          上层应显示数据并附带提示，而不是直接报错。
    """
    synthetic = False
    source = ""
    data_date = ""
    error = None
    warnings = []
    snap = {}
    ff = {}
    fin = {}
    kline = []
    news = []

    def _safe(fn, label):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            # 数据源异常统一转为用户友好的短文本，避免把 urllib 底层异常
            # 直接显示，让用户误以为是浏览器/服务器连接断了。
            etype = type(e).__name__
            detail = str(e)
            if "Remote end closed" in detail or "RemoteDisconnected" in etype:
                reason = "连接被数据源关闭/限流"
            elif "timeout" in detail.lower() or "timed out" in detail.lower():
                reason = "请求超时"
            elif "空" in detail or "empty" in detail.lower():
                reason = "返回空数据"
            else:
                reason = etype
            return DataSourceError(f"{label} 暂时不可用（{reason}）")

    import concurrent.futures as cf
    ex = cf.ThreadPoolExecutor(max_workers=5)
    try:
        f_snap = ex.submit(_safe, lambda: _fetch_snapshot_best(sym), "快照(腾讯/东财)")
        f_ff = ex.submit(_safe, lambda: _fetch_flow_best(sym), "资金流(东财/Tushare)")
        f_fin = ex.submit(_safe, lambda: _fetch_financials_best(sym), "财务(东财/Tushare)")
        f_kl = ex.submit(_safe, lambda: fetch_kline(sym, days=60, fast=True), "日K(腾讯/东财/新浪)")
        f_news = ex.submit(_safe, lambda: fetch_news_titles(sym, limit=8), "新闻(东财)")
        done, _ = cf.wait([f_snap, f_ff, f_fin, f_kl, f_news], timeout=8)
        for fut, key in ((f_snap, "snap"), (f_ff, "ff"), (f_fin, "fin"),
                         (f_kl, "kline"), (f_news, "news")):
            if fut not in done:
                warnings.append(f"{key}: 超时未完成")
                continue
            r = fut.result()
            if isinstance(r, Exception):
                warnings.append(str(r))
                continue
            if key == "snap" and isinstance(r, dict):
                snap = r
            elif key == "ff" and isinstance(r, dict):
                ff = r
            elif key == "fin" and isinstance(r, dict):
                fin = r
            elif key == "news" and isinstance(r, list):
                news = r
            elif key == "kline":
                try:
                    kline = [{"date": str(idx.date()), "open": float(r.open),
                              "close": float(r.close), "low": float(r.low),
                              "high": float(r.high)}
                             for idx, r in r.iterrows()]
                    source = r.attrs.get("source", "") or ""
                    data_date = str(r.index[-1].date()) if len(r) else ""
                except Exception:  # noqa: BLE001
                    kline = []
    finally:
        # 放弃未完成的慢任务，立即归还连接（不等待）
        ex.shutdown(wait=False, cancel_futures=True)

    if isinstance(snap, dict):
        source = source or snap.get("source", "")
        data_date = data_date or snap.get("data_date", "")

    # 降级：东财快照被限流(snap 为空)时，用腾讯K线最后一根回填现价/开高低/昨收，
    # 保证详情页永远有真实价（K线源最稳）。
    kline_backfilled = False
    if (not isinstance(snap, dict) or not snap.get("price")) and len(kline) >= 1:
        last = kline[-1]
        prev = kline[-2]["close"] if len(kline) >= 2 else last["open"]
        snap = snap if isinstance(snap, dict) else {}
        snap["price"] = last["close"]
        snap["open"] = last["open"]
        snap["high"] = last["high"]
        snap["low"] = last["low"]
        snap["prev_close"] = prev
        snap["pct"] = round((last["close"] / prev - 1) * 100, 2) if prev else 0.0
        if not source:
            source = "腾讯财经(前复权K线回填)"
        kline_backfilled = True

    # 只有当快照和 K 线都失败、完全没有价格数据时，才把错误传给上层阻断显示。
    has_price = bool(snap.get("price")) or (kline_backfilled and len(kline) >= 1)
    if not has_price:
        error = "; ".join(warnings) if warnings else "无法获取行情数据"
    elif warnings:
        # 有价格但部分源失败：降级为提示，不阻断页面
        warnings.append("已用可用数据源回填")

    return {
        "snapshot": snap if isinstance(snap, dict) else {},
        "fund_flow": ff,
        "financials": fin,
        "kline": kline,
        "news": news,
        "synthetic": synthetic,
        "source": source,
        "data_date": data_date,
        "error": error,
        "warnings": warnings,
    }


def _detail_cache_get(sym):
    now = time.time()
    if sym in _DETAIL_CACHE and now - _DETAIL_CACHE_T.get(sym, 0) < 30:
        return _DETAIL_CACHE[sym]
    d = _build_stock_detail(sym)
    _DETAIL_CACHE[sym] = d
    _DETAIL_CACHE_T[sym] = now
    return d


def control_html() -> str:
    """顶部控制条 SPA：日常盯盘/板块选股/全部运行/查任意股票/模拟仓/
    板块下拉勾选/自动刷新/实时报价条/加密行情区。"""
    try:
        _all_sec = load_sectors(include_extra=True)
    except Exception:  # noqa: BLE001
        _all_sec = []
    _sec_boxes = "".join(
        '<label class="sec"><input type="checkbox" class="secChk" value="{0}"{1}>{2}</label>'.format(
            s["label"], ' checked' if s.get("default") else "", s["label"])
        for s in _all_sec
    )
    _html = r"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>量化信号面板</title>
<script src="/static/echarts.min.js"></script>
<style>
* { box-sizing:border-box; }
body { margin:0; background:#0b0f14; color:#e6e6e6;
  font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
.bar { position:sticky; top:0; z-index:20; display:flex; align-items:center;
  gap:8px; flex-wrap:wrap; padding:10px 16px; background:#121821;
  border-bottom:1px solid #232c38; }
.bar h1 { font-size:15px; margin:0 10px 0 0; white-space:nowrap; }
button { background:#1f6feb; color:#fff; border:none; border-radius:8px;
  padding:7px 13px; font-size:13px; cursor:pointer; font-weight:600; }
button:hover { background:#2a7dff; }
button:disabled { background:#2a3340; color:#6b7888; cursor:not-allowed; }
button.ghost { background:#1a2230; color:#9fb0c0; border:1px solid #2a3340; }
button.back { background:#1a2230; color:#cfe0f0; border:1px solid #2a3340;
  margin-left:10px; border-radius:8px; padding:5px 12px; font-size:13px; cursor:pointer; }
button.back:hover { background:#243044; }
label { font-size:13px; color:#9fb0c0; display:flex; align-items:center; gap:4px;
  cursor:pointer; user-select:none; }
.pill { padding:5px 12px; border-radius:16px; font-size:12px; font-weight:600; }
.pill.idle { background:#16341f; color:#5fd98a; }
.pill.run { background:#332c12; color:#e0c45a; }
.pill.phase { background:#1a2430; color:#7fb3d5; }
.pill.trading { background:#16341f; color:#5fd98a; }
#status { font-size:12px; color:#8b98a5; margin-left:auto; }
.sep { width:1px; height:22px; background:#2a3340; margin:0 2px; }
select { background:#1a2230; color:#cfe0f0; border:1px solid #2a3340;
  border-radius:6px; padding:6px 8px; font-size:12px; }
input#qSearch { background:#0f1620; color:#e6e6e6; border:1px solid #2a3340;
  border-radius:6px; padding:6px 9px; font-size:13px; width:190px; }
.ticker { display:flex; gap:18px; flex-wrap:wrap; align-items:center;
  padding:7px 18px; background:#0e141c; border-bottom:1px solid #1c2530;
  font-size:13px; font-variant-numeric:tabular-nums; color:#8b98a5; }
.ticker.crypto { background:#0c1118; }
.ticker .q { white-space:nowrap; }
.ticker .nm { color:#cfe0f0; margin-right:6px; }
.ticker .up { color:#ff5b5b; font-weight:700; }
.ticker .down { color:#2ecc71; font-weight:700; }
.ticker .flat { color:#888; }
.ticker .ts { margin-left:auto; font-size:11px; color:#5a6875; }
iframe { width:100%; height:calc(100vh - 132px); border:none; background:#0f1419; }
.panel { display:none; position:absolute; top:54px; left:16px; z-index:30;
  background:#121821; border:1px solid #2a3340; border-radius:10px; padding:10px 12px;
  max-width:520px; box-shadow:0 8px 30px rgba(0,0,0,.5); }
.panel .sec { display:inline-flex; margin:3px 8px 3px 0; padding:3px 8px;
  background:#0f1620; border:1px solid #232c38; border-radius:14px; font-size:12px; }
.mask { display:none; position:fixed; inset:0; z-index:50;
  background:rgba(0,0,0,.6); align-items:flex-start; justify-content:center; }
.mask.show { display:flex; }
.mbox { background:#0f1419; color:#e6e6e6; margin-top:60px; max-width:760px; width:92%;
  max-height:82vh; overflow:auto; border:1px solid #2a3340; border-radius:12px;
  padding:18px 20px; box-shadow:0 12px 40px rgba(0,0,0,.6); }
.mbox h2 { margin:0 0 6px; font-size:18px; }
.mbox .sub { color:#9fb0c0; font-size:13px; margin-bottom:10px; }
.mbox .close { float:right; background:#2a3340; color:#cfe0f0; border:none;
  border-radius:6px; padding:4px 10px; cursor:pointer; font-size:12px; }
.chart { width:100%; height:300px; margin:8px 0 4px; }
.kpi { display:grid; grid-template-columns:repeat(auto-fill,minmax(120px,1fr)); gap:8px; margin:10px 0; }
.kpi .cell { background:#141b24; border:1px solid #1f2937; border-radius:8px; padding:8px 10px; }
.kpi .k { color:#7f8ea0; font-size:11px; }
.kpi .v { font-size:16px; font-weight:700; margin-top:2px; }
.kpi .v.up { color:#ff5b5b; } .kpi .v.down { color:#2ecc71; } .kpi .v.flat { color:#888; }
.tag-real { color:#5fd98a; background:#15301f; padding:2px 8px; border-radius:10px; font-size:12px; }
.tag-syn { color:#e0a45a; background:#332712; padding:2px 8px; border-radius:10px; font-size:12px; }
</style></head>
<body>
<div class="bar">
  <h1>量化信号面板</h1>
  <input id="qSearch" placeholder="查任意股票(代码/名称)">
  <button onclick="searchStock()">查</button>
  <span class="sep"></span>
  <button id="b1" onclick="scan('daily')">日常盯盘</button>
  <button id="b2" onclick="scan('screener')">板块选股</button>
  <button id="b3" onclick="scan('both')">全部运行</button>
  <button id="bP" onclick="openPortfolio()">模拟仓</button>
  <button class="ghost" onclick="openRecommend()">🤖 自动荐股</button>
  <button class="ghost" onclick="openBacktest()">📈 回测</button>
  <label><input type="checkbox" id="offline"> 离线验证</label>
  <label><input type="checkbox" id="push"> 推送钉钉</label>
  <span class="sep"></span>
  <button class="ghost" onclick="toggleSectors()">板块</button>
  <div id="sectorPanel" class="panel">[[SECTORS]]</div>
  <span class="sep"></span>
  <label><input type="checkbox" id="auto" checked> 自动刷新</label>
  <select id="interval">
    <option value="60">每 1 分钟</option>
    <option value="180" selected>每 3 分钟</option>
    <option value="300">每 5 分钟</option>
    <option value="900">每 15 分钟</option>
  </select>
  <label><input type="checkbox" id="onlyTrading" checked> 仅盘中</label>
  <span id="pill" class="pill idle">空闲</span>
  <span id="phase" class="pill phase"></span>
  <span id="status"></span>
</div>
<div id="cryptoTicker" class="ticker crypto">加密行情加载中…</div>
<div id="ticker" class="ticker">实时报价加载中…</div>
<iframe id="board" src="/api/board"></iframe>
<div id="mainModalMask" class="mask" onclick="if(event.target===this)closeMain()">
  <div id="mainModalBox" class="mbox"></div>
</div>
<script>
let lastFinished=null, lastMode='daily', nextAt=0, isRunning=false, isTrading=false;
function scan(mode){
  lastMode=mode;
  const offline=document.getElementById('offline').checked;
  const push=document.getElementById('push').checked;
  let sectors=null;
  if(mode!=='daily'){
    const chks=document.querySelectorAll('.secChk:checked');
    sectors=[].slice.call(chks).map(c=>c.value);
  }
  fetch('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode,offline,push,sectors})})
    .then(r=>r.json()).then(j=>setStatus(j.msg||'已启动'))
    .catch(e=>setStatus('启动失败：'+e));
}
function setStatus(t){document.getElementById('status').textContent=t;}
function autoTick(){
  const on=document.getElementById('auto').checked;
  const onlyTrading=document.getElementById('onlyTrading').checked;
  if(!on){nextAt=0;return;}
  if(onlyTrading && !isTrading){setStatus('自动刷新已开，但当前非交易时段（取消「仅盘中」可强制）');nextAt=0;return;}
  const iv=parseInt(document.getElementById('interval').value,10)*1000;
  const now=Date.now();
  if(nextAt===0){nextAt=now+iv;return;}
  if(now>=nextAt && !isRunning){nextAt=now+iv;scan(lastMode);}
}
function countdownText(){
  const on=document.getElementById('auto').checked;
  if(!on||nextAt===0)return '';
  return ' ｜ 下次刷新 '+Math.max(0,Math.round((nextAt-Date.now())/1000))+'s';
}
function loadQuotes(){
  fetch('/api/quotes').then(r=>r.json()).then(j=>{
    const el=document.getElementById('ticker');
    if(!j.ok||!j.quotes||!j.quotes.length){el.innerHTML='<span class="flat">实时报价不可用（限流/休市）</span>';return;}
    el.innerHTML=j.quotes.map(q=>{
      const cls=q.pct>0?'up':(q.pct<0?'down':'flat');const sg=q.pct>0?'+':'';
      return '<span class="q"><span class="nm">'+q.name+'</span>'+q.price.toFixed(2)+' <span class="'+cls+'">'+sg+q.pct.toFixed(2)+'%</span></span>';
    }).join('')+'<span class="ts">报价 '+j.ts+'</span>';
  }).catch(()=>{document.getElementById('ticker').innerHTML='<span class="flat">服务已断开</span>';});
}
function loadCrypto(){
  fetch('/api/crypto_quotes').then(r=>r.json()).then(j=>{
    const el=document.getElementById('cryptoTicker');
    if(!j.ok||!j.quotes||!j.quotes.length){el.innerHTML='<span class="flat">加密行情不可用（需联网/限流）</span>';return;}
    el.innerHTML=j.quotes.map(q=>{
      const cls=q.pct>0?'up':(q.pct<0?'down':'flat');const sg=q.pct>0?'+':'';
      const px=q.price!=null?q.price.toFixed(2):'-';
      const pc=q.pct!=null?q.pct.toFixed(2):'0.00';
      return '<span class="q"><span class="nm">'+q.symbol+'</span>'+px+' <span class="'+cls+'">'+sg+pc+'%</span></span>';
    }).join('')+'<span class="ts">'+(j.ts||'')+'</span>';
  }).catch(()=>{document.getElementById('cryptoTicker').innerHTML='<span class="flat">加密行情断开</span>';});
}
function poll(){
  fetch('/api/status').then(r=>r.json()).then(s=>{
    const pill=document.getElementById('pill');
    isRunning=!!s.running; isTrading=!!s.is_trading;
    ['b1','b2','b3'].forEach(id=>document.getElementById(id).disabled=isRunning);
    if(s.running){pill.textContent='扫描中…';pill.className='pill run';}
    else{pill.textContent='空闲';pill.className='pill idle';}
    const ph=document.getElementById('phase');
    ph.textContent=(s.is_trading?'盘中 ':'休市 ')+(s.phase||'')+' '+(s.server_time||'');
    ph.className='pill '+(s.is_trading?'trading':'phase');
    if(s.error){setStatus('错误：'+s.error);}
    else if(s.running&&s.progress){setStatus('扫描中：'+s.progress.label+' '+s.progress.done+'/'+s.progress.total);}
    else if(s.log&&s.log.length){setStatus(s.log[s.log.length-1]+countdownText());}
    autoTick();
    if(!s.running&&s.finished_at&&s.finished_at!==lastFinished){
      lastFinished=s.finished_at;
      document.getElementById('board').src='/api/board?t='+Date.now();
    }
  }).catch(()=>{
    document.getElementById('pill').textContent='服务已断开';
    document.getElementById('pill').className='pill run';
    setStatus('后台服务未运行，请双击「启动看板.bat」重启');
  });
  setTimeout(poll,2000);
}
function toggleSectors(){const p=document.getElementById('sectorPanel');p.style.display=p.style.display==='block'?'none':'block';}
function openPortfolio(){document.getElementById('board').src='/api/portfolio';}
function searchStock(){
  const v=document.getElementById('qSearch').value.trim();
  if(!v){setStatus('请输入代码或名称');return;}
  fetch('/api/stock_detail?symbol='+encodeURIComponent(v)+'&format=json')
    .then(r=>r.json()).then(d=>showMainDetail(v,d))
    .catch(e=>openMain('<div class="sub" style="color:#ef7a66">'+e+'</div>'));
}
function showMainDetail(q,d){
  if(d.error && (!d.snapshot || !d.snapshot.price)){
    openMain('<h2>'+q+'</h2><div class="sub" style="color:#ef7a66">'+d.error+'</div>');return;
  }
  const warn = d.error || (d.warnings && d.warnings.length ? d.warnings.join('；') : '');
  const warnBanner = warn ? '<div class="sub" style="color:#e0a45a;background:#332712;padding:8px 10px;border-radius:6px;margin-bottom:10px">⚠️ 部分数据源失败，已用可用数据回填：'+warn+'</div>' : '';
  const s=d.snapshot||{}, f=d.fund_flow||{}, fin=d.financials||{};
  const cls=(s.pct||0)>0?'up':((s.pct||0)<0?'down':'flat');const sg=(s.pct||0)>0?'+':'';
  const srcTag=d.synthetic?'<span class="tag-syn">合成数据</span>':'<span class="tag-real">'+(d.source||'真实行情')+'</span>';
  let kpi='<div class="kpi">';
  kpi+='<div class="cell"><div class="k">现价</div><div class="v '+cls+'">'+(s.price!=null?s.price.toFixed(2):'-')+'</div></div>';
  kpi+='<div class="cell"><div class="k">涨跌幅</div><div class="v '+cls+'">'+(s.pct!=null?sg+s.pct.toFixed(2)+'%':'-')+'</div></div>';
  kpi+='<div class="cell"><div class="k">市盈率</div><div class="v">'+(s.pe!=null?s.pe.toFixed(2):'-')+'</div></div>';
  kpi+='<div class="cell"><div class="k">市净率</div><div class="v">'+(s.pb!=null?s.pb.toFixed(2):'-')+'</div></div>';
  kpi+='<div class="cell"><div class="k">总市值(亿)</div><div class="v">'+(s.mktcap!=null?(s.mktcap/1e8).toFixed(2):'-')+'</div></div>';
  kpi+='<div class="cell"><div class="k">流通市值(亿)</div><div class="v">'+(s.float_mktcap!=null?(s.float_mktcap/1e8).toFixed(2):'-')+'</div></div>';
  kpi+='</div>';
  let flow='<div class="kpi">';
  const fb=(l,v)=>{if(v==null)return '';const c=v>0?'up':(v<0?'down':'flat');const g=v>0?'+':'';return '<div class="cell"><div class="k">'+l+'</div><div class="v '+c+'">'+g+(v/1e8).toFixed(2)+'亿</div></div>';};
  flow+=fb('主力',f.main)+fb('超大单',f.huge)+fb('大单',f.big)+fb('中单',f.mid)+fb('小单',f.retail);
  flow+='</div>';
  const html='<button class="close" onclick="closeMain()">关闭</button>'
    +'<h2>'+(s.name||q)+' '+(d.symbol||'')+'</h2>'
    +'<div class="sub">'+srcTag+' ｜ 数据日 '+(d.data_date||'-')+'</div>'
    +warnBanner
    +kpi+'<div id="mchart" class="chart"></div>'
    +'<h3 style="font-size:14px;margin:12px 0 4px">资金流向</h3>'+flow;
  openMain(html);
  try{
    const chart=echarts.init(document.getElementById('mchart'));
    const dl=(d.kline||[]).map(x=>x.date);
    const ohlc=(d.kline||[]).map(x=>[x.open,x.close,x.low,x.high]);
    chart.setOption({backgroundColor:'#0d1219',grid:{left:55,right:18,top:16,bottom:28},tooltip:{trigger:'axis'},xAxis:{type:'category',data:dl,axisLabel:{color:'#8b98a5',fontSize:10}},yAxis:{scale:true,axisLabel:{color:'#8b98a5'}},dataZoom:[{type:'inside'},{type:'slider',height:14,bottom:6}],series:[{type:'candlestick',data:ohlc,itemStyle:{color:'#ff5b5b',color0:'#2ecc71',borderColor:'#ff5b5b',borderColor0:'#2ecc71'}}]});
    window.addEventListener('resize',()=>chart.resize());
  }catch(e){}
}
function openMain(h){document.getElementById('mainModalBox').innerHTML=h;document.getElementById('mainModalMask').classList.add('show');}
function openRecommend(){
  openMain('<button class="close" onclick="closeMain()">关闭</button>'
    +'<h2>🤖 横截面相对排名荐股（相对沪深300多空）</h2>'
    +'<div class="sub" id="recSub">加载中…</div>'
    +'<button id="recRefresh" onclick="refreshRecommend()">刷新荐股（后台扫39只）</button>'
    +'<div id="recBody" style="margin-top:10px"></div>');
  fetch('/api/recommend').then(r=>r.json()).then(d=>renderRecommend(d)).catch(e=>{
    document.getElementById('recSub').textContent='加载失败：'+e;
  });
}
function renderRecommend(d){
  const sub=document.getElementById('recSub');
  if(!d.exists){
    sub.textContent='尚未生成荐股名单。点「刷新荐股」后台扫全池（约3-5分钟，39只龙头）。';
    return;
  }
  if(d.version==='xsec'){
    const ver = (d.label_mode==='rel_hs300') ? '相对沪深300' : '绝对涨跌';
    sub.innerHTML='生成：'+d.generated_at+' ｜ 标签：<b>'+ver+'</b> ｜ 视角：未来'+d.horizon+'日 ｜ 多头/空头各 '+d.top_n+' 只 ｜ 有效 '+d.n_valid+'/'+d.n_universe;
    const longRows=(d.longs||[]).map((r,i)=>recRow(r,i+1)).join('');
    const shortRows=(d.shorts||[]).map((r,i)=>recRow(r,i+1)).join('');
    document.getElementById('recBody').innerHTML=
      '<div class="sub" style="color:#8fd">🟢 <b>多头候选</b>（最看好像跑赢沪深300，Top '+d.top_n+'）</div>'
      +recTable(longRows)
      +'<div class="sub" style="color:#f99;margin-top:10px">🔴 <b>空头候选</b>（最看好像跑输沪深300，Bottom '+d.top_n+'）</div>'
      +recTable(shortRows)
      +'<div class="sub" style="margin-top:8px">⚠️ 横截面 IC≈0.08 为<b>弱因子</b>；本名单是「相对沪深300排名最高候选」，多头部/空尾部思路，<b>非单票买卖点</b>，切勿重仓押注，风险自担。</div>';
    return;
  }
  // legacy 旧版（绝对方向 ML 评分）
  sub.textContent='生成：'+d.generated_at+' ｜ 模型：'+d.model+' ｜ 视角：未来'+d.horizon+'日 ｜ 评分阈值≥'+(d.min_prob*100)+'% ｜ 高置信 '+d.rec.length+' 只 / 全池 '+d.picks.length+' 只';
  const list=(d.rec.length?d.rec:d.picks.slice(0,15));
  const rows=list.map((r,i)=>{
    const c=r.pct>0?'up':(r.pct<0?'down':'flat');const sg=r.pct>0?'+':'';
    const f=r.factors; const f2=x=>(x>=0?'+':'')+x.toFixed(2);
    return '<tr>'
      +'<td>'+(i+1)+'</td><td>'+r.symbol+'</td><td>'+r.name+'</td><td>'+r.sector+'</td>'
      +'<td><b>'+(r.prob*100).toFixed(1)+'%</b></td>'
      +'<td>'+r.last.toFixed(2)+'</td>'
      +'<td class="'+c+'">'+sg+r.pct.toFixed(2)+'%</td>'
      +'<td>'+f2(f[0])+'</td><td>'+f2(f[1])+'</td><td>'+f2(f[2])+'</td><td>'+f2(f[3])+'</td><td>'+f2(f[4])+'</td>'
      +'</tr>';
  }).join('');
  const note=d.rec.length?'':('<div class="sub" style="color:#e0a45a">本次无标的达高置信阈值，已展示全池概率最高 Top15。</div>');
  document.getElementById('recBody').innerHTML=
    note
    +'<table style="width:100%;border-collapse:collapse;font-size:12px">'
    +'<tr style="color:#9fb0c0;text-align:left"><th>#</th><th>代码</th><th>名称</th><th>板块</th><th>ML 评分</th><th>最新价</th><th>今日</th><th>趋势</th><th>资金</th><th>轮动</th><th>估值</th><th>大盘</th></tr>'
    +rows+'</table>'
    +'<div class="sub" style="margin-top:8px">⚠️ 历史回测 precision_up 约45-52%（随机基准50%），模型暂无稳定方向 alpha；本「ML 评分」为未校准相对排序、非涨跌概率，仅供参考、风险自担。</div>';
}
function recRow(r,i){
  const c=r.pct>0?'up':(r.pct<0?'down':'flat');const sg=r.pct>0?'+':'';
  return '<tr>'
    +'<td>'+i+'</td><td>'+r.symbol+'</td><td>'+r.name+'</td><td>'+r.sector+'</td>'
    +'<td><b>'+(r.prob*100).toFixed(1)+'%</b></td>'
    +'<td>'+r.last.toFixed(2)+'</td>'
    +'<td class="'+c+'">'+sg+r.pct.toFixed(2)+'%</td>'
    +'</tr>';
}
function recTable(rows){
  return '<table style="width:100%;border-collapse:collapse;font-size:12px">'
    +'<tr style="color:#9fb0c0;text-align:left"><th>#</th><th>代码</th><th>名称</th><th>板块</th><th>跑赢概率</th><th>最新价</th><th>今日</th></tr>'
    +rows+'</table>';
}
function refreshRecommend(){
  const b=document.getElementById('recRefresh'); if(b){b.disabled=true;b.textContent='计算中…';}
  fetch('/api/recommend/refresh',{method:'POST'}).then(r=>r.json()).then(j=>{
    document.getElementById('recSub').textContent=(j.msg||'已触发')+' 完成后刷新本页查看。';
  }).catch(e=>{
    document.getElementById('recSub').textContent='刷新失败：'+e;
  }).finally(()=>{ if(b){b.disabled=false;b.textContent='刷新荐股（后台扫39只）';} });
}
function closeMain(){document.getElementById('mainModalMask').classList.remove('show');}
function openBacktest(){
  const h='<button class="close" onclick="closeMain()">关闭</button>'
    +'<h2>📈 历史回测（信号→真实撮合）</h2>'
    +'<div class="sub">复用 signal_engine 五因子信号，T+1 + 手续费 + 滑点 + 止损撮合；不推送钉钉，仅供复盘。</div>'
    +'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:10px 0">'
    +'<input id="btSym" placeholder="代码 如 300034" style="background:#0f1620;color:#e6e6e6;border:1px solid #2a3340;border-radius:6px;padding:6px 9px;font-size:13px;width:120px" value="300034">'
    +'<label>止损%<input id="btStop" type="number" step="0.5" value="8" style="width:62px;background:#0f1620;color:#e6e6e6;border:1px solid #2a3340;border-radius:6px;padding:5px;margin-left:4px"></label>'
    +'<label>持仓天<input id="btHold" type="number" step="5" value="30" style="width:62px;background:#0f1620;color:#e6e6e6;border:1px solid #2a3340;border-radius:6px;padding:5px;margin-left:4px"></label>'
    +'<label><input id="btSweep" type="checkbox"> 参数扫描(止损×持仓网格)</label>'
    +'<button id="btRun" onclick="runBacktest()">运行回测</button>'
    +'</div>'
    +'<div id="btRes" style="margin-top:10px"></div>';
  openMain(h);
}
function runBacktest(){
  const b=document.getElementById('btRun'); b.disabled=true; b.textContent='回测中…';
  const sym=document.getElementById('btSym').value.trim();
  const stop=parseFloat(document.getElementById('btStop').value||'0')/100;
  const mh=parseInt(document.getElementById('btHold').value||'0',10);
  const sweep=document.getElementById('btSweep').checked;
  const q='symbol='+encodeURIComponent(sym)+'&stop='+stop+'&max_hold='+mh+'&sweep='+(sweep?1:0);
  fetch('/api/backtest?'+q).then(r=>r.json()).then(d=>renderBacktest(d)).catch(e=>{
    document.getElementById('btRes').innerHTML='<div class="sub" style="color:#ef7a66">失败：'+e+'</div>';
  }).finally(()=>{b.disabled=false;b.textContent='运行回测';});
}
function renderBacktest(d){
  const box=document.getElementById('btRes');
  if(d.error){box.innerHTML='<div class="sub" style="color:#ef7a66">'+d.error+'</div>';return;}
  if(d.sweep){
    let rows=(d.grid||[]).map(g=>{
      if(g.error) return '<tr><td>'+(g.stop*100).toFixed(1)+'%</td><td>'+g.max_hold+'</td><td colspan="5">⚠️ '+g.error+'</td></tr>';
      const rc=g.total_return>0?'up':'down';const sg=g.total_return>0?'+':'';
      return '<tr><td>'+(g.stop*100).toFixed(1)+'%</td><td>'+g.max_hold+'</td><td>'+g.n_trades+'</td><td class="'+rc+'">'+sg+(g.total_return*100).toFixed(1)+'%</td><td>'+g.sharpe.toFixed(2)+'</td><td class="down">'+(g.max_dd*100).toFixed(1)+'%</td><td>'+(g.win_rate*100).toFixed(1)+'%</td></tr>';
    }).join('');
    box.innerHTML='<div class="sub">'+d.symbol+' ｜ 网格 '+(d.stops?d.stops.length:0)+'×'+(d.max_holds?d.max_holds.length:0)+'：止损 × 最大持仓</div>'
      +'<table style="width:100%;border-collapse:collapse;font-size:12px"><tr style="color:#9fb0c0;text-align:left"><th>止损</th><th>最大持仓</th><th>交易数</th><th>总收益</th><th>夏普</th><th>最大回撤</th><th>胜率</th></tr>'+rows+'</table>';
    return;
  }
  const r=d.result||{};
  const tag=r.synthetic?'<span class="tag-syn">合成数据(无真实行情)</span>':'<span class="tag-real">真实行情回测</span>';
  let trows=(r.trades||[]).slice(-20).map(t=>{
    const rc=t.net_ret>0?'up':'down';const sg=t.net_ret>0?'+':'';
    return '<tr><td>'+t.entry_date+'</td><td>'+t.exit_date+'</td><td>'+t.entry_price+'→'+t.exit_price+'</td><td>'+t.qty+'</td><td class="'+rc+'">'+sg+(t.net_ret*100).toFixed(1)+'%</td><td>'+t.reason+'</td></tr>';
  }).join('');
  const kpi='<div class="kpi">'
    +'<div class="cell"><div class="k">总收益</div><div class="v '+(r.total_return>0?'up':'down')+'">'+(r.total_return*100).toFixed(1)+'%</div></div>'
    +'<div class="cell"><div class="k">夏普</div><div class="v">'+r.sharpe+'</div></div>'
    +'<div class="cell"><div class="k">最大回撤</div><div class="v down">'+(r.max_dd*100).toFixed(1)+'%</div></div>'
    +'<div class="cell"><div class="k">胜率</div><div class="v">'+((r.win_rate||0)*100).toFixed(1)+'%</div></div>'
    +'<div class="cell"><div class="k">交易数</div><div class="v">'+r.n_trades+'</div></div>'
    +'<div class="cell"><div class="k">末值</div><div class="v">'+r.final_value+'</div></div></div>';
  box.innerHTML=tag+'<div class="sub">'+d.symbol+' ｜ 止损'+(d.stop*100).toFixed(1)+'% ｜ 最大持仓'+d.max_hold+'天 ｜ 信号: signal_engine 五因子</div>'+kpi
    +'<table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:8px"><tr style="color:#9fb0c0;text-align:left"><th>进场</th><th>出场</th><th>价(进→出)</th><th>股数</th><th>净收益</th><th>离场原因</th></tr>'+trows+'</table>'
    +'<div class="sub" style="margin-top:8px">⚠️ 历史回测不代表未来收益；本引擎为 point-in-time 撮合，已含 A 股真实摩擦。</div>';
}
window.onload=()=>{
  poll(); loadQuotes(); loadCrypto();
  setInterval(loadQuotes,10000); setInterval(loadCrypto,10000);
  document.getElementById('auto').addEventListener('change',function(){nextAt=this.checked?Date.now()+parseInt(document.getElementById('interval').value,10)*1000:0;});
  document.getElementById('interval').addEventListener('change',function(){if(document.getElementById('auto').checked){nextAt=Date.now()+parseInt(this.value,10)*1000;}});
  document.getElementById('qSearch').addEventListener('keydown',function(e){if(e.key==='Enter')searchStock();});
};
</script>
</body></html>"""
    return _html.replace("[[SECTORS]]", _sec_boxes)


def _run_scan(mode: str, offline: bool, push: bool, sectors: list = None):
    try:
        with _lock:
            _state["running"] = True
            _state["mode"] = mode
            _state["offline"] = offline
            _state["push"] = push
            _state["started_at"] = time.time()
            _state["finished_at"] = None
            _state["error"] = None
            _state["log"] = ["开始扫描…"]
            _state["progress"] = None

        watch = load_watchlist()
        weights = watch.get("weights")
        holding = watch.get("holding", False)  # 顶层持仓状态：False=空仓
        results = []
        any_offline = False
        as_of = ""
        if mode in ("daily", "both"):
            results, as_of = get_watchlist_signals(watch, offline=offline)
            any_offline = any(r.offline for r in results)

        scr = None
        show_wl = mode in ("daily", "both")
        if mode in ("screener", "both"):

            def _progress(done, total, label):
                with _lock:
                    _state["progress"] = {"done": done, "total": total,
                                          "label": label}

            scr = run_screener(offline=offline, sectors=sectors,
                               progress_cb=_progress)

        mode_tag = "offline" if (offline or any_offline) else "online"
        html = render_dashboard(results if show_wl else [],
                                screener_result=scr, mode=mode_tag,
                                show_watchlist=show_wl, as_of=as_of)

        pushed = False
        if push and not offline and not any_offline and mode in ("daily", "both"):
            try:
                title = f"A股信号日报 {datetime.today().strftime('%Y-%m-%d')}"
                rep = build_report(results)
                send_markdown(title, rep)
                send_wecom(rep)
                pushed = True
            except Exception as e:  # noqa
                _state["log"].append(f"推送失败: {e}")

        with _lock:
            _state["html"] = html
            _state["finished_at"] = time.time()
            _state["log"].append("扫描完成" + ("，已推送钉钉" if pushed else ""))
    except Exception as e:  # noqa
        with _lock:
            _state["error"] = str(e)
            _state["log"].append(f"错误: {e}")
    finally:
        with _lock:
            _state["running"] = False
            _state["progress"] = None


CONTROL_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>量化信号面板</title>
<style>
* { box-sizing:border-box; }
body { margin:0; background:#0b0f14; color:#e6e6e6;
  font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
.bar { position:sticky; top:0; z-index:10; display:flex; align-items:center;
  gap:10px; flex-wrap:wrap; padding:12px 18px; background:#121821;
  border-bottom:1px solid #232c38; }
.bar h1 { font-size:16px; margin:0 14px 0 0; white-space:nowrap; }
button { background:#1f6feb; color:#fff; border:none; border-radius:8px;
  padding:8px 14px; font-size:13px; cursor:pointer; font-weight:600; }
button:hover { background:#2a7dff; }
button:disabled { background:#2a3340; color:#6b7888; cursor:not-allowed; }
label { font-size:13px; color:#9fb0c0; display:flex; align-items:center; gap:4px;
  cursor:pointer; user-select:none; }
.pill { padding:5px 12px; border-radius:16px; font-size:12px; font-weight:600; }
.pill.idle { background:#16341f; color:#5fd98a; }
.pill.run { background:#332c12; color:#e0c45a; }
.pill.phase { background:#1a2430; color:#7fb3d5; }
.pill.trading { background:#16341f; color:#5fd98a; }
#status { font-size:12px; color:#8b98a5; margin-left:auto; }
.sep { width:1px; height:22px; background:#2a3340; margin:0 4px; }
select { background:#1a2230; color:#cfe0f0; border:1px solid #2a3340;
  border-radius:6px; padding:6px 8px; font-size:12px; }
.ticker { display:flex; gap:18px; flex-wrap:wrap; align-items:center;
  padding:8px 18px; background:#0e141c; border-bottom:1px solid #1c2530;
  font-size:13px; font-variant-numeric:tabular-nums; color:#8b98a5; }
.ticker .q { white-space:nowrap; }
.ticker .nm { color:#cfe0f0; margin-right:6px; }
/* A股惯例：涨红跌绿 */
.ticker .up { color:#ff5b5b; font-weight:700; }
.ticker .down { color:#2ecc71; font-weight:700; }
.ticker .flat { color:#888; }
.ticker .ts { margin-left:auto; font-size:11px; color:#5a6875; }
iframe { width:100%; height:calc(100vh - 100px); border:none; background:#0f1419; }
</style></head>
<body>
<div class="bar">
  <h1>🦀 量化信号面板</h1>
  <button id="b1" onclick="scan('daily')">📊 日常盯盘</button>
  <button id="b2" onclick="scan('screener')">🔎 板块选股</button>
  <button id="b3" onclick="scan('both')">🚀 全部运行</button>
  <label><input type="checkbox" id="offline"> 离线验证</label>
  <label><input type="checkbox" id="push"> 推送钉钉</label>
  <span class="sep"></span>
  <label><input type="checkbox" id="auto"> 🔄 自动刷新</label>
  <select id="interval">
    <option value="60">每 1 分钟</option>
    <option value="180" selected>每 3 分钟</option>
    <option value="300">每 5 分钟</option>
    <option value="900">每 15 分钟</option>
  </select>
  <label><input type="checkbox" id="onlyTrading" checked> 仅盘中</label>
  <span id="pill" class="pill idle">🟢 空闲</span>
  <span id="phase" class="pill phase">—</span>
  <span id="status"></span>
</div>
<div id="ticker" class="ticker">实时报价加载中…</div>
<iframe id="board" src="/api/board"></iframe>
<script>
let lastFinished = null;
let lastMode = 'daily';        // 自动刷新沿用最近一次手动选择的模式
let nextAt = 0;                // 下次自动刷新的时间戳(ms)
let isRunning = false;
let isTrading = false;

function scan(mode){
  lastMode = mode;
  const offline = document.getElementById('offline').checked;
  const push = document.getElementById('push').checked;
  fetch('/api/scan',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode,offline,push})})
    .then(r=>r.json()).then(j=>setStatus(j.msg||'已启动'))
    .catch(e=>setStatus('🔴 启动失败，后台服务可能已退出：'+e));
}
function setStatus(t){ document.getElementById('status').textContent = t; }

/* ---------- 自动刷新调度：仅在开关打开且(未勾仅盘中 或 当前盘中)时触发 ---------- */
function autoTick(){
  const on = document.getElementById('auto').checked;
  const onlyTrading = document.getElementById('onlyTrading').checked;
  if(!on){ nextAt = 0; return; }
  if(onlyTrading && !isTrading){
    setStatus('🔄 自动刷新已开启，但当前非交易时段（勾掉「仅盘中」可强制刷新）');
    nextAt = 0; return;
  }
  const iv = parseInt(document.getElementById('interval').value,10)*1000;
  const now = Date.now();
  if(nextAt === 0){ nextAt = now + iv; return; }
  if(now >= nextAt && !isRunning){
    nextAt = now + iv;
    scan(lastMode);
  }
}
function countdownText(){
  const on = document.getElementById('auto').checked;
  if(!on || nextAt === 0) return '';
  const left = Math.max(0, Math.round((nextAt - Date.now())/1000));
  return ' ｜ 下次自动刷新 ' + left + 's';
}

/* ---------- 实时报价条：轻量接口，不跑引擎，盘中每 10s 刷新 ---------- */
function loadQuotes(){
  fetch('/api/quotes').then(r=>r.json()).then(j=>{
    const el = document.getElementById('ticker');
    if(!j.ok || !j.quotes || !j.quotes.length){
      el.innerHTML = '<span class="flat">实时报价不可用（数据源限流或非交易日）</span>';
      return;
    }
    el.innerHTML = j.quotes.map(q=>{
      const cls = q.pct>0 ? 'up' : (q.pct<0 ? 'down' : 'flat');
      const sign = q.pct>0 ? '+' : '';
      return '<span class="q"><span class="nm">'+q.name+'</span>'+
             q.price.toFixed(2)+' <span class="'+cls+'">'+sign+q.pct.toFixed(2)+'%</span></span>';
    }).join('') + '<span class="ts">报价 '+j.ts+'</span>';
  }).catch(()=>{
    document.getElementById('ticker').innerHTML =
      '<span class="flat">🔴 服务已断开，实时报价停止</span>';
  });
}
function poll(){
  fetch('/api/status').then(r=>r.json()).then(s=>{
    const pill = document.getElementById('pill');
    const dis = s.running;
    isRunning = !!s.running;
    isTrading = !!s.is_trading;
    ['b1','b2','b3'].forEach(id=>document.getElementById(id).disabled=dis);
    if(s.running){ pill.textContent='⏳ 扫描中…'; pill.className='pill run'; }
    else { pill.textContent='🟢 空闲'; pill.className='pill idle'; }
    const ph = document.getElementById('phase');
    ph.textContent = (s.is_trading?'🔔 ':'🕘 ') + (s.phase||'—') +
                     (s.server_time?(' '+s.server_time):'');
    ph.className = 'pill ' + (s.is_trading ? 'trading' : 'phase');
    if(s.error){ setStatus('❌ '+s.error); }
    else if(s.log && s.log.length){ setStatus(s.log[s.log.length-1] + countdownText()); }
    autoTick();
    if(!s.running && s.finished_at && s.finished_at!==lastFinished){
      lastFinished = s.finished_at;
      document.getElementById('board').src = '/api/board?t='+Date.now();
    }
  }).catch(()=>{
    // 服务已退出时必须明确告知，不能静默——否则用户点按钮完全不知道发生了什么
    const pill = document.getElementById('pill');
    pill.textContent='🔴 服务已断开'; pill.className='pill run';
    setStatus('后台服务未运行，请双击项目根目录的「启动看板.bat」重启');
  });
  setTimeout(poll, 2000);
}
window.onload = ()=>{
  poll();
  loadQuotes();
  setInterval(loadQuotes, 10000);   // 实时报价条每 10 秒刷新
  // 勾选自动刷新时立刻起算倒计时；取消则清零
  document.getElementById('auto').addEventListener('change', function(){
    nextAt = this.checked ? Date.now() +
      parseInt(document.getElementById('interval').value,10)*1000 : 0;
  });
  document.getElementById('interval').addEventListener('change', function(){
    if(document.getElementById('auto').checked){
      nextAt = Date.now() + parseInt(this.value,10)*1000;
    }
  });
};
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # 支持 keep-alive 与大文件(如 echarts.min.js)正确传输

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            # 客户端（浏览器）中断连接属正常情况，忽略以免刷屏
            pass

    def handle_one_request(self):
        # 吞掉请求读取阶段浏览器中断连接产生的异常 (WinError 10053/10054)，
        # 否则会冒泡到 socketserver 打印整段 traceback 刷屏。
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError,
                BrokenPipeError, OSError):
            self.close_connection = True

    def do_GET(self):
        p = urlparse(self.path)
        try:
            self._do_get(p)
        except Exception as e:  # noqa: BLE001
            # 任何未捕获异常都转为可读的 500 页面，避免 socketserver
            # 直接关连接导致浏览器只能报 "Remote end closed connection"。
            tb = traceback.format_exc()
            err_html = (
                "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
                "<style>body{background:#0b0f14;color:#ff6b6b;font-family:-apple-system,"
                "'Microsoft YaHei',sans-serif;padding:24px;line-height:1.6}"
                "pre{background:#161c24;padding:12px;border-radius:8px;overflow:auto;"
                "color:#e6e6e6;font-size:12px}</style></head><body>"
                "<h2>🔴 服务器处理请求时出错</h2>"
                "<p><b>路径：</b>" + escape(self.path) + "</p>"
                "<p><b>异常：</b>" + escape(str(e)) + "</p>"
                "<pre>" + escape(tb) + "</pre></body></html>"
            )
            self._send(500, err_html, "text/html; charset=utf-8")

    def _do_get(self, p):
        if p.path in ("/", "/index.html"):
            self._send(200, control_html(), "text/html; charset=utf-8")
        elif p.path == "/api/board":
            with _lock:
                html = _state["html"] or _placeholder_html(_state["progress"])
            self._send(200, html, "text/html; charset=utf-8")
        elif p.path == "/api/status":
            with _lock:
                d = {k: _state[k] for k in
                     ("running", "mode", "offline", "push",
                      "started_at", "finished_at", "error", "log", "progress")}
            try:
                phase, is_trading = market_phase()
            except Exception:  # noqa: BLE001
                phase, is_trading = "未知", False
            d["phase"] = phase
            d["is_trading"] = is_trading
            d["server_time"] = datetime.now().strftime("%H:%M:%S")
            self._send(200, json.dumps(d, ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p.path == "/api/quotes":
            # 轻量实时报价：不跑引擎，仅拉一次批量快照，供面板秒级刷新价格
            try:
                watch = load_watchlist()
                syms = [i["symbol"] for i in watch.get("watchlist", [])]
                q = fetch_realtime(syms)
                names = {i["symbol"]: i.get("name", "") for i in watch.get("watchlist", [])}
                out = []
                for s in syms:
                    v = q.get(s)
                    if not v:
                        continue
                    out.append({"symbol": s, "name": v.get("name") or names.get(s, ""),
                                "price": round(v["price"], 2),
                                "pct": round(v["pct"], 2)})
                self._send(200, json.dumps({"ok": True, "quotes": out,
                                            "ts": datetime.now().strftime("%H:%M:%S")},
                                           ensure_ascii=False),
                           "application/json; charset=utf-8")
            except Exception as e:  # noqa: BLE001
                self._send(200, json.dumps({"ok": False, "msg": str(e)},
                                           ensure_ascii=False),
                           "application/json; charset=utf-8")
        elif p.path == "/api/stock_detail":
            q = parse_qs(p.query)
            sym = _resolve_symbol(q.get("symbol", [""])[0])
            fmt = q.get("format", ["html"])[0]
            if not sym:
                self._send(200, json.dumps({"error": "缺少 symbol"}, ensure_ascii=False),
                           "application/json; charset=utf-8"); return
            d = _detail_cache_get(sym)
            if fmt == "json":
                try:
                    body = json.dumps(_safe_json(d), ensure_ascii=False)
                except Exception as e:  # noqa: BLE001
                    _log_err("stock_detail 序列化失败 sym=%s: %s" % (sym, e))
                    body = json.dumps({"error": "序列化失败: " + str(e)},
                                      ensure_ascii=False)
                self._send(200, body, "application/json; charset=utf-8")
            else:
                self._send(200, render_stock_detail(sym, d), "text/html; charset=utf-8")
        elif p.path == "/api/portfolio":
            q = parse_qs(p.query)
            fmt = q.get("format", ["html"])[0]
            book = sim_engine.summary()
            if fmt == "json":
                self._send(200, json.dumps({"book": book}, ensure_ascii=False, default=str),
                           "application/json; charset=utf-8")
            else:
                self._send(200, render_portfolio(book), "text/html; charset=utf-8")
        elif p.path == "/api/crypto_quotes":
            try:
                res = fetch_crypto_quotes()
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "msg": str(e), "quotes": []}
            res["ts"] = datetime.now().strftime("%H:%M:%S")
            self._send(200, json.dumps(res, ensure_ascii=False, default=str),
                       "application/json; charset=utf-8")
        elif p.path == "/api/recommend":
            # 优先读横截面相对排名荐股缓存（新版，相对沪深300多空）；旧版兜底
            cache_path = os.path.join(HERE, "xsec_recommend_cache.json")
            if os.path.exists(cache_path):
                try:
                    with open(cache_path, "r", encoding="utf-8") as _f:
                        d = json.load(_f)
                    d["exists"] = True
                    d["version"] = "xsec"
                    self._send(200, json.dumps(d, ensure_ascii=False, default=str),
                               "application/json; charset=utf-8")
                    return
                except Exception:  # noqa: BLE001
                    pass
            old = os.path.join(HERE, "recommend_cache.json")
            if os.path.exists(old):
                try:
                    with open(old, "r", encoding="utf-8") as _f:
                        d = json.load(_f)
                    d["exists"] = True
                    d["version"] = "legacy"
                    self._send(200, json.dumps(d, ensure_ascii=False, default=str),
                               "application/json; charset=utf-8")
                    return
                except Exception:  # noqa: BLE001
                    pass
            self._send(200, json.dumps({"exists": False}, ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p.path.startswith("/api/backtest"):
            # 历史回测接口：不触发钉钉推送，仅供面板复盘展示。
            q = parse_qs(p.query)
            sym = _resolve_symbol(q.get("symbol", [""])[0]) or "300034"
            try:
                stop = float(q.get("stop", ["0"])[0])
            except Exception:  # noqa: BLE001
                stop = 0.0
            try:
                max_hold = int(q.get("max_hold", ["0"])[0])
            except Exception:  # noqa: BLE001
                max_hold = 0
            sweep = q.get("sweep", ["0"])[0] in ("1", "true", "True")
            try:
                days = int(q.get("days", ["500"])[0])
            except Exception:  # noqa: BLE001
                days = 500
            use_engine = q.get("use_engine", ["1"])[0] not in ("0", "false")
            try:
                import backtest_engine as bt
                if sweep:
                    stops = [0.05, 0.08, 0.12]
                    max_holds = [20, 30, 60]
                    grid = bt.parameter_sweep(sym, days=days, stops=stops,
                                              max_holds=max_holds,
                                              use_engine=use_engine)
                    self._send(200, json.dumps(
                        {"symbol": sym, "sweep": True, "stops": stops,
                         "max_holds": max_holds, "grid": _safe_json(grid)},
                        ensure_ascii=False), "application/json; charset=utf-8")
                else:
                    r = bt.backtest_symbol(sym, days=days, use_engine=use_engine,
                                           stop=stop, max_hold=max_hold)
                    out = {"symbol": sym, "stop": stop, "max_hold": max_hold,
                           "sweep": False}
                    if "error" in r:
                        out["error"] = r["error"]
                    else:
                        out["result"] = _safe_json(r)
                    self._send(200, json.dumps(out, ensure_ascii=False, default=str),
                               "application/json; charset=utf-8")
            except Exception as e:  # noqa: BLE001
                self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False),
                           "application/json; charset=utf-8")
        elif p.path == "/static/echarts.min.js":
            ep = os.path.join(HERE, "static", "echarts.min.js")
            if os.path.exists(ep):
                with open(ep, "rb") as _f:
                    self._send(200, _f.read(), "application/javascript; charset=utf-8")
            else:
                self._send(404, "not found", "text/plain")
        else:
            self._send(204, b"", "")

    def do_POST(self):
        p = urlparse(self.path)
        if p.path == "/api/scan":
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                data = {}
            mode = data.get("mode", "daily")
            offline = bool(data.get("offline", False))
            push = bool(data.get("push", False))
            sectors = data.get("sectors") or None
            if mode not in ("daily", "screener", "both"):
                mode = "daily"
            with _lock:
                running = _state["running"]
            if running:
                self._send(200, json.dumps({"ok": False, "msg": "正在运行中，请稍候"}),
                           "application/json; charset=utf-8")
                return
            t = threading.Thread(target=_run_scan,
                                 args=(mode, offline, push, sectors),
                                 daemon=True)
            t.start()
            self._send(200, json.dumps({"ok": True, "msg": "已启动扫描"}),
                       "application/json; charset=utf-8")
        elif p.path == "/api/trade":
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                data = {}
            symbol = str(data.get("symbol", ""))
            name = str(data.get("name", ""))
            side = str(data.get("side", "buy"))
            try:
                price = float(data.get("price", 0))
                qty = int(data.get("qty", 0))
            except Exception:
                price = qty = 0
            if side == "sell":
                res = sim_engine.sell(symbol, price, qty)
            else:
                res = sim_engine.buy(symbol, name, price, qty)
            self._send(200, json.dumps(
                {"ok": res.get("ok"), "msg": res.get("msg"), "book": res.get("book")},
                ensure_ascii=False, default=str), "application/json; charset=utf-8")
        elif p.path == "/api/watchlist":
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                data = {}
            action = data.get("action", "add")
            symbol = str(data.get("symbol", "")).strip()
            name = str(data.get("name", "")).strip()
            if not symbol:
                self._send(200, json.dumps({"ok": False, "msg": "缺少 symbol"},
                                           ensure_ascii=False),
                           "application/json; charset=utf-8"); return
            wl = load_watchlist()
            lst = wl.setdefault("watchlist", [])
            if action == "remove":
                newlst = [x for x in lst if x.get("symbol") != symbol]
                removed = len(lst) != len(newlst)
                wl["watchlist"] = newlst
                _save_watchlist(wl)
                self._send(200, json.dumps({"ok": removed,
                                           "msg": ("已删除 " + symbol) if removed
                                           else ("未找到 " + symbol)},
                                           ensure_ascii=False),
                           "application/json; charset=utf-8"); return
            # add
            if any(x.get("symbol") == symbol for x in lst):
                self._send(200, json.dumps({"ok": True, "msg": "已在自选股中"},
                                           ensure_ascii=False),
                           "application/json; charset=utf-8"); return
            lst.append({"symbol": symbol, "name": name})
            wl["watchlist"] = lst
            _save_watchlist(wl)
            self._send(200, json.dumps({"ok": True, "msg": "已加入自选股 " + symbol},
                                       ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p.path == "/api/recommend/refresh":
            # 后台触发横截面相对排名荐股（相对沪深300多空版），结果写入 xsec_recommend_cache.json
            try:
                import subprocess
                py = sys.executable
                script = os.path.join(HERE, "ml_model.py")
                subprocess.Popen([py, script, "--xsec-recommend", "--money", "real",
                                  "--xsec-label", "rel_hs300", "--xsec-top-n", "12",
                                  "--horizon", "10", "--model", "LR"],
                                 cwd=HERE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                self._send(200, json.dumps(
                    {"ok": True, "msg": "已启动后台横截面荐股重算（约3-5分钟，扫39只）"},
                    ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:  # noqa: BLE001
                self._send(200, json.dumps({"ok": False, "msg": str(e)},
                                           ensure_ascii=False),
                           "application/json; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"🦀 量化信号面板已启动： {url}")
    print("   浏览器将自动打开；若未打开请手动访问上面的地址。点按钮即可运行扫描（无需命令行）。Ctrl+C 退出。")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()

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
import socket
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
                       fetch_crypto_quotes, fetch_kline, DataSourceError,
                       fetch_crypto_kline, fetch_crypto_ticker24,
                       compute_crypto_indicators, crypto_forecast,
                       load_crypto_watchlist, save_crypto_watchlist)
from polymarket import fetch_polymarket_odds


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
import arb_book
from dashboard import (render_dashboard, render_stock_detail, render_portfolio)
import kalshi
import polymarket
import arbitrage

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
  gap:8px; flex-wrap:wrap; padding:10px 16px;
  background:linear-gradient(180deg,#15202e,#10161f);
  border-bottom:1px solid #243042; box-shadow:0 2px 14px rgba(0,0,0,.4); }
.bar h1 { font-size:15px; margin:0 10px 0 0; white-space:nowrap; }
button { background:linear-gradient(180deg,#3b82f6,#2563eb); color:#fff; border:none;
  border-radius:8px; padding:7px 13px; font-size:13px; cursor:pointer; font-weight:600;
  transition:filter .15s,transform .05s; }
button:hover { filter:brightness(1.12); }
button:active { transform:translateY(1px); }
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
.ticker.crypto { background:#0c1118; flex-wrap:nowrap; overflow-x:auto; gap:14px;
  padding:6px 16px; font-size:12px; scrollbar-width:thin; }
.ticker.crypto .q { font-size:12px; }
.ticker.crypto .nm { font-size:12px; }
.ticker.crypto::-webkit-scrollbar{height:6px;}
.ticker.crypto::-webkit-scrollbar-thumb{background:#2c3a4e;border-radius:3px;}
.cpblock { margin:10px 14px 0; padding:11px 14px; background:#131a24;
  border:1px solid #243042; border-radius:12px; box-shadow:0 8px 30px rgba(0,0,0,.4); }
.cpHead { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-bottom:8px;
  font-size:13px; color:#9fb0c0; }
.cpHead .ttl { font-size:15px; color:#cfe0f0; }
.watchEditor { margin:8px 0; padding:8px 10px; background:#0f1620;
  border:1px solid #232c38; border-radius:8px; }
.chips { display:flex; gap:6px; flex-wrap:wrap; margin-top:4px; }
.chip { background:#1a2230; border:1px solid #2a3340; border-radius:12px;
  padding:2px 9px; font-size:12px; color:#cfe0f0; }
.chart { width:100%; height:440px; }
.ticker .q { white-space:nowrap; }
.ticker .nm { color:#cfe0f0; margin-right:6px; }
.polyList { margin-top:8px; display:grid;
  grid-template-columns:repeat(auto-fill,minmax(290px,1fr)); gap:12px;
  max-height:62vh; overflow:auto; padding:2px 4px 6px; }
.polyRow { background:#161f2b; border:1px solid #243042; border-radius:10px;
  padding:12px 13px; display:flex; flex-direction:column; gap:8px;
  transition:border-color .15s,background .15s; }
.polyRow:hover { border-color:#2c3a4e; background:#1a2433; }
.cryptoGrid { margin-top:8px; display:grid;
  grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:12px;
  max-height:64vh; overflow:auto; padding:2px 4px 6px; }
.coinCard { background:#161f2b; border:1px solid #243042; border-radius:10px;
  padding:12px 13px; display:flex; flex-direction:column; gap:7px;
  transition:border-color .15s,background .15s; }
.coinCard:hover { border-color:#2c3a4e; background:#1a2433; }
.coinCard .sym { font-size:15px; font-weight:700; color:#cfe0f0; }
.coinCard .px { font-size:18px; font-weight:700; font-variant-numeric:tabular-nums; }
.coinCard .meta { font-size:11px; color:#7e8da0; }
.sigPill { padding:2px 8px; border-radius:10px; font-size:11px; font-weight:700; }
.sigPill.buy { background:#16341f; color:#5fd98a; }
.sigPill.sell { background:#3a1c18; color:#ef7a66; }
.sigPill.hold { background:#332c12; color:#e0c45a; }
.polyRow .q { font-size:13px; color:#dbe6f0; line-height:1.45; }
.polyRow .meta { font-size:11px; color:#7e8da0; margin-top:3px; }
.probBar { height:8px; background:#0f1620; border-radius:5px; margin-top:2px; overflow:hidden;
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.05); }
.probBar > i { display:block; height:100%; border-radius:4px; }
.ticker .up { color:#ff5b5b; font-weight:700; }
.ticker .down { color:#2ecc71; font-weight:700; }
.ticker .flat { color:#888; }
.ticker .ts { margin-left:auto; font-size:11px; color:#5a6875; }
iframe { width:100%; height:calc(100vh - 132px); border:none; background:#0f1419; }
.panel { display:none; position:absolute; top:54px; left:16px; z-index:30;
  background:#131a24; border:1px solid #2c3a4e; border-radius:12px; padding:10px 12px;
  max-width:520px; box-shadow:0 10px 36px rgba(0,0,0,.55); }
.panel .sec { display:inline-flex; margin:3px 8px 3px 0; padding:3px 8px;
  background:#0f1620; border:1px solid #232c38; border-radius:14px; font-size:12px; }
.mask { display:none; position:fixed; inset:0; z-index:50;
  background:rgba(0,0,0,.6); align-items:flex-start; justify-content:center; }
.mask.show { display:flex; }
.mbox { background:#131a24; color:#e6e6e6; font-size:14px; margin-top:48px; max-width:1080px; width:96%;
  max-height:90vh; overflow:auto; border:1px solid #2c3a4e; border-radius:14px;
  padding:22px 26px; box-shadow:0 12px 40px rgba(0,0,0,.6); }
.mbox h2 { margin:0 0 10px; font-size:22px; }
.mbox .sub { color:#9fb0c0; font-size:13px; margin-bottom:10px; }
.mbox .close { float:right; background:#2a3340; color:#cfe0f0; border:none;
  border-radius:6px; padding:4px 10px; cursor:pointer; font-size:12px; }
.chart { width:100%; height:300px; margin:8px 0 4px; }
.kpi { display:grid; grid-template-columns:repeat(auto-fill,minmax(120px,1fr)); gap:8px; margin:10px 0; }
.kpi .cell { background:#141b24; border:1px solid #1f2937; border-radius:8px; padding:8px 10px; }
.kpi .k { color:#7f8ea0; font-size:12px; }
.kpi .v { font-size:18px; font-weight:700; margin-top:2px; }
.kpi .v.up { color:#ff5b5b; } .kpi .v.down { color:#2ecc71; } .kpi .v.flat { color:#888; }
.tag-real { color:#5fd98a; background:#15301f; padding:2px 8px; border-radius:10px; font-size:12px; }
.tag-syn { color:#e0a45a; background:#332712; padding:2px 8px; border-radius:10px; font-size:12px; }
.polyList::-webkit-scrollbar,.mbox::-webkit-scrollbar,.panel::-webkit-scrollbar,.cryptoGrid::-webkit-scrollbar{width:8px;height:8px;}
.polyList::-webkit-scrollbar-thumb,.mbox::-webkit-scrollbar-thumb,.panel::-webkit-scrollbar-thumb,.cryptoGrid::-webkit-scrollbar-thumb{background:#2c3a4e;border-radius:4px;}
.polyList::-webkit-scrollbar-track,.mbox::-webkit-scrollbar-track,.panel::-webkit-scrollbar-track,.cryptoGrid::-webkit-scrollbar-track{background:transparent;}
/* 分区收起/展开 */
.collapseBtn{margin-left:auto;}
.secHead{display:flex;align-items:center;gap:10px;margin:12px 14px 0;padding:10px 14px;background:#131a24;border:1px solid #243042;border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.4);cursor:pointer;user-select:none;}
.secHead:hover{border-color:#2c3a4e;}
.secHead .ttl{font-size:15px;color:#cfe0f0;font-weight:700;}
.secHead .caret{margin-left:auto;color:#9fb0c0;font-size:13px;}
/* 加密详情弹窗：竖向加大、横向收窄（高瘦比例，整体放大） */
.mboxCrypto{max-width:820px;width:94%;max-height:96vh;}
.mboxCrypto .chart{height:600px;}
/* 模拟套利面板 */
.arbTable{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px;}
.arbTable th,.arbTable td{padding:7px 9px;border-bottom:1px solid #22303f;text-align:left;vertical-align:middle;}
.arbTable th{color:#8fa3b5;font-weight:600;font-size:12px;background:#161e29;}
.arbTable tr:hover td{background:#172230;}
.arbEdge{color:#5fd38a;font-weight:700;}
.arbDemo{color:#e0a45a;font-size:11px;padding:1px 6px;border:1px solid #4a3a1a;border-radius:8px;margin-left:6px;}
.arbBtn{padding:4px 10px;border-radius:8px;border:1px solid #2c6e49;background:#16331f;color:#9be8b0;cursor:pointer;font-size:12px;}
.arbBtn:hover{background:#1d4a2c;}
.arbBtn:disabled{opacity:.4;cursor:not-allowed;}
.arbSec{margin-top:12px;padding-top:10px;border-top:1px solid #22303f;}
.arbSummary{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;margin:6px 0 8px;}
.arbSummary b{color:#cfe0f0;font-size:16px;}
.arbNote{color:#e0a45a;font-size:12px;margin-top:6px;}
.arbBookRow{display:flex;gap:10px;align-items:center;padding:5px 8px;border-bottom:1px solid #1c2733;font-size:12px;}
.arbBookRow .pid{color:#7f93a5;width:42px;}
.arbBookRow .qn{color:#cfe0f0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.arbInvList{margin-top:6px;}
.arbInvRow{display:flex;gap:12px;align-items:center;padding:5px 8px;border-bottom:1px solid #1c2733;font-size:12px;}
.arbInvRow .invMkt{color:#cfe0f0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.arbInvRow .invNet{color:#9fb3c4;width:96px;}
.arbInvRow .invSkew{width:84px;}
.arbInvRow .invBar{flex:0 0 160px;height:8px;background:#16202c;border-radius:5px;overflow:hidden;}
.arbInvRow .invBarFill{display:block;height:100%;border-radius:5px;}
.arbLiq{color:#9fb3c4;text-align:right;}
.arbEv{margin:8px 0;padding:8px 10px;border:1px solid #2a3a4d;border-left:3px solid #c9a227;border-radius:6px;background:#0e1620;}
.arbEvH{font-size:13px;color:#e8eef5;margin-bottom:5px;}
.arbWarn{color:#e0b341;font-size:11px;font-weight:600;background:#2a230d;padding:1px 6px;border-radius:4px;}
.arbEvSubs{display:flex;flex-direction:column;gap:3px;}
.arbSub{font-size:11px;color:#9fb3c8;}
.arbSizeInput{width:54px;padding:2px 4px;background:#0e141c;border:1px solid #2c3a4e;color:#e6e6e6;border-radius:6px;}
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
  <button class="ghost" onclick="toggleCryptoPanel()">🪙 加密行情</button>
  <button class="ghost" onclick="togglePolyPanel()">📊 事件概率</button>
  <button class="ghost" onclick="toggleArbPanel()">🧪 模拟套利</button>
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
<div id="cryptoPanel" class="cpblock" style="display:none">
  <div class="cpHead">
    <span class="ttl">🪙 加密货币 · 自选快照</span>
    <button onclick="loadCryptoGrid()">刷新</button>
    <label><input type="checkbox" id="cryptoAuto" checked> 自动刷新</label>
    <button class="ghost" onclick="toggleWatchEditor()">✎ 编辑自选</button>
    <span class="sub" id="cpTs"></span>
    <button class="ghost collapseBtn" id="cryptoPanelBtn" onclick="toggleCollapse('cryptoPanel')">▴ 收起</button>
  </div>
  <div class="cpBody" id="cryptoPanelBody">
  <div id="watchEditor" style="display:none" class="watchEditor">
    <div class="sub">点击 × 移除，或输入交易对（如 SOL/USDT）后回车 / 点添加：</div>
    <div id="watchChips" class="chips"></div>
    <div style="margin-top:6px">
      <input id="watchInput" placeholder="XXX/USDT" style="width:120px" onkeydown="if(event.key==='Enter')addWatch()">
      <button onclick="addWatch()">添加</button>
      <span id="watchMsg" class="sub"></span>
    </div>
  </div>
  <div id="cryptoGrid" class="cryptoGrid"></div>
  </div>
</div>
<div id="polyPanel" class="cpblock" style="display:none">
  <div class="cpHead">
    <span class="ttl">📊 事件概率 · <b id="polyTag">crypto</b></span>
    <span>类别 <select id="polyTagSel" onchange="polyState.tag=this.value;document.getElementById('polyTag').textContent=this.value;loadPoly()">
      <option value="crypto">加密</option><option value="economy">经济</option><option value="finance">金融</option><option value="business">商业</option><option value="tech">科技</option><option value="science">科学</option><option value="sports">体育</option><option value="entertainment">娱乐</option>
    </select></span>
    <button onclick="loadPoly()">刷新</button>
    <label><input type="checkbox" id="polyAuto" checked> 自动刷新</label>
    <span class="sub" id="polyTs"></span>
    <button class="ghost collapseBtn" id="polyPanelBtn" onclick="toggleCollapse('polyPanel')">▴ 收起</button>
  </div>
  <div class="cpBody" id="polyPanelBody">
  <div class="sub">来源：Polymarket 公开行情（Gamma API，无需密钥）｜ 价格为市场隐含概率 ｜ <b>已过滤政治/地缘等敏感类别</b></div>
  <div id="polyList" class="polyList"></div>
  </div>
</div>
<div id="arbPanel" class="cpblock" style="display:none">
  <div class="cpHead">
    <span class="ttl">🧪 模拟套利 · Polymarket 单源</span>
    <button onclick="loadArb()">刷新扫描</button>
    <label><input type="checkbox" id="arbAuto" checked> 自动刷新</label>
    <button class="ghost" onclick="loadArbDemo()">载入演示对</button>
    <button class="ghost" onclick="autoMM()">🔄 自动轮动做市</button>
    <input id="autoMMN" value="5" title="轮动笔数" style="width:44px;background:#0e1620;color:#cfe0f0;border:1px solid #2a3a4a;border-radius:6px;padding:3px 6px">
    <span class="sub" id="arbTs"></span>
    <button class="ghost collapseBtn" id="arbPanelBtn" onclick="toggleCollapse('arbPanel')">▴ 收起</button>
  </div>
  <div class="cpBody" id="arbPanelBody">
    <div class="sub">Polymarket 单源模拟（链上、非美可正常访问）：单边做市吃价差 + 同事件互斥套利扫描。只读公开盘口，虚拟本金模拟成交，不碰真实资金。Kalshi 因美国身份/IP 限制不可得，已转单源深化。</div>
    <div id="arbCfg" style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:10px 0;padding:10px 12px;background:#0e1620;border:1px solid #2a3a4a;border-radius:8px">
      <span style="color:#9fb0c0;font-size:13px">⚙️ 策略设置</span>
      <label style="color:#cfe0f0;font-size:13px">偏斜上限
        <input id="arbSkewInput" value="300" title="单市场最大净库存(份额)，10~5000" style="width:64px;background:#0e1620;color:#cfe0f0;border:1px solid #2a3a4a;border-radius:6px;padding:3px 6px">
      </label>
      <button class="ghost" onclick="setSkew()">设置</button>
      <label style="color:#cfe0f0;font-size:13px">手续费率%
        <input id="arbFeeInput" value="1.0" title="单边撮合手续费(百分比), 0~10" style="width:54px;background:#0e1620;color:#cfe0f0;border:1px solid #2a3a4a;border-radius:6px;padding:3px 6px">
      </label>
      <button class="ghost" onclick="setFee()">设置费率</button>
      <label style="color:#cfe0f0;font-size:13px"><input type="checkbox" id="arbSkipSkewed" checked> 轮动跳过高偏斜市场</label>
      <label style="color:#cfe0f0;font-size:13px"><input type="checkbox" id="arbAutoReb"> 轮动后自动再平衡</label>
      <label style="color:#cfe0f0;font-size:13px">最小流动性
        <input id="arbMinLiq" value="0" title="仅对流动性≥此值的市场轮动" style="width:70px;background:#0e1620;color:#cfe0f0;border:1px solid #2a3a4a;border-radius:6px;padding:3px 6px">
      </label>
      <label style="color:#cfe0f0;font-size:13px">轮动份额
        <input id="arbRotSize" value="0" title="0=用默认份额(size_hint)" style="width:54px;background:#0e1620;color:#cfe0f0;border:1px solid #2a3a4a;border-radius:6px;padding:3px 6px">
      </label>
      <button class="ghost" style="color:#ef7a66;border-color:#5a2a2a" onclick="resetArb()">🗑 重置账本</button>
    </div>
    <div id="arbSummary" class="arbSummary"></div>
    <div id="arbList"></div>
    <div class="arbSec" style="margin-top:14px">
      <div class="ttl" style="color:#cfe0f0;font-size:14px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">📊 库存偏斜控制（单市场净库存 / 上限 <span id="arbMaxSkew">300</span>）<button class="ghost" onclick="rebalanceArb()">⚖️ 再平衡·对冲平仓</button></div>
      <div id="arbInv"></div>
    </div>
    <div class="arbSec" style="margin-top:14px">
      <div class="ttl" style="color:#cfe0f0;font-size:14px">📒 持仓 / 盈亏（虚拟）</div>
      <div id="arbBook"></div>
    </div>
    <div class="arbSec" style="margin-top:14px">
      <div class="ttl" style="color:#cfe0f0;font-size:14px">📈 历史回测（验证策略历史表现，只读公开历史价格）</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0">
        <label style="color:#cfe0f0;font-size:13px">市场
          <select id="btMarket" style="max-width:320px;background:#0e1620;color:#cfe0f0;border:1px solid #2a3a4a;border-radius:6px;padding:3px 6px"></select>
        </label>
        <label style="color:#cfe0f0;font-size:13px">回看天数
          <input id="btDays" value="30" style="width:50px;background:#0e1620;color:#cfe0f0;border:1px solid #2a3a4a;border-radius:6px;padding:3px 6px">
        </label>
        <label style="color:#cfe0f0;font-size:13px">频率(分钟)
          <input id="btEvery" value="1440" title="每多少分钟做一次成对做市" style="width:60px;background:#0e1620;color:#cfe0f0;border:1px solid #2a3a4a;border-radius:6px;padding:3px 6px">
        </label>
        <label style="color:#cfe0f0;font-size:13px">份额
          <input id="btSize" value="100" style="width:54px;background:#0e1620;color:#cfe0f0;border:1px solid #2a3a4a;border-radius:6px;padding:3px 6px">
        </label>
        <button class="ghost" onclick="runBacktest()">📈 运行回测</button>
      </div>
      <div id="btResult"></div>
    </div>
  </div>
</div>
<div id="ticker" class="ticker">实时报价加载中…</div>
<div class="secHead" id="boardHead" onclick="toggleCollapse('boardSec')">
  <span class="ttl">📋 日常盯盘</span>
  <span class="caret" id="boardSecCaret">▴ 收起</span>
</div>
<div class="cpBody" id="boardSecBody">
<iframe id="board" src="/api/board"></iframe>
</div>
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
      return '<span class="q" style="cursor:pointer" onclick="selectCrypto(\''+q.symbol+'\')"><span class="nm">'+q.symbol+'</span>'+px+' <span class="'+cls+'">'+sg+pc+'%</span></span>';
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
function toggleCollapse(id){
  const body=document.getElementById(id+'Body');
  if(!body)return;
  const collapsed=body.style.display==='none';
  body.style.display=collapsed?'block':'none';
  const txt=collapsed?'▴ 收起':'▾ 展开';
  const btn=document.getElementById(id+'Btn');
  const caret=document.getElementById(id+'Caret');
  if(btn)btn.textContent=txt;
  if(caret)caret.textContent=txt;
}
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
function openMain(h,cls){const b=document.getElementById('mainModalBox');b.className=cls||'mbox';b.innerHTML=h;document.getElementById('mainModalMask').classList.add('show');}
let cryptoState={symbol:'BTC/USDT',timeframe:'1h'};
let cryptoAutoTimer=null, cryptoPanelOpen=false;
function toggleCryptoPanel(){
  cryptoPanelOpen=!cryptoPanelOpen;
  const p=document.getElementById('cryptoPanel');
  p.style.display=cryptoPanelOpen?'block':'none';
  if(cryptoPanelOpen){
    loadCryptoWatchlist();
    loadCryptoGrid();
    if(cryptoAutoTimer)clearInterval(cryptoAutoTimer);
    cryptoAutoTimer=setInterval(()=>{
      if(cryptoPanelOpen && document.getElementById('cryptoAuto').checked)loadCryptoGrid();
    },30000);
  }else if(cryptoAutoTimer){clearInterval(cryptoAutoTimer);cryptoAutoTimer=null;}
}
function loadCryptoWatchlist(){
  fetch('/api/crypto_watchlist').then(r=>r.json()).then(j=>{
    if(!j.ok||!j.symbols)return;
    const syms=j.symbols;
    if(syms.indexOf(cryptoState.symbol)<0)cryptoState.symbol=syms[0];
    renderWatchChips(syms);
    loadCryptoGrid();
  }).catch(()=>{});
}
function renderWatchChips(syms){
  document.getElementById('watchChips').innerHTML=syms.map(s=>'<span class="chip">'+s+' <span style="cursor:pointer;color:#ef7a66" onclick="removeWatch(\''+s+'\')">×</span></span>').join('');
}
function toggleWatchEditor(){
  const e=document.getElementById('watchEditor');
  e.style.display = e.style.display==='none'?'block':'none';
}
function addWatch(){
  const inp=document.getElementById('watchInput');
  const sym=inp.value.trim().toUpperCase();
  if(!sym)return;
  fetch('/api/crypto_watchlist',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'add',symbol:sym})}).then(r=>r.json()).then(j=>{
    document.getElementById('watchMsg').textContent=j.msg||'';
    if(j.ok){inp.value='';renderWatchChips(j.symbols);loadCryptoWatchlist();loadCryptoGrid();loadCrypto();}
  }).catch(e=>{document.getElementById('watchMsg').textContent='失败：'+e;});
}
function removeWatch(sym){
  fetch('/api/crypto_watchlist',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'remove',symbol:sym})}).then(r=>r.json()).then(j=>{
    if(j.ok){renderWatchChips(j.symbols);loadCryptoWatchlist();loadCryptoGrid();loadCrypto();}
  }).catch(()=>{});
}
function selectCrypto(sym){ openCryptoDetail(sym); }
function loadCryptoGrid(){
  const el=document.getElementById('cryptoGrid');
  if(el)el.innerHTML='<div class="sub" style="padding:16px;color:#9fb0c0">加载中…（首次计算指标约需数秒）</div>';
  const url='/api/crypto_grid?_='+Date.now();
  fetch(url).then(r=>r.json()).then(renderCryptoGrid).catch(e=>{
    if(el)el.innerHTML='<div class="sub" style="color:#ef7a66">加载失败：'+e+'</div>';
  });
}
function renderCryptoGrid(d){
  const tsEl=document.getElementById('cpTs');
  if(tsEl)tsEl.textContent=d.ts?('更新 '+d.ts+(d.cached?'（缓存）':'')):'';
  const el=document.getElementById('cryptoGrid');
  if(!el)return;
  if(!d.ok||!d.coins||!d.coins.length){
    el.innerHTML='<div class="sub" style="color:#ef7a66">'+(d.msg||'暂无数据')+'</div>';return;
  }
  const emaMap={'golden':'金叉🟢','dead':'死叉🔴','bullish':'多头🟢','bearish':'空头🔴','neutral':'中性'};
  el.innerHTML=d.coins.map(c=>{
    const cls=(c.pct||0)>0?'up':((c.pct||0)<0?'down':'flat');const sg=(c.pct||0)>0?'+':'';
    const sigCls=c.signal==='buy'?'buy':(c.signal==='sell'?'sell':'hold');
    const sigTxt=c.signal==='buy'?'买入':(c.signal==='sell'?'卖出':'持有');
    const emaTxt=emaMap[c.ema_state]||'-';
    const sym=c.symbol.replace(/'/g,"\\'");
    return '<div class="coinCard">'
      +'<div class="sym">'+escapeHtml(c.symbol)
      +' <span class="sigPill '+sigCls+'">'+sigTxt+'</span></div>'
      +'<div class="px '+cls+'">'+(c.price!=null?c.price.toFixed(2):'-')+'</div>'
      +'<div class="meta">24h '+(c.pct!=null?sg+c.pct.toFixed(2)+'%':'-')
      +' ｜ RSI '+(c.rsi14!=null?c.rsi14.toFixed(0):'-')
      +' ｜ '+emaTxt+'</div>'
      +'<button class="ghost" style="align-self:flex-start" onclick="openCryptoDetail(\''+sym+'\')">📈 详情</button>'
      +'</div>';
  }).join('');
}
function openCryptoDetail(sym){
  const tf='1h';
  const url='/api/crypto_detail?symbol='+encodeURIComponent(sym)+'&timeframe='+tf+'&limit=200';
  fetch(url).then(r=>r.json()).then(d=>{
    if(!d.ok){ openMain('<button class="close" onclick="closeMain()">关闭</button><h2>'+escapeHtml(sym)+'</h2><div class="sub" style="color:#ef7a66">'+(d.msg||'获取失败')+'</div>','mbox mboxCrypto'); return; }
    const t=d.ticker||{}, ind=d.indicators||{}, fc=d.forecast||{};
    const cls=(t.pct||0)>0?'up':((t.pct||0)<0?'down':'flat');const sg=(t.pct||0)>0?'+':'';
    let kpi='<div class="kpi">';
    kpi+='<div class="cell"><div class="k">现价</div><div class="v '+cls+'">'+(t.price!=null?t.price.toFixed(2):'-')+'</div></div>';
    kpi+='<div class="cell"><div class="k">24h涨跌</div><div class="v '+cls+'">'+(t.pct!=null?sg+t.pct.toFixed(2)+'%':'-')+'</div></div>';
    kpi+='<div class="cell"><div class="k">24h高</div><div class="v">'+(t.high!=null?t.high.toFixed(2):'-')+'</div></div>';
    kpi+='<div class="cell"><div class="k">24h低</div><div class="v">'+(t.low!=null?t.low.toFixed(2):'-')+'</div></div>';
    kpi+='<div class="cell"><div class="k">24h额(万$)</div><div class="v">'+(t.quote_volume!=null?(t.quote_volume/1e4).toFixed(1):'-')+'</div></div>';
    kpi+='<div class="cell"><div class="k">RSI(14)</div><div class="v">'+((ind.rsi14!=null)?ind.rsi14.toFixed(1):'-')+'</div></div>';
    kpi+='</div>';
    const html='<button class="close" onclick="closeMain()">关闭</button>'
      +'<h2>🪙 '+escapeHtml(sym)+' · 短期研判（'+tf+'）</h2>'
      +'<div class="sub">信号由量化引擎生成，仅供研究参考，不构成投资建议</div>'
      +kpi+'<div id="cryptoModalChart" class="chart"></div>'
      +'<div class="sub" style="margin-top:6px">图上蓝色阴影带=下一周期1σ统计区间；加密资产波动剧烈，风险自担。</div>';
    openMain(html,'mbox mboxCrypto');
    try{
      const chart=echarts.init(document.getElementById('cryptoModalChart'));
      const dl=d.kline.map(x=>x.date);
      const ohlc=d.kline.map(x=>[x.open,x.close,x.low,x.high]);
      const vol=d.kline.map(x=>x.volume);
      const nT=(fc.next_time||'');const hasBand=nT&&fc.range_low!=null&&fc.range_high!=null;
      if(hasBand){dl.push(nT);ohlc.push(['-','-','-','-']);vol.push('-');}
      const bandArea=hasBand?{markArea:{silent:true,itemStyle:{color:'rgba(120,160,255,0.12)'},data:[[{xAxis:d.kline[d.kline.length-1].date,yAxis:fc.range_low},{xAxis:nT,yAxis:fc.range_high}]]}}:{};
      const midLine=hasBand?{markLine:{silent:true,symbol:'none',lineStyle:{type:'dash',color:'#7aa0ff'},data:[{xAxis:d.kline[d.kline.length-1].date,yAxis:fc.range_mid},{xAxis:nT,yAxis:fc.range_mid}]}}:{};
      chart.setOption({backgroundColor:'#0d1219',animation:false,
        grid:[{left:60,right:18,top:16,bottom:62,height:'60%'},{left:60,right:18,top:'76%',height:'16%'}],
        tooltip:{trigger:'axis'},
        xAxis:[{type:'category',data:dl,axisLabel:{color:'#8b98a5',fontSize:12}},{type:'category',gridIndex:1,data:dl,axisLabel:{show:false}}],
        yAxis:[{scale:true,axisLabel:{color:'#8b98a5'}},{scale:true,gridIndex:1,axisLabel:{show:false}}],
        dataZoom:[{type:'inside',xAxisIndex:[0,1]},{type:'slider',height:14,bottom:6,xAxisIndex:[0,1]}],
        series:[Object.assign({type:'candlestick',data:ohlc,itemStyle:{color:'#ff5b5b',color0:'#2ecc71',borderColor:'#ff5b5b',borderColor0:'#2ecc71'}},bandArea,midLine),
                {type:'bar',xAxisIndex:1,yAxisIndex:1,data:vol,itemStyle:{color:'#3a4a5a'}}]});
      window.addEventListener('resize',()=>chart.resize());
    }catch(e){}
  }).catch(e=>{ openMain('<button class="close" onclick="closeMain()">关闭</button><h2>加载失败</h2><div class="sub" style="color:#ef7a66">'+e+'</div>','mbox mboxCrypto'); });
}
function loadCryptoDetail(){
  const url='/api/crypto_detail?symbol='+encodeURIComponent(cryptoState.symbol)+'&timeframe='+cryptoState.timeframe+'&limit=200';
  fetch(url).then(r=>r.json()).then(renderCryptoDetail).catch(e=>{
    const el=document.getElementById('cryptoForecast');
    if(el)el.innerHTML='<div class="sub" style="color:#ef7a66">加载失败：'+e+'</div>';
  });
}
let polyState={tag:'crypto'};
let polyAutoTimer=null, polyPanelOpen=false;
function togglePolyPanel(){
  polyPanelOpen=!polyPanelOpen;
  const p=document.getElementById('polyPanel');
  p.style.display=polyPanelOpen?'block':'none';
  if(polyPanelOpen){
    loadPoly();
    if(polyAutoTimer)clearInterval(polyAutoTimer);
    polyAutoTimer=setInterval(()=>{
      if(polyPanelOpen && document.getElementById('polyAuto').checked)loadPoly();
    },30000);
  }else if(polyAutoTimer){clearInterval(polyAutoTimer);polyAutoTimer=null;}
}
function loadPoly(){
  const el=document.getElementById('polyList');
  if(el)el.innerHTML='<div class="sub" style="padding:16px;color:#9fb0c0">加载中…（首次拉取行情池约需 10–15 秒，之后 2 分钟内走缓存）</div>';
  const url='/api/polymarket_odds?tag='+encodeURIComponent(polyState.tag)+'&limit=80&_='+Date.now();
  fetch(url).then(r=>r.json()).then(renderPoly).catch(e=>{
    if(el)el.innerHTML='<div class="sub" style="color:#ef7a66">加载失败：'+e+'</div>';
  });
}
function renderPoly(d){
  document.getElementById('polyTs').textContent=d.ts?('更新 '+d.ts+(d.cached?'（缓存）':'')):'';
  const el=document.getElementById('polyList');
  if(!d.ok||!d.markets||!d.markets.length){
    el.innerHTML='<div class="sub" style="color:#ef7a66">'+(d.msg||'暂无数据')+'</div>';return;
  }
  el.innerHTML=d.markets.map(m=>{
    const outs=(m.outcomes||[]).map(o=>{
      const pct=(o.price!=null?o.price*100:0);
      const col=pct>=50?'#5fd98a':'#7e8da0';
      return '<div style="display:flex;align-items:center;gap:8px;margin-top:4px">'
        +'<span style="width:96px;font-size:12px;color:#9fb0c0">'+escapeHtml(o.label)+'</span>'
        +'<span class="probBar" style="flex:1"><i style="width:'+pct.toFixed(1)+'%;background:'+col+'"></i></span>'
        +'<b style="width:50px;text-align:right;color:'+col+'">'+pct.toFixed(1)+'%</b></div>';
    }).join('');
    return '<div class="polyRow"><div class="q">'+escapeHtml(m.question)+'</div>'
      +'<div class="meta">24h量 $'+fmtNum(m.volume24hr)+' ｜ 流动性 $'+fmtNum(m.liquidity)
      +(m.endDate?' ｜ 截止 '+String(m.endDate).slice(0,10):'')+'</div>'+outs+'</div>';
  }).join('');
}
function fmtNum(v){ if(v==null)return '-'; v=Number(v);
  if(v>=1e9)return (v/1e9).toFixed(2)+'B'; if(v>=1e6)return (v/1e6).toFixed(2)+'M';
  if(v>=1e3)return (v/1e3).toFixed(1)+'K'; return v.toFixed(0); }
function escapeHtml(s){ return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function renderCryptoDetail(d){
  const kpiEl=document.getElementById('cryptoKpi');
  const fcEl=document.getElementById('cryptoForecast');
  const chartEl=document.getElementById('cryptoChart');
  document.getElementById('cpTs').textContent=d.ts?('更新 '+d.ts):'';
  if(!d.ok){
    if(kpiEl)kpiEl.innerHTML='';
    if(fcEl)fcEl.innerHTML='<div class="sub" style="color:#ef7a66">'+(d.msg||'获取失败')+'</div>';
    return;
  }
  const t=d.ticker||{}, ind=d.indicators||{}, fc=d.forecast||{};
  const cls=(t.pct||0)>0?'up':((t.pct||0)<0?'down':'flat');const sg=(t.pct||0)>0?'+':'';
  let kpi='<div class="cell"><div class="k">现价</div><div class="v '+cls+'">'+(t.price!=null?t.price.toFixed(2):'-')+'</div></div>';
  kpi+='<div class="cell"><div class="k">24h涨跌</div><div class="v '+cls+'">'+(t.pct!=null?sg+t.pct.toFixed(2)+'%':'-')+'</div></div>';
  kpi+='<div class="cell"><div class="k">24h高</div><div class="v">'+(t.high!=null?t.high.toFixed(2):'-')+'</div></div>';
  kpi+='<div class="cell"><div class="k">24h低</div><div class="v">'+(t.low!=null?t.low.toFixed(2):'-')+'</div></div>';
  kpi+='<div class="cell"><div class="k">24h额(万$)</div><div class="v">'+(t.quote_volume!=null?(t.quote_volume/1e4).toFixed(1):'-')+'</div></div>';
  kpi+='<div class="cell"><div class="k">RSI(14)</div><div class="v">'+((ind.rsi14!=null)?ind.rsi14.toFixed(1):'-')+'</div></div>';
  const emaTxt={'golden':'金叉🟢','dead':'死叉🔴','bullish':'多头🟢','bearish':'空头🔴','neutral':'中性'}[ind.ema_state]||'-';
  kpi+='<div class="cell"><div class="k">EMA(7/25)</div><div class="v">'+(emaTxt)+'</div></div>';
  kpi+='<div class="cell"><div class="k">波动率1σ%</div><div class="v">'+(ind.volatility_pct!=null?ind.volatility_pct.toFixed(2):'-')+'</div></div>';
  kpi+='<div class="cell"><div class="k">趋势斜率%</div><div class="v">'+((ind.trend_slope_pct!=null)?(ind.trend_slope_pct>=0?'+':'')+ind.trend_slope_pct.toFixed(2):'-')+'</div></div>';
  if(kpiEl)kpiEl.innerHTML=kpi;
  // 蜡烛图 + 成交量副图 + 预测区间阴影带
  try{
    const chart=echarts.init(chartEl);
    const dl=d.kline.map(x=>x.date);
    const ohlc=d.kline.map(x=>[x.open,x.close,x.low,x.high]);
    const vol=d.kline.map(x=>x.volume);
    const nT=(fc.next_time||'');
    const hasBand = nT && fc.range_low!=null && fc.range_high!=null;
    if(hasBand){dl.push(nT);ohlc.push(['-','-','-','-']);vol.push('-');}
    const bandArea=hasBand?{markArea:{silent:true,itemStyle:{color:'rgba(120,160,255,0.12)'},
      data:[[{xAxis:d.kline[d.kline.length-1].date,yAxis:fc.range_low},{xAxis:nT,yAxis:fc.range_high}]]}}:{};
    const midLine=hasBand?{markLine:{silent:true,symbol:'none',lineStyle:{type:'dashed',color:'#7aa0ff'},
      data:[{xAxis:d.kline[d.kline.length-1].date,yAxis:fc.range_mid},{xAxis:nT,yAxis:fc.range_mid}]}}:{};
    chart.setOption({backgroundColor:'#0d1219',animation:false,
      grid:[{left:60,right:18,top:16,bottom:62,height:'60%'},{left:60,right:18,top:'76%',height:'16%'}],
      tooltip:{trigger:'axis'},
      xAxis:[{type:'category',data:dl,axisLabel:{color:'#8b98a5',fontSize:12}},{type:'category',gridIndex:1,data:dl,axisLabel:{show:false}}],
      yAxis:[{scale:true,axisLabel:{color:'#8b98a5'}},{scale:true,gridIndex:1,axisLabel:{show:false}}],
      dataZoom:[{type:'inside',xAxisIndex:[0,1]},{type:'slider',height:14,bottom:6,xAxisIndex:[0,1]}],
      series:[Object.assign({type:'candlestick',data:ohlc,itemStyle:{color:'#ff5b5b',color0:'#2ecc71',borderColor:'#ff5b5b',borderColor0:'#2ecc71'}},bandArea,midLine),
              {type:'bar',xAxisIndex:1,yAxisIndex:1,data:vol,itemStyle:{color:'#3a4a5a'}}]});
    window.addEventListener('resize',()=>chart.resize());
  }catch(e){}
  // 研判面板
  const bias=(fc.bias||'');
  const biasColor=bias.indexOf('偏多')>=0?'#5fd98a':(bias.indexOf('偏空')>=0?'#ef7a66':'#9fb0c0');
  const sigColor=fc.signal==='buy'?'#5fd98a':(fc.signal==='sell'?'#ef7a66':'#9fb0c0');
  const sigTxt=fc.signal==='buy'?'买入信号':(fc.signal==='sell'?'卖出信号':'持有/观望');
  if(fcEl)fcEl.innerHTML=
    '<h3 style="font-size:14px;margin:14px 0 6px">📊 短期研判（'+cryptoState.timeframe+'）</h3>'
    +'<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">'
    +'<span style="padding:4px 12px;border-radius:12px;font-weight:700;background:'+biasColor+'22;color:'+biasColor+'">方向：'+bias+'</span>'
    +'<span style="padding:4px 12px;border-radius:12px;font-weight:700;background:'+sigColor+'22;color:'+sigColor+'">'+sigTxt+'</span>'
    +'<span style="color:#9fb0c0;font-size:12px">'+(fc.reason||'')+' ｜ '+(fc.signal_reason||'')+'</span></div>'
    +'<div class="kpi" style="margin-top:10px">'
    +'<div class="cell"><div class="k">下一周期1σ下沿</div><div class="v down">'+(fc.range_low!=null?fc.range_low.toFixed(2):'-')+'</div></div>'
    +'<div class="cell"><div class="k">当前价</div><div class="v">'+(fc.range_mid!=null?fc.range_mid.toFixed(2):'-')+'</div></div>'
    +'<div class="cell"><div class="k">下一周期1σ上沿</div><div class="v up">'+(fc.range_high!=null?fc.range_high.toFixed(2):'-')+'</div></div>'
    +'</div>'
    +'<div class="sub" style="margin-top:6px">图上蓝色阴影带=下一周期1σ统计区间（'+(fc.next_time||'')+'）；区间基于近24根K线对数收益1σ（'+(fc.sigma_pct!=null?fc.sigma_pct.toFixed(2):'-')+'%），含方向研判但<b>不构成投资建议</b>；加密资产波动剧烈，风险自担。</div>';
}
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
let arbPanelOpen=false, arbAutoTimer=null, arbState={pairs:[]};
function toggleArbPanel(){
  arbPanelOpen=!arbPanelOpen;
  const p=document.getElementById('arbPanel');
  p.style.display=arbPanelOpen?'block':'none';
  if(arbPanelOpen){
    loadArb(); loadArbBook();
    if(arbAutoTimer)clearInterval(arbAutoTimer);
    arbAutoTimer=setInterval(()=>{
      if(arbPanelOpen && document.getElementById('arbAuto').checked){loadArb();loadArbBook();}
    },30000);
  }else if(arbAutoTimer){clearInterval(arbAutoTimer);arbAutoTimer=null;}
}
function loadArb(){
  fetch('/api/arb_opps?_='+Date.now()).then(r=>r.json()).then(renderArb).catch(e=>{document.getElementById('arbList').innerHTML='<div class="sub" style="color:#ef7a66">扫描失败：'+e+'</div>';});
}
function loadArbDemo(){
  fetch('/api/arb_demo').then(r=>r.json()).then(d=>{arbState.pairs=d.pairs||[];renderArbFromPairs(arbState.pairs,'演示对（示例价格，非实时）',d);}).catch(e=>{document.getElementById('arbList').innerHTML='<div class="sub" style="color:#ef7a66">载入演示失败：'+e+'</div>';});
}
function renderArb(d){
  arbState.mm = d.marketmaking||[];
  arbState.ev = d.event_arb||[];
  const invMap = d.inventory||{};
  const ms = d.max_skew||300;
  const sum=document.getElementById('arbSummary');
  const list=document.getElementById('arbList');
  sum.innerHTML='Polymarket 单源 ｜ 实时市场 '+(d.poly_count!=null?d.poly_count:'?')+' 条 ｜ 做市机会 '+arbState.mm.length+' ｜ 事件套利(需确认) '+arbState.ev.length;
  let h='';
  h+='<div class="arbSec"><div class="ttl" style="color:#cfe0f0;font-size:14px;margin-bottom:6px">🔁 单边做市价差（买 bid / 卖 ask，库存中性锁定价差）</div>';
  if(arbState.mm.length){
    h+='<table class="arbTable"><thead><tr><th>市场</th><th>买</th><th>卖</th><th>价差</th><th>每单位锁定</th><th>流动性</th><th>份额</th><th></th></tr></thead><tbody>';
    arbState.mm.forEach((o,i)=>{
      const cur = (invMap[o.buy_id]!=null)?parseInt(invMap[o.buy_id]):0;
      const skp = ms? Math.abs(cur)/ms : 0;
      const scol = Math.abs(skp)>=0.8?'#ef7a66':(Math.abs(skp)>=0.5?'#e8c46a':'#5fd38a');
      const capped = Math.abs(cur)>=ms;
      const badge = capped ? ' <span style="color:#ef7a66">⚠已达上限</span>'
                           : (cur!==0?(' <span style="color:'+scol+'">本仓'+(cur>0?'+':'')+cur+'</span>'):'');
      h+='<tr'+(capped?' style="opacity:.5"':'')+'><td>'+escapeHtml(o.question)+badge+'</td><td>'+o.bid.toFixed(4)+'</td><td>'+o.ask.toFixed(4)+'</td><td class="arbEdge">'+o.spread.toFixed(4)+' ('+o.spread_pct+'%)</td><td class="arbEdge">+'+o.unit_profit.toFixed(4)+'</td><td class="arbLiq">'+(o.liquidity||0).toLocaleString()+'</td>'
       +'<td><input class="arbSizeInput" id="mmSize'+i+'" value="'+(o.size_hint||100)+'"></td>'
       +'<td><button class="arbBtn" onclick="execArbMM('+i+')">模拟做市</button></td></tr>';
    });
    h+='</tbody></table>';
  } else h+='<div class="sub" style="margin-top:6px">暂无价差足够的高流动性市场。</div>';
  h+='</div>';
  h+='<div class="arbSec" style="margin-top:14px"><div class="ttl" style="color:#cfe0f0;font-size:14px;margin-bottom:6px">🎯 同事件互斥套利（实验性 · 需人工确认完备性）</div>';
  if(arbState.ev.length){
    arbState.ev.forEach((e)=>{
      h+='<div class="arbEv"><div class="arbEvH">'+escapeHtml(e.question)+' <span class="arbWarn">⚠️ 需人工确认是否互斥完备</span></div>';
      h+='<div class="arbEvSubs">';
      (e.submarkets||[]).forEach(s=>{ h+='<span class="arbSub">'+escapeHtml(s.q)+' &nbsp;bid '+s.bid.toFixed(4)+' / ask '+s.ask.toFixed(4)+'</span>'; });
      h+='</div><div class="sub" style="margin-top:4px">买齐所有结果(ask)成本 '+e.sum_ask.toFixed(4)+' → 理论利润 +'+e.profit_if_complete.toFixed(4)+'</div></div>';
    });
  } else h+='<div class="sub" style="margin-top:6px">暂无确认中的互斥套利（Polymarket 已消除大部分无风险免费钱；此块仅供人工复核线索）。</div>';
  h+='</div>';
  list.innerHTML=h;
  const sel=document.getElementById('btMarket');
  if(sel){
    sel.innerHTML = arbState.mm.slice(0,20).map(o=>'<option value="'+o.buy_id+'">'+escapeHtml((o.question||'').slice(0,60))+'</option>').join('');
  }
}
function renderArbFromPairs(pairs, note, d){
  const sum=document.getElementById('arbSummary');
  const list=document.getElementById('arbList');
  sum.innerHTML='Kalshi 流动性行情：'+(d&&d.kalshi_count!=null?d.kalshi_count:'?')+' 条 ｜ Poly：'+(d&&d.poly_count!=null?d.poly_count:'?')+' 条 ｜ 实时机会：'+pairs.length+' 个';
  if(note) sum.innerHTML+=' <span class="arbNote">'+note+'</span>';
  if(!pairs.length){
    list.innerHTML='<div class="sub" style="margin-top:8px">当前无实时跨平台同事件匹配（Kalshi 本环境公开行情不可读，或两边无同事件价差）。可点「载入演示对」试跑模拟器。</div>';
    return;
  }
  let h='<table class="arbTable"><thead><tr><th>事件</th><th>置信</th><th>方向</th><th>每份额外</th><th>份额</th><th></th></tr></thead><tbody>';
  pairs.forEach((o,i)=>{
    const dem=o.demo?'<span class="arbDemo">演示</span>':'';
    h+='<tr><td>'+escapeHtml(o.question)+dem+'</td><td>'+(o.confidence!=null?o.confidence:'-')+'</td><td>'+escapeHtml(o.action)+'</td><td class="arbEdge">+'+o.edge.toFixed(4)+'</td>'
      +'<td><input class="arbSizeInput" id="arbSize'+i+'" value="'+(o.size_hint||100)+'"></td>'
      +'<td><button class="arbBtn" onclick="execArb('+i+')">模拟下单</button></td></tr>';
  });
  h+='</tbody></table>';
  list.innerHTML=h;
}
function execArb(i){
  const o=arbState.pairs[i]; if(!o)return;
  let size=parseInt(document.getElementById('arbSize'+i).value,10)||0;
  fetch('/api/arb_execute',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({opp:o,size:size})})
    .then(r=>r.json()).then(j=>{
      if(j.ok){loadArbBook(); const b=document.getElementById('arbList'); b.insertAdjacentHTML('afterbegin','<div class="sub" style="color:#5fd38a;margin:4px 0">✅ '+escapeHtml(j.msg)+'</div>');}
      else alert(j.msg||'执行失败');
    }).catch(e=>alert('执行失败：'+e));
}
function execArbMM(i){
  const o=arbState.mm[i]; if(!o)return;
  let size=parseInt(document.getElementById('mmSize'+i).value,10)||0;
  fetch('/api/arb_mm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({opp:o,size:size})})
    .then(r=>r.json()).then(j=>{
      if(j.ok){loadArbBook(); const b=document.getElementById('arbList'); b.insertAdjacentHTML('afterbegin','<div class="sub" style="color:#5fd38a;margin:4px 0">✅ '+escapeHtml(j.msg)+'</div>');}
      else alert(j.msg||'执行失败');
    }).catch(e=>alert('执行失败：'+e));
}
function autoMM(){
  let n=parseInt(document.getElementById('autoMMN').value,10)||5;
  const skip=document.getElementById('arbSkipSkewed').checked?1:0;
  const reb=document.getElementById('arbAutoReb').checked?1:0;
  const minliq=parseInt(document.getElementById('arbMinLiq').value,10)||0;
  const size=parseInt(document.getElementById('arbRotSize').value,10)||0;
  const q='n='+n+'&skip='+skip+'&reb='+reb+'&minliq='+minliq+'&size='+size;
  fetch('/api/arb_auto_mm?'+q).then(r=>r.json()).then(j=>{
    if(j.ok){ loadArb(); loadArbBook();
      const b=document.getElementById('arbList'); b.insertAdjacentHTML('afterbegin','<div class="sub" style="color:#5fd38a;margin:4px 0">✅ 自动轮动：成交 '+j.executed+' 笔 / 跳过 '+j.skipped+' 个高偏斜，锁定收益 $'+j.locked_total.toFixed(2)+'（'+escapeHtml(j.msg||'')+'）</div>');
    } else alert(j.msg||'自动轮动失败');
  }).catch(e=>alert('自动轮动失败：'+e));
}
function setSkew(){
  const v=parseInt(document.getElementById('arbSkewInput').value,10)||0;
  fetch('/api/arb_set_skew',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({value:v})})
   .then(r=>r.json()).then(j=>{ if(j.ok){document.getElementById('arbMaxSkew').textContent=j.max_skew; alert(j.msg); loadArbBook();} else alert(j.msg||'设置失败'); })
   .catch(e=>alert('设置失败：'+e));
}
function setFee(){
  const v=parseFloat(document.getElementById('arbFeeInput').value)||0;
  fetch('/api/arb_set_fee',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({value:v/100})})
   .then(r=>r.json()).then(j=>{ if(j.ok){ alert(j.msg); loadArbBook();} else alert(j.msg||'设置失败'); })
   .catch(e=>alert('设置失败：'+e));
}
function resetArb(){
  if(!confirm('确定清空虚拟账本？\n本金/持仓/库存将清空，但偏斜上限设置会保留。'))return;
  fetch('/api/arb_reset',{method:'POST'}).then(r=>r.json()).then(j=>{ if(j.ok){loadArbBook(); alert(j.msg);} else alert(j.msg||'重置失败'); }).catch(e=>alert('重置失败：'+e));
}
function rebalanceArb(){
  fetch('/api/arb_rebalance',{method:'POST'}).then(r=>r.json()).then(j=>{
    if(j.ok){ loadArbBook(); alert(j.msg); } else alert(j.msg||'再平衡失败');
  }).catch(e=>alert('再平衡失败：'+e));
}
function runBacktest(){
  const mid=document.getElementById('btMarket').value;
  if(!mid){ alert('请先刷新扫描以载入市场列表'); return; }
  const days=parseInt(document.getElementById('btDays').value,10)||30;
  const every=parseInt(document.getElementById('btEvery').value,10)||1440;
  const size=parseInt(document.getElementById('btSize').value,10)||100;
  const el=document.getElementById('btResult');
  el.innerHTML='<div class="sub" style="color:#9fb0c0">回测中（拉取历史价格，请稍候）…</div>';
  fetch('/api/arb_backtest?market='+encodeURIComponent(mid)+'&days='+days+'&every='+every+'&size='+size)
    .then(r=>r.json()).then(renderBacktest).catch(e=>{el.innerHTML='<div class="sub" style="color:#ef7a66">回测失败：'+e+'</div>';});
}
function renderBacktest(j){
  const el=document.getElementById('btResult');
  if(!j.ok){ el.innerHTML='<div class="sub" style="color:#ef7a66">'+escapeHtml(j.msg||'回测失败')+'</div>'; return; }
  let h='<div class="arbSummary"><div>样本点 <b>'+j.points+'</b></div><div>交易笔数 <b>'+j.trades+'</b></div><div>胜率 <b>'+((j.win_rate||0)*100).toFixed(1)+'%</b></div><div>净盈亏 <b style="color:'+(j.net_pnl>=0?'#5fd38a':'#ef7a66')+'">$'+(j.net_pnl>=0?'+':'')+j.net_pnl.toFixed(2)+'</b></div><div>最终权益 <b>$'+j.final_equity.toFixed(2)+'</b></div><div>最大回撤 <b style="color:#ef7a66">$'+j.max_drawdown.toFixed(2)+'</b></div><div>半价差 <b>'+(j.half_spread*100).toFixed(2)+'%</b></div><div>费率 <b>'+(j.fee_rate*100).toFixed(2)+'%</b></div></div>';
  h+='<div class="sub" style="margin:6px 0;color:#9fb0c0">'+escapeHtml(j.note||'')+'</div>';
  h+=drawCurve(j.curve);
  el.innerHTML=h;
}
function drawCurve(curve){
  if(!curve||!curve.length) return '';
  const W=680,H=200,pad=30;
  const eqs=curve.map(c=>c.equity);
  const lo=Math.min(Math.min(eqs),10000), hi=Math.max(Math.max(eqs),10000);
  const x=i=>pad+(W-2*pad)*i/(curve.length-1||1);
  const y=v=>H-pad-(H-2*pad)*(v-lo)/((hi-lo)||1);
  let pts=curve.map((c,i)=>x(i).toFixed(1)+','+y(c.equity).toFixed(1)).join(' ');
  const yb=y(10000);
  return '<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;max-width:680px;background:#0e1620;border:1px solid #2a3a4a;border-radius:8px;margin-top:6px">'
    +'<line x1="'+pad+'" y1="'+yb+'" x2="'+(W-pad)+'" y2="'+yb+'" stroke="#3a4a5a" stroke-dasharray="4 4"/>'
    +'<polyline points="'+pts+'" fill="none" stroke="#5fd38a" stroke-width="2"/>'
    +'<text x="'+pad+'" y="'+(yb-4)+'" fill="#9fb0c0" font-size="10">基准 $10000</text>'
    +'<text x="'+(W-pad-70)+'" y="16" fill="#9fb0c0" font-size="10">权益曲线</text></svg>';
}
function drawEquityCurve(positions){
  if(!positions||positions.length<2) return '';
  const vals=positions.map(p=>p.cash_after!=null?p.cash_after:0);
  const W=680,H=180,pad=28;
  const lo=Math.min(Math.min.apply(null,vals),10000), hi=Math.max(Math.max.apply(null,vals),10000);
  const x=i=>pad+(W-2*pad)*i/(positions.length-1||1);
  const y=v=>H-pad-(H-2*pad)*(v-lo)/((hi-lo)||1);
  let pts=positions.map((p,i)=>x(i).toFixed(1)+','+y(p.cash_after!=null?p.cash_after:0).toFixed(1)).join(' ');
  const yb=y(10000);
  return '<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;max-width:680px;background:#0e1620;border:1px solid #2a3a4a;border-radius:8px;margin:6px 0">'
    +'<line x1="'+pad+'" y1="'+yb+'" x2="'+(W-pad)+'" y2="'+yb+'" stroke="#3a4a5a" stroke-dasharray="4 4"/>'
    +'<polyline points="'+pts+'" fill="none" stroke="#5fa8d3" stroke-width="2"/>'
    +'<text x="'+pad+'" y="'+(yb-4)+'" fill="#9fb0c0" font-size="10">起始 $10000</text>'
    +'<text x="'+(W-pad-80)+'" y="14" fill="#9fb0c0" font-size="10">账户资金曲线</text></svg>';
}
function loadArbBook(){
  fetch('/api/arb_book').then(r=>r.json()).then(renderArbBook).catch(e=>{document.getElementById('arbBook').innerHTML='<div class="sub" style="color:#ef7a66">读取持仓失败：'+e+'</div>';});
}
function renderArbBook(d){
  const el=document.getElementById('arbBook');
  const invEl=document.getElementById('arbInv');
  if(document.getElementById('arbMaxSkew')) document.getElementById('arbMaxSkew').textContent=d.max_skew;
  if(document.getElementById('arbSkewInput')) document.getElementById('arbSkewInput').value=d.max_skew;
  if(invEl){
    if(d.inventory&&d.inventory.length){
      let ih='<div class="arbInvList">';
      d.inventory.forEach(x=>{
        const sk=x.skew; const col=Math.abs(sk)>=0.8?'#ef7a66':(Math.abs(sk)>=0.5?'#e8c46a':'#5fd38a');
        const u=x.unrealized||0; const ucol=u>=0?'#5fd38a':'#ef7a66';
        ih+='<div class="arbInvRow"><span class="invMkt" title="'+(x.mkt||'')+'">'+escapeHtml(x.question||x.mkt||'')+'</span>'
          +'<span class="invNet">净库存 '+(x.net>0?'+':'')+x.net+'</span>'
          +'<span class="invSkew" style="color:'+col+'">偏斜 '+(sk*100).toFixed(0)+'%</span>'
          +'<span class="invMid">mid '+(x.mid!=null?x.mid.toFixed(4):'?')+'</span>'
          +'<span class="invUnreal" style="color:'+ucol+'">未实现 '+(u>=0?'+':'')+'$'+u.toFixed(2)+'</span>'
          +'<span class="invBar"><span class="invBarFill" style="width:'+Math.min(100,Math.abs(sk)*100)+'%;background:'+col+'"></span></span></div>';
      });
      ih+='</div>';
      invEl.innerHTML=ih;
    } else invEl.innerHTML='<div class="sub" style="margin-top:6px">全部市场库存中性（净库存 0），无需再平衡。</div>';
  }
  const up=d.unrealized_pnl||0, eq=d.equity||0, fr=(d.fee_rate!=null?d.fee_rate*100:1);
  let h='<div class="arbSummary"><div>虚拟本金 <b>$'+d.bankroll.toFixed(2)+'</b></div><div>可用现金 <b>$'+d.cash.toFixed(2)+'</b></div><div>已实现盈亏 <b style="color:'+(d.realized_pnl>=0?'#5fd38a':'#ef7a66')+'">$'+(d.realized_pnl>=0?'+':'')+d.realized_pnl.toFixed(2)+'</b></div><div>未实现盈亏 <b style="color:'+(up>=0?'#5fd38a':'#ef7a66')+'">$'+(up>=0?'+':'')+up.toFixed(2)+'</b></div><div>权益市值 <b>$'+eq.toFixed(2)+'</b></div><div>费率 <b>'+fr.toFixed(2)+'%</b></div><div>未平仓腿 <b>'+d.open_positions+'</b></div></div>';
  h+=drawEquityCurve(d.positions);
  if(d.positions&&d.positions.length){
    h+='<div style="margin-top:6px">';
    d.positions.slice().reverse().forEach(p=>{
      const tag=p.kind==='mm_leg'?(p.side==='buy'?'建多仓':'对冲卖'):(p.kind==='long'?'买':'卖');
      const t=p.ts?new Date(p.ts*1000).toLocaleString():'-';
      const fee=(p.fee!=null&&p.fee)?' 费$'+p.fee.toFixed(2):'';
      const ca=(p.cash_after!=null)?p.cash_after.toFixed(2):'?';
      h+='<div class="arbBookRow"><span class="pid">'+p.pid+'</span><span class="qn">'+escapeHtml(p.question||'')+'</span><span>'+tag+' '+escapeHtml(p.venue||'')+' @'+(p.entry!=null?p.entry.toFixed(4):'?')+' ×'+(p.size||0)+fee+'</span><span class="ts">'+t+'</span><span class="cashAfter">余$'+ca+'</span><button class="arbBtn" onclick="settleArb(\''+p.pid+'\')">结算</button></div>';
    });
    h+='</div>';
  } else {
    h+='<div class="sub" style="margin-top:6px">暂无成交记录。</div>';
  }
  el.innerHTML=h;
}
function settleArb(pid){
  fetch('/api/arb_settle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pid:pid})})
    .then(r=>r.json()).then(j=>{ if(j.ok)loadArbBook(); else alert(j.msg||'结算失败'); }).catch(e=>alert('结算失败：'+e));
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


# ----------------------------------------------------- 加密行情卡片网格（逐币指标快照，60s 缓存）
_CRYPTO_GRID_CACHE = (0, None)


def _crypto_grid(ttl: int = 60) -> dict:
    """返回自选加密币的指标快照列表，供前端并列卡片渲染。

    逐币拉 1h K 线计算 RSI(14)/EMA 状态/买卖信号（复用 datasource 已有函数），
    价格与 24h 涨跌幅用 ticker24 公开接口。整组结果缓存 60s，降低 Binance 限流压力。
    """
    global _CRYPTO_GRID_CACHE
    now = time.time()
    if _CRYPTO_GRID_CACHE[1] is not None and (now - _CRYPTO_GRID_CACHE[0]) < ttl:
        return _CRYPTO_GRID_CACHE[1]
    syms = load_crypto_watchlist()
    coins = []
    for sym in syms:
        try:
            df = fetch_crypto_kline(sym, "1h", 100)
            if df is None or getattr(df, "empty", True):
                continue
            ind = compute_crypto_indicators(df)
            tk = fetch_crypto_ticker24(sym)
            if tk and tk.get("ok"):
                price = tk.get("price")
                pct = tk.get("pct")
            else:
                price = float(df["close"].iloc[-1]) if len(df) else None
                pct = None
            rsi = ind.get("rsi14")
            ema_state = ind.get("ema_state", "neutral")
            signal = ("buy" if (ema_state == "golden" and (rsi or 0) < 70)
                      else "sell" if (ema_state == "dead" and (rsi or 0) > 30)
                      else "hold")
            coins.append({
                "symbol": sym,
                "price": price,
                "pct": pct,
                "rsi14": rsi,
                "ema_state": ema_state,
                "volatility_pct": ind.get("volatility_pct"),
                "signal": signal,
            })
        except Exception:  # noqa: BLE001
            continue
    res = {"ok": True, "coins": coins,
           "ts": datetime.now().strftime("%H:%M:%S")}
    _CRYPTO_GRID_CACHE = (now, res)
    return res


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
            self.send_header("Cache-Control",
                             "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
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
        elif p.path == "/api/polymarket_odds":
            q = parse_qs(p.query)
            tag = q.get("tag", ["crypto"])[0]
            try:
                limit = int(q.get("limit", ["30"])[0])
            except ValueError:
                limit = 30
            try:
                res = fetch_polymarket_odds(tag=tag, limit=limit)
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "msg": str(e), "markets": []}
            res["ts"] = datetime.now().strftime("%H:%M:%S")
            self._send(200, json.dumps(res, ensure_ascii=False, default=str),
                       "application/json; charset=utf-8")
        elif p.path == "/api/arb_opps":
            try:
                book = arb_book.get_book()
                pq = polymarket.fetch_poly_quotes(300)
                poly_count = len([x for x in pq if "error" not in x])
                res = arbitrage.scan_poly(
                    pq, inventory=book.inventory, max_skew=book.max_skew)
                res["poly_count"] = poly_count
                res["max_skew"] = book.max_skew
                res["inventory"] = book.inventory
                res["ts"] = datetime.now().strftime("%H:%M:%S")
                self._send(200, json.dumps(res, ensure_ascii=False, default=str),
                           "application/json; charset=utf-8")
            except Exception as e:  # noqa: BLE001
                self._send(200, json.dumps(
                    {"marketmaking": [], "event_arb": [],
                     "poly_count": 0, "note": str(e)},
                    ensure_ascii=False),
                    "application/json; charset=utf-8")
        elif p.path == "/api/arb_book":
            try:
                pq = polymarket.fetch_poly_quotes(300)
                pm = {}
                for q in pq:
                    if "error" in q or not q.get("id"):
                        continue
                    bid = q.get("yes_bid") or 0
                    ask = q.get("yes_ask") or 0
                    pm[q["id"]] = {"bid": bid, "ask": ask,
                                   "mid": (bid + ask) / 2 if bid and ask else 0}
                res = arb_book.get_book().view(pm)
            except Exception:
                res = arb_book.get_book().view()
            self._send(200, json.dumps(res, ensure_ascii=False, default=str),
                       "application/json; charset=utf-8")
        elif p.path == "/api/arb_demo":
            self._send(200, json.dumps({"pairs": arbitrage.demo_pairs()},
                                       ensure_ascii=False, default=str),
                       "application/json; charset=utf-8")
        elif p.path == "/api/arb_auto_mm":
            try:
                qs = parse_qs(p.query)

                def _gi(name, dflt, lo=None, hi=None):
                    try:
                        v = type(dflt)(qs.get(name, [str(dflt)])[0])
                        if lo is not None:
                            v = max(lo, v)
                        if hi is not None:
                            v = min(hi, v)
                        return v
                    except Exception:  # noqa: BLE001
                        return dflt

                n = _gi("n", 5, 1, 50)
                skip = _gi("skip", 1, 0, 1)
                reb = _gi("reb", 0, 0, 1)
                minliq = _gi("minliq", 0, 0, 10 ** 9)
                size = _gi("size", 0, 0, 100000)
                book = arb_book.get_book()
                pq = polymarket.fetch_poly_quotes(300)
                opps = arbitrage.scan_poly_marketmaking(
                    pq, top_n=n, min_liquidity=minliq,
                    inventory=book.inventory, skip_skewed=bool(skip),
                    max_skew=book.max_skew)
                executed = 0
                skipped = 0
                locked_total = 0.0
                msgs = []
                for o in opps:
                    sz = size if size > 0 else int(o.get("size_hint", 100) or 100)
                    mkt = o.get("buy_id")
                    net = int(book.inventory.get(mkt, 0))
                    # 智能选股：若一轮就会触顶偏斜上限，则跳过（避免越做越偏）
                    if skip and (book.max_skew - abs(net)) < sz:
                        skipped += 1
                        continue
                    r = book.market_make(o, sz)
                    if not r.get("ok"):
                        skipped += 1
                        continue
                    executed += 1
                    locked_total += float(r.get("pnl", 0.0) or 0.0)
                    # 自动轮动：建仓后立刻补一笔反向对冲，使该市场成对锁利润
                    if r.get("side") == "buy":
                        r2 = book.market_make(o, sz)
                        if r2.get("ok"):
                            locked_total += float(r2.get("pnl", 0.0) or 0.0)
                            msgs.append(r2.get("msg", ""))
                        else:
                            msgs.append(r.get("msg", ""))
                    else:
                        msgs.append(r.get("msg", ""))
                # 可选：轮动结束后对剩余偏斜做一次强制再平衡
                reb_msg = ""
                if reb:
                    price_map = {}
                    for q in pq:
                        if "error" in q:
                            continue
                        price_map[q["id"]] = {"bid": q.get("yes_bid"),
                                              "ask": q.get("yes_ask")}
                    rr = book.rebalance(price_map)
                    reb_msg = rr.get("msg", "")
                self._send(200, json.dumps({
                    "ok": True, "executed": executed, "skipped": skipped,
                    "locked_total": round(locked_total, 2),
                    "msg": ("；".join(msgs[:3])
                            + ((" ｜ " + reb_msg) if reb_msg else "")),
                    "ts": datetime.now().strftime("%H:%M:%S"),
                }, ensure_ascii=False, default=str),
                    "application/json; charset=utf-8")
            except Exception as e:  # noqa: BLE001
                self._send(200, json.dumps({"ok": False, "msg": str(e)},
                                           ensure_ascii=False),
                           "application/json; charset=utf-8")
        elif p.path == "/api/arb_backtest":
            try:
                qs = parse_qs(p.query)
                mid = (qs.get("market") or [None])[0]
                if not mid:
                    self._send(200, json.dumps(
                        {"ok": False, "msg": "缺 market 参数"},
                        ensure_ascii=False),
                        "application/json; charset=utf-8")
                    return
                try:
                    days = int((qs.get("days") or ["30"])[0])
                    every = int((qs.get("every") or ["1440"])[0])
                    size = int((qs.get("size") or ["100"])[0])
                except Exception:
                    days, every, size = 30, 1440, 100
                import arb_backtest as _abt
                res = _abt.run_backtest(mid, days=days, every_min=every,
                                        size=size,
                                        fee_rate=arb_book.get_book().fee_rate)
                self._send(200, json.dumps(res, ensure_ascii=False,
                                           default=str),
                           "application/json; charset=utf-8")
            except Exception as e:  # noqa: BLE001
                self._send(200, json.dumps({"ok": False, "msg": str(e)},
                                           ensure_ascii=False),
                           "application/json; charset=utf-8")
        elif p.path == "/api/crypto_watchlist":
            try:
                syms = load_crypto_watchlist()
                self._send(200, json.dumps({"ok": True, "symbols": syms},
                                           ensure_ascii=False),
                           "application/json; charset=utf-8")
            except Exception as e:  # noqa: BLE001
                self._send(200, json.dumps({"ok": False, "msg": str(e)},
                                           ensure_ascii=False),
                           "application/json; charset=utf-8")
        elif p.path == "/api/crypto_grid":
            try:
                res = _crypto_grid()
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "msg": str(e)[:120]}
            self._send(200, json.dumps(res, ensure_ascii=False, default=str),
                       "application/json; charset=utf-8")
        elif p.path == "/api/crypto_detail":
            q = parse_qs(p.query)
            sym = q.get("symbol", ["BTC/USDT"])[0]
            tf = q.get("timeframe", ["1h"])[0]
            try:
                limit = int(q.get("limit", ["200"])[0])
            except Exception:  # noqa: BLE001
                limit = 200
            try:
                df = fetch_crypto_kline(sym, tf, limit)
                if df.empty:
                    res = {"ok": False, "msg": "K线获取失败（联网/限流）"}
                else:
                    ind = compute_crypto_indicators(df)
                    fc = crypto_forecast(df, ind, tf)
                    tk = fetch_crypto_ticker24(sym)
                    kline = [{
                        "date": idx.strftime("%Y-%m-%d %H:%M"),
                        "open": float(r.open), "high": float(r.high),
                        "low": float(r.low), "close": float(r.close),
                        "volume": float(r.volume),
                    } for idx, r in df.iterrows()]
                    res = {"ok": True, "symbol": sym, "timeframe": tf,
                           "kline": kline, "indicators": ind,
                           "forecast": fc, "ticker": tk}
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "msg": str(e)[:120]}
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
        elif p.path == "/api/crypto_watchlist":
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                data = {}
            action = str(data.get("action", "add"))
            sym = str(data.get("symbol", "")).strip().upper()
            cur = load_crypto_watchlist()
            if action == "remove":
                new = [s for s in cur if s != sym]
                if len(new) == len(cur):
                    self._send(200, json.dumps(
                        {"ok": False, "msg": "未找到 " + sym}, ensure_ascii=False),
                        "application/json; charset=utf-8"); return
                cur = save_crypto_watchlist(new)
                self._send(200, json.dumps(
                    {"ok": True, "msg": "已删除 " + sym, "symbols": cur},
                    ensure_ascii=False), "application/json; charset=utf-8"); return
            # add
            if not re.match(r"^[A-Z0-9]{2,20}/USDT$", sym):
                self._send(200, json.dumps(
                    {"ok": False,
                     "msg": "格式应为 XXX/USDT（如 SOL/USDT）"},
                    ensure_ascii=False), "application/json; charset=utf-8"); return
            if sym in cur:
                self._send(200, json.dumps(
                    {"ok": True, "msg": "已在自选", "symbols": cur},
                    ensure_ascii=False), "application/json; charset=utf-8"); return
            cur = save_crypto_watchlist(cur + [sym])
            self._send(200, json.dumps(
                {"ok": True, "msg": "已加入 " + sym, "symbols": cur},
                ensure_ascii=False), "application/json; charset=utf-8"); return
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
        elif p.path == "/api/arb_execute":
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                data = {}
            opp = data.get("opp") or {}
            try:
                size = int(data.get("size", 0))
            except Exception:
                size = 0
            res = arb_book.get_book().execute_arb(opp, size)
            self._send(200, json.dumps(res, ensure_ascii=False, default=str),
                       "application/json; charset=utf-8")
        elif p.path == "/api/arb_mm":
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                data = {}
            opp = data.get("opp") or {}
            try:
                size = int(data.get("size", 0))
            except Exception:
                size = 0
            res = arb_book.get_book().market_make(opp, size)
            self._send(200, json.dumps(res, ensure_ascii=False, default=str),
                       "application/json; charset=utf-8")
        elif p.path == "/api/arb_settle":
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                data = {}
            pid = str(data.get("pid", ""))
            res = arb_book.get_book().settle(pid)
            self._send(200, json.dumps(res, ensure_ascii=False, default=str),
                       "application/json; charset=utf-8")
        elif p.path == "/api/arb_rebalance":
            try:
                pq = polymarket.fetch_poly_quotes(300)
                price_map = {}
                for q in pq:
                    if "error" in q:
                        continue
                    price_map[q["id"]] = {"bid": q.get("yes_bid"),
                                          "ask": q.get("yes_ask")}
                res = arb_book.get_book().rebalance(price_map)
                self._send(200, json.dumps(res, ensure_ascii=False, default=str),
                           "application/json; charset=utf-8")
            except Exception as e:  # noqa: BLE001
                self._send(200, json.dumps({"ok": False, "msg": str(e)},
                                           ensure_ascii=False),
                           "application/json; charset=utf-8")
        elif p.path == "/api/arb_set_skew":
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except Exception:  # noqa: BLE001
                data = {}
            try:
                value = int(data.get("value", 0))
            except Exception:  # noqa: BLE001
                value = 0
            res = arb_book.get_book().set_max_skew(value)
            self._send(200, json.dumps(res, ensure_ascii=False, default=str),
                       "application/json; charset=utf-8")
        elif p.path == "/api/arb_set_fee":
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except Exception:  # noqa: BLE001
                data = {}
            try:
                value = float(data.get("value", -1))
            except Exception:  # noqa: BLE001
                value = -1
            res = arb_book.get_book().set_fee(value)
            self._send(200, json.dumps(res, ensure_ascii=False, default=str),
                       "application/json; charset=utf-8")
        elif p.path == "/api/arb_reset":
            res = arb_book.get_book().reset()
            self._send(200, json.dumps(res, ensure_ascii=False, default=str),
                       "application/json; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass


class DualStackServer(ThreadingHTTPServer):
    """IPv4+IPv6 双栈监听：使 127.0.0.1 / localhost / [::1] 均可访问。

    浏览器对 localhost 常优先解析 IPv6(::1)。旧实现只绑 127.0.0.1(IPv4)，
    重启电脑后浏览器按 IPv6 解析 localhost 即 ERR_CONNECTION_REFUSED。
    双栈绑定 :: 并关闭 IPV6_V6ONLY，可在单一 socket 上同时接受 IPv4/IPv6。
    """

    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        super().server_bind()


def _make_server(port, handler):
    try:
        return DualStackServer(("::", port), handler)
    except Exception:
        return ThreadingHTTPServer(("0.0.0.0", port), handler)


def main():
    # 强制 UTF-8：stdout 被重定向到文件时 Python 默认用 GBK，emoji 横幅会崩。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    srv = _make_server(PORT, Handler)
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

"""生成自包含 HTML 看板（零外部依赖，浏览器双击即看）

用法（在 run_daily.py 内调用）：
    from dashboard import render_dashboard
    html = render_dashboard(results, screener_result=screener_result, mode="online")
    # 写入 output/dashboard.html

设计：深色主题、内联 CSS、无 CDN/无 JS 依赖，离线可看。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from html import escape

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.abspath(os.path.join(HERE, "..", "output"))
OUT_PATH = os.path.join(OUTPUT_DIR, "dashboard.html")


def _score_color(score: float) -> str:
    if score >= 0.5:
        return "#21ba72"
    if score >= 0.15:
        return "#4caf50"
    if score > -0.15:
        return "#d4a017"
    if score > -0.5:
        return "#e5533c"
    return "#c0392b"


def _bar(score: float) -> str:
    pct = max(2, min(100, (score + 1) / 2 * 100))
    color = _score_color(score)
    return (
        f'<div class="bar"><span style="width:{pct:.1f}%;'
        f'background:{color}"></span></div>'
    )


def _fmt(x, nd=2):
    try:
        return f"{x:+.{nd}f}"
    except Exception:
        return "-"


def _summary(results) -> str:
    buy = sell = hold = 0
    for r in results:
        if not r.signal:
            continue
        if r.signal in ("买入", "偏多"):
            buy += 1
        elif r.signal in ("减仓", "卖出", "暂停"):
            sell += 1
        else:
            hold += 1
    return (
        f'<div class="summary">'
        f'<span class="pill buy">🟢 关注/买入 {buy}</span>'
        f'<span class="pill hold">🟡 观望/持有 {hold}</span>'
        f'<span class="pill sell">🔴 减仓/卖出 {sell}</span>'
        f"</div>"
    )


def _watchlist_table(results) -> str:
    cards = []
    for r in results:
        notes = "；".join(r.notes) or "—"
        price = f"{r.last_price:.2f}" if r.last_price else "-"
        risk = "" if r.risk_pass else f'<div class="risk">⛔ 风控拦截：{escape(r.risk_reason)}</div>'
        # 离线行同样完整渲染（只加样式与标注），否则看板一片空白像"没有结果"
        card_style = ' style="opacity:.7"' if r.offline else ""
        # 涨跌幅：A股惯例 涨红跌绿
        pct = getattr(r, "pct_change", None)
        if pct is None:
            pct_html = '<span class="flat">-</span>'
        elif pct > 0:
            pct_html = f'<span class="up">+{pct:.2f}%</span>'
        elif pct < 0:
            pct_html = f'<span class="down">{pct:.2f}%</span>'
        else:
            pct_html = '<span class="flat">0.00%</span>'
        src = getattr(r, "source", "") or "-"
        ddate = getattr(r, "data_date", "") or ""
        src_html = (f'<span class="src-syn">合成</span>' if r.offline
                    else f'<span class="src-real">{escape(src.split("(")[0])}</span>')
        nm = escape(r.name)
        sym = escape(r.symbol)
        sig_cls = {"买入": "buy", "卖出": "sell", "持有": "hold",
                   "观望": "hold"}.get(r.signal, "hold")
        intraday = (f'<br><span class="ialert">{escape(r.intraday_alert)}</span>'
                    if getattr(r, "intraday_alert", "") else "")
        cards.append(
            f'<div class="card" data-sym="{sym}"{card_style}>'
            f'<div class="hd"><span><span class="nm">{nm}</span>'
            f'<span class="code">{sym}</span></span>'
            f'<span class="pill {sig_cls}">{r.signal_emoji} {escape(r.signal)}</span></div>'
            f'<div class="px">{price} <span style="font-size:13px">{pct_html}</span></div>'
            f'<div class="dims">趋势{_fmt(r.market_score)} 资金{_fmt(r.money_score)} '
            f'轮动{_fmt(r.sector_score)} ｜ 估值{_fmt(r.valuation_score)} '
            f'消息{_fmt(r.news_score)} 大盘{_fmt(r.regime_score)}</div>'
            f'<div class="score">{_bar(r.composite)}<small>{_fmt(r.composite)}</small></div>'
            f'<div class="meta" style="font-size:11px;color:#7e8da0">{src_html} ｜ {escape(ddate)}{intraday}</div>'
            f'<div class="notes">{escape(notes)}{risk}</div>'
            f'<div class="acts">'
            f'<button class="dtl" onclick="showDetail(\'{sym}\',\'{nm}\')">详情</button>'
            f'<button class="buy" onclick="showTrade(\'{sym}\',\'{nm}\')">📝模拟买卖</button>'
            f'<button class="del" onclick="removeFromWatchlist(\'{sym}\')">删除</button>'
            f'</div>'
            f'</div>'
        )
    return '<div class="grid">' + "\n".join(cards) + "</div>"


def _screener_blocks(screener_result: dict) -> str:
    if not screener_result:
        return ""
    blocks = []
    for label, blk in screener_result.items():
        off = blk.get("offline")
        # 区分「价格合成」(真警告) 与「股池用本地核心池」(价格仍真实，不该报警)
        if off:
            tag = ' <span class="off">⚠️ 价格合成·非真实信号</span>'
        elif blk.get("pool_local"):
            tag = ' <span class="pool">股池：本地核心池 · 价格真实</span>'
        else:
            tag = ""
        rows = []
        for i, rr in enumerate(blk["rows"], 1):
            last = f"{rr['last']:.2f}" if rr.get("last") else "-"
            rows.append(
                f"<tr>"
                f"<td>{i}</td>"
                f'<td class="code">{escape(rr["symbol"])}</td>'
                f"<td>{escape(rr["name"])}</td>"
                f'<td class="score">{_bar(rr["score"])}<small>{_fmt(rr["score"])}</small></td>'
                f"<td>{last}</td>"
                f'<td class="notes">{escape(rr.get("note", ""))}</td>'
                f'<td class="acts">'
                f'<button class="dtl" onclick="showDetail(\'{escape(rr["symbol"])}\',\'{escape(rr["name"])}\')">详情</button>'
                f'<button class="buy" onclick="showTrade(\'{escape(rr["symbol"])}\',\'{escape(rr["name"])}\')">📝模拟买卖</button>'
                f'<button class="add" onclick="addToWatchlist(\'{escape(rr["symbol"])}\',\'{escape(rr["name"])}\')">＋自选</button>'
                f"</td>"
                f"</tr>"
            )
        blocks.append(
            f'<div class="sector">'
            f"<h3>🧩 {escape(label)} {tag}</h3>"
            f'<table><thead><tr><th>#</th><th>代码</th><th>名称</th>'
            f"<th>强度</th><th>最新价</th><th>备注</th><th>操作</th></tr></thead>"
            f'<tbody>{"".join(rows)}</tbody></table></div>'
        )
    return "\n".join(blocks)


CSS = """
* { box-sizing: border-box; }
body { margin:0; background:#0f1419; color:#e6e6e6; font-family:-apple-system,
  "Segoe UI", "Microsoft YaHei", sans-serif; font-size:14px; line-height:1.5; }
.wrap { max-width:1800px; margin:0 auto; padding:16px 16px 40px; }
header { border-bottom:1px solid #2a3340; padding-bottom:12px; margin-bottom:14px; }
h1 { margin:0 0 5px; font-size:18px; }
.meta { color:#8b98a5; font-size:13px; }
.badge { display:inline-block; padding:2px 10px; border-radius:12px; font-size:12px;
  margin-left:8px; }
.badge.online { background:#16341f; color:#5fd98a; }
.badge.offline { background:#3a2a16; color:#e0a85a; }
.summary { margin:10px 0 16px; display:flex; gap:10px; flex-wrap:wrap; }
.pill { padding:5px 13px; border-radius:18px; font-weight:600; font-size:13px; }
.pill.buy { background:#16341f; color:#5fd98a; }
.pill.hold { background:#332c12; color:#e0c45a; }
.pill.sell { background:#3a1c18; color:#ef7a66; }
section { margin-bottom:20px; }
h2 { font-size:16px; border-left:3px solid #4caf50; padding-left:9px; margin:0 0 10px; }
table { width:100%; border-collapse:collapse; background:#161c24;
  border-radius:8px; overflow:hidden; }
th, td { text-align:left; padding:6px 10px; border-bottom:1px solid #232c38;
  vertical-align:top; }
th { background:#1c2530; color:#9fb0c0; font-weight:600; font-size:12px; }
tr:last-child td { border-bottom:none; }
tr.offline td { color:#9b8a6a; font-style:italic; }
/* A股惯例：涨红跌绿 */
.up   { color:#ff5b5b; font-weight:700; }
.down { color:#2ecc71; font-weight:700; }
.flat { color:#888; }
td.px { white-space:nowrap; font-variant-numeric:tabular-nums; }
td.src { white-space:nowrap; font-size:12px; }
.src-real { color:#5ad19a; }
.src-syn  { color:#e0a85a; }
td.src small { color:#777; }
.code { font-family:"SFMono-Regular",Consolas,monospace; color:#7fb4ff; white-space:nowrap; }
.sig { white-space:nowrap; font-weight:600; }
.ialert { font-size:11px; font-weight:400; color:#7fb4ff; display:block; margin-top:2px; }
.score { min-width:130px; }
.bar { height:8px; background:#2a3340; border-radius:5px; overflow:hidden; }
.bar span { display:block; height:100%; border-radius:5px; }
.score small { color:#9fb0c0; font-size:12px; margin-left:6px; }
.dims { color:#9fb0c0; font-size:12px; white-space:nowrap; }
.notes { color:#c4d0db; font-size:12px; }
.risk { color:#ef7a66; font-size:12px; margin-top:4px; }
.sector { background:#121821; border:1px solid #232c38; border-radius:10px;
  padding:12px 14px; margin-bottom:12px; }
.sector h3 { margin:0 0 8px; font-size:14px; }
.off { color:#e0a85a; font-size:12px; }
.pool { color:#7fb3d5; font-size:12px; }
footer { color:#6b7888; font-size:12px; border-top:1px solid #2a3340;
  padding-top:10px; margin-top:20px; }
/* 行内操作按钮 */
.acts { white-space:nowrap; }
.acts button { background:#243246; color:#cfe0f0; border:1px solid #34506e;
  border-radius:6px; padding:4px 8px; font-size:11px; cursor:pointer; margin-right:4px; }
.acts button:hover { background:#2f4660; }
.acts .buy { color:#6ff0a0; border-color:#2c6e4a; }
.acts .dtl { color:#7fb4ff; border-color:#2c4a6e; }
.acts .add { color:#ffd479; border-color:#6e5a2c; }
.acts .del { color:#ef9a9a; border-color:#6e2c2c; }
/* 弹窗（看板内嵌） */
.modal-mask { position:fixed; inset:0; background:rgba(0,0,0,.6);
  display:none; align-items:flex-start; justify-content:center; z-index:9999;
  padding:30px 12px; overflow:auto; }
.modal-mask.show { display:flex; }
.modal { background:#10161f; border:1px solid #2a3340; border-radius:12px;
  max-width:780px; width:100%; padding:18px 20px; box-shadow:0 12px 40px rgba(0,0,0,.5); }
.modal h2 { margin:0 0 4px; font-size:18px; }
.modal .sub { color:#8b98a5; font-size:12px; margin-bottom:12px; }
.modal .close { float:right; background:#243246; color:#cfe0f0; border:none;
  border-radius:6px; padding:4px 10px; cursor:pointer; font-size:12px; }
.kpi { display:grid; grid-template-columns:repeat(auto-fill,minmax(120px,1fr));
  gap:8px; margin:8px 0 12px; }
.kpi .cell { background:#161c24; border:1px solid #232c38; border-radius:8px;
  padding:7px 9px; }
.kpi .k { color:#8b7888; font-size:11px; }
.kpi .v { color:#e6e6e6; font-size:14px; font-weight:600; margin-top:3px;
  font-variant-numeric:tabular-nums; }
.chart { width:100%; height:260px; background:#0d1219; border-radius:8px; }
.flowbar { display:flex; gap:6px; flex-wrap:wrap; margin:6px 0; }
.flowbar .fb { flex:1; min-width:90px; text-align:center; border-radius:6px;
  padding:5px 4px; font-size:12px; }
.flowbar .fb .lbl { color:#9fb0c0; font-size:11px; }
.flowbar .fb .val { font-weight:700; font-variant-numeric:tabular-nums; }
.fin td, .fin th { font-size:12px; }
.trade-form { display:flex; gap:10px; align-items:center; flex-wrap:wrap;
  margin:10px 0; }
.trade-form input { width:110px; background:#0d1219; color:#e6e6e6;
  border:1px solid #2a3340; border-radius:6px; padding:6px 8px; font-size:13px; }
.trade-form .res { flex-basis:100%; color:#9fb0c0; font-size:12px; }
.tag-real { color:#5ad19a; } .tag-syn { color:#e0a85a; } .tag-na { color:#888; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(210px,1fr)); gap:12px; }
.card { background:#161c24; border:1px solid #232c38; border-radius:10px;
  padding:12px 14px; display:flex; flex-direction:column; gap:8px; }
.card:hover { border-color:#2c3a4e; }
.card .hd { display:flex; justify-content:space-between; align-items:baseline; gap:8px; }
.card .nm { font-size:15px; font-weight:700; color:#e6e6e6; }
.card .code { font-family:"SFMono-Regular",Consolas,monospace; color:#7fb4ff;
  font-size:12px; margin-left:6px; }
.card .px { font-size:18px; font-weight:700; font-variant-numeric:tabular-nums; line-height:1.2; }
.card .dims { color:#9fb0c0; font-size:12px; line-height:1.6; }
.card .notes { color:#c4d0db; font-size:12px; }
.card .risk { color:#ef7a66; font-size:12px; margin-top:4px; }
.card .acts { margin-top:2px; }
"""


def render_dashboard(results, screener_result: dict = None,
                     mode: str = "online", show_watchlist: bool = True,
                     as_of: str = "") -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    badge_cls = "online" if mode == "online" else "offline"
    badge_txt = "联网真实信号" if mode == "online" else "离线合成·仅验证"
    summary = _summary(results)
    wl = _watchlist_table(results)
    scr = _screener_blocks(screener_result)
    scr_section = (
        f'<section><h2>🔎 五板块自动选股推荐</h2>{scr}</section>'
        if scr else ""
    )
    asof_note = (f'<div class="meta">信号截至 {as_of}（每日更新，盘中仅提示价突破，'
                 f'买卖信号不随分时抖动）</div>') if as_of else ""
    wl_section = (
        "<section><h2>👁 自选股信号总览</h2>"
        f"{asof_note}"
        f"{wl}</section>"
        if show_watchlist else ""
    )
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>A股信号看板</title><style>" + CSS + "</style>"
        "<script src=\"/static/echarts.min.js\"></script>"
        "<script>if(typeof echarts==='undefined'){"
        "document.write('<script src=\"https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js\"><\\/script>');}"
        "</script></head><body>"
        "<div class=\"wrap\">"
        f"<header><h1>📊 A股信号看板（六因子）</h1>"
        f'<div class="meta">生成时间 {ts}'
        f'<span class="badge {badge_cls}">{badge_txt}</span></div>'
        f"{summary}</header>"
        f"{wl_section}"
        f"{scr_section}"
        "<footer>信号由量化引擎生成，仅供研究参考，不构成投资建议。"
        "A股手动决策、手动下单、风险自担。在可视面板点击「运行」即可刷新本看板。"
        "点击表格「详情」看个股深度资料，「📝模拟买卖」演练下单（零资金）。</footer>"
        "</div>"
        + _modal_html()
        + '<script src="/static/stock_actions.js"></script>'
        + _live_price_js()
        + "</body></html>"
    )


def _modal_html() -> str:
    return (
        '<div class="modal-mask" id="modalMask" '
        'onclick="if(event.target===this)closeModal()">'
        '<div class="modal" id="modalBox"></div></div>'
    )


def _live_price_js() -> str:
    """看板内嵌：每 10 秒拉 /api/quotes，就地刷新自选股表格的「最新价/涨跌」列，
    与顶部横栏报价条同频刷新，不重载整页、不重跑引擎。"""
    return """
<script>
(function(){
  function refreshPx(){
    fetch('/api/quotes').then(function(r){return r.json();}).then(function(j){
      if(!j.ok || !j.quotes) return;
      var m = {}; j.quotes.forEach(function(q){ m[q.symbol] = q; });
      document.querySelectorAll('div.card[data-sym]').forEach(function(card){
        var q = m[card.getAttribute('data-sym')]; if(!q) return;
        var td = card.querySelector('.px'); if(!td) return;
        var cls = q.pct>0 ? 'up' : (q.pct<0 ? 'down' : 'flat');
        var sg = q.pct>0 ? '+' : '';
        td.innerHTML = (q.price!=null ? q.price.toFixed(2) : '-') +
          ' <span style="font-size:13px" class="'+cls+'">'+sg+(q.pct!=null?q.pct.toFixed(2):'0.00')+'%</span>';
      });
    }).catch(function(){});
  }
  refreshPx();
  setInterval(refreshPx, 10000);
})();
</script>
"""


def _fmt_money(x, unit: float = 1.0, nd: int = 2, suffix: str = "") -> str:
    if x is None:
        return '<span class="tag-na">—</span>'
    v = x / unit
    return f"{v:,.{nd}f}{suffix}"





def render_stock_detail(symbol: str, data: dict) -> str:
    """独立个股详情页（查任意股票 / 新标签页打开）。data 由 webui 聚合。"""
    s = data.get("snapshot") or {}
    f = data.get("fund_flow") or {}
    fin = data.get("financials") or {}
    cls = "up" if (s.get("pct") or 0) > 0 else ("down" if (s.get("pct") or 0) < 0 else "flat")
    sign = "+" if (s.get("pct") or 0) > 0 else ""
    src_tag = ('<span class="tag-syn">⚠️ 合成数据</span>' if data.get("synthetic")
               else f'<span class="tag-real">✅ {escape(str(data.get("source") or "真实行情"))}</span>')
    kpi_items = [
        ("现价", f'{s["price"]:.2f}' if s.get("price") is not None else "—", cls),
        ("涨跌幅", f'{sign}{s["pct"]:.2f}%' if s.get("pct") is not None else "—", cls),
        ("今开", f'{s["open"]:.2f}' if s.get("open") is not None else "—", ""),
        ("昨收", f'{s["prev_close"]:.2f}' if s.get("prev_close") is not None else "—", ""),
        ("最高", f'{s["high"]:.2f}' if s.get("high") is not None else "—", ""),
        ("最低", f'{s["low"]:.2f}' if s.get("low") is not None else "—", ""),
        ("振幅", f'{s["amplitude"]:.2f}%' if s.get("amplitude") is not None else "—", ""),
        ("换手率", f'{s["turnover"]:.2f}%' if s.get("turnover") is not None else "—", ""),
        ("量比", f'{s["vol_ratio"]:.2f}' if s.get("vol_ratio") is not None else "—", ""),
        ("市盈率", f'{s["pe"]:.2f}' if s.get("pe") is not None else "—", ""),
        ("市净率", f'{s["pb"]:.2f}' if s.get("pb") is not None else "—", ""),
        ("总市值(亿)", f'{s["mktcap"]/1e8:.2f}' if s.get("mktcap") is not None else "—", ""),
        ("流通市值(亿)", f'{s["float_mktcap"]/1e8:.2f}' if s.get("float_mktcap") is not None else "—", ""),
    ]
    kpi_html = "".join(
        f'<div class="cell"><div class="k">{k}</div>'
        f'<div class="v {c}">{v}</div></div>' for k, v, c in kpi_items)
    flow_items = [
        ("主力净流入", f.get("main")), ("超大单", f.get("huge")),
        ("大单", f.get("big")), ("中单", f.get("mid")), ("小单", f.get("retail")),
    ]
    flow_html = "".join(
        (f'<div class="fb" style="background:#161c24"><div class="lbl">{l}</div>'
         f'<div class="val {("up" if (v or 0)>0 else ("down" if (v or 0)<0 else "flat"))}">'
         f'{("+" if (v or 0)>0 else "")}{(v or 0)/1e8:.2f}亿</div></div>')
        for l, v in flow_items)
    if fin.get("report_date"):
        def _fc(k, suffix="%", nd=2):
            v = fin.get(k)
            return f"<td>{v:.{nd}f}{suffix}</td>" if v is not None else "<td>—</td>"

        fin_html = (
            "<table class=\"fin\"><tr><th>报告期</th><th>营收(亿)</th><th>归母净利(亿)</th>"
            "<th>ROE</th><th>毛利率</th><th>净利同比</th></tr><tr>"
            f"<td>{escape(str(fin.get('report_date','')))}</td>"
            f"<td>{_fmt_money(fin.get('revenue'),1e8)}</td>"
            f"<td>{_fmt_money(fin.get('net_profit'),1e8)}</td>"
            + _fc("roe") + _fc("gross_margin") + _fc("profit_yoy")
            + "</tr></table>"
        )
    else:
        fin_html = '<div class="tag-na">财务数据暂不可用（需联网取 F10）</div>'
    news_html = "".join(f"<li>{escape(t)}</li>" for t in data.get("news", [])) or \
        '<li class="tag-na">暂无新闻（需联网）</li>'
    kline_json = json.dumps(data.get("kline", []), ensure_ascii=False)
    parts = []
    parts.append("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">")
    parts.append("<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">")
    parts.append("<title>" + escape(symbol) + " " + escape(s.get("name", "")) + " 个股详情</title>")
    parts.append("<style>" + CSS + "</style>")
    parts.append("<script src=\"/static/echarts.min.js\"></script>")
    parts.append("<script>if(typeof echarts==='undefined'){document.write('<script src=\"https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js\"><\\/script>');}</script>")
    parts.append("</head><body><div class=\"wrap\">")
    parts.append("<header><h1>" + escape(symbol) + " " + escape(s.get("name", "")) + "</h1>")
    parts.append("<div class=\"meta\">" + src_tag + " ｜ 数据日 " + escape(str(data.get("data_date", "—")))
                 + " ｜ <a href=\"javascript:history.back()\" style=\"color:#7fb3d5\">← 返回</a></div></header>")
    parts.append("<div class=\"kpi\">" + kpi_html + "</div>")
    parts.append("<div id=\"kchart\" class=\"chart\"></div>")
    parts.append("<h2>资金流向</h2><div class=\"flowbar\">" + flow_html + "</div>")
    parts.append("<h2>财务摘要</h2>" + fin_html)
    parts.append("<h2>相关新闻</h2><ul style=\"color:#9fb0c0;font-size:12px;line-height:1.8\">"
                 + news_html + "</ul>")
    parts.append("<footer>数据来自公开行情接口，仅供研究参考，不构成投资建议。</footer>")
    parts.append("</div><script>")
    parts.append("var K=" + kline_json + ";")
    parts.append("var chart=echarts.init(document.getElementById('kchart'));")
    parts.append("chart.setOption({backgroundColor:'#0d1219',grid:{left:55,right:18,top:16,bottom:28},"
                 "tooltip:{trigger:'axis'},"
                 "xAxis:{type:'category',data:K.map(function(x){return x.date;}),axisLine:{lineStyle:{color:'#445'}},axisLabel:{color:'#8b98a5',fontSize:10}},"
                 "yAxis:{scale:true,axisLabel:{color:'#8b98a5'},splitLine:{lineStyle:{color:'#1c2530'}}},"
                 "dataZoom:[{type:'inside'},{type:'slider',height:14,bottom:6}],"
                 "series:[{type:'candlestick',data:K.map(function(x){return [x.open,x.close,x.low,x.high];}),"
                 "itemStyle:{color:'#ff5b5b',color0:'#2ecc71',borderColor:'#ff5b5b',borderColor0:'#2ecc71'}}]});")
    parts.append("window.addEventListener('resize',function(){chart.resize();});")
    parts.append("</script></body></html>")
    return "".join(parts)


def render_portfolio(book: dict) -> str:
    """模拟仓独立页面。"""
    cls = "up" if (book.get("total_pnl") or 0) >= 0 else "down"
    sign = "+" if (book.get("total_pnl") or 0) >= 0 else ""
    pos_html = ""
    if book.get("positions"):
        rows = []
        for p in book["positions"]:
            c = "up" if (p.get("float_pnl") or 0) >= 0 else "down"
            pc = "up" if (p.get("float_pct") or 0) >= 0 else "down"
            rows.append(
                f"<tr><td>{escape(p['symbol'])}</td><td>{escape(p['name'])}</td>"
                f"<td>{p['qty']}</td><td>{p['cost_price']:.2f}</td>"
                f"<td>{p.get('current','—')}</td>"
                f"<td class=\"{c}\">{(p.get('float_pnl') or 0)>=0 and '+' or ''}{p.get('float_pnl',0):.2f}</td>"
                f"<td class=\"{pc}\">{(p.get('float_pct') or 0)>=0 and '+' or ''}{p.get('float_pct',0):.2f}%</td>"
                f"</tr>"
            )
        pos_html = (
            "<table class=\"fin\"><tr><th>代码</th><th>名称</th><th>数量</th>"
            "<th>成本</th><th>现价</th><th>浮盈</th><th>浮盈%</th></tr>"
            + "".join(rows) + "</table>"
        )
    else:
        pos_html = '<div class="tag-na">当前无持仓</div>'
    trades = book.get("trades", [])
    tr_html = ""
    if trades:
        trows = []
        for t in trades[::-1][:30]:
            side = t.get("side")
            sc = "up" if side == "buy" else "down"
            rl = t.get("realized_pnl", 0) or 0
            rlc = "up" if rl >= 0 else "down"
            trows.append(
                f"<tr><td>{escape(t.get('ts',''))}</td><td>{escape(t.get('symbol',''))}</td>"
                f"<td>{escape(t.get('name',''))}</td>"
                f"<td class=\"{sc}\">{'买入' if side=='buy' else '卖出'}</td>"
                f"<td>{t.get('price',0):.2f}</td><td>{t.get('qty',0)}</td>"
                f"<td>{t.get('amount',0):,.2f}</td>"
                f"<td class=\"{rlc}\">{(rl>=0 and '+' or '')}{rl:,.2f}</td></tr>"
            )
        tr_html = (
            "<table class=\"fin\"><tr><th>时间</th><th>代码</th><th>名称</th><th>方向</th>"
            "<th>价格</th><th>数量</th><th>金额</th><th>已实现盈亏</th></tr>"
            + "".join(trows) + "</table>"
        )
    parts = []
    parts.append("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">")
    parts.append("<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">")
    parts.append("<title>模拟仓 · A股</title><style>" + CSS + "</style></head><body><div class=\"wrap\">")
    parts.append("<header><h1>模拟仓（A股 · 独立账本）</h1>")
    parts.append('<button class="back" onclick="try{parent.document.getElementById(\'board\').src=\'/api/board\'}catch(e){history.back()}">← 返回看板</button>')
    parts.append('<div class="meta">模拟盘 · 零资金 · 与加密模拟盘完全独立 ｜ 初始资金 ¥100,000</div></header>')
    parts.append('<div class="kpi">')
    parts.append(f'<div class="cell"><div class="k">总资产</div><div class="v">¥{book.get("total_asset",0):,.2f}</div></div>')
    parts.append(f'<div class="cell"><div class="k">可用资金</div><div class="v">¥{book.get("cash",0):,.2f}</div></div>')
    parts.append(f'<div class="cell"><div class="k">持仓市值</div><div class="v">¥{book.get("market_value",0):,.2f}</div></div>')
    parts.append(f'<div class="cell"><div class="k">总盈亏</div><div class="v {cls}">{sign}{book.get("total_pnl",0):,.2f} ({sign}{book.get("total_pct",0):.2f}%)</div></div>')
    parts.append(f'<div class="cell"><div class="k">已实现盈亏</div><div class="v">¥{book.get("realized_pnl",0):,.2f}</div></div>')
    parts.append(f'<div class="cell"><div class="k">交易笔数</div><div class="v">{book.get("trade_count",0)}</div></div>')
    parts.append("</div>")
    parts.append("<h2>持仓</h2>" + pos_html)
    parts.append("<h2>交易流水（最近30笔）</h2>" + tr_html)
    parts.append("<footer>模拟数据仅本地保存，不构成投资建议。返回看板请关闭此页。</footer>")
    parts.append("</div></body></html>")
    return "".join(parts)


def write_dashboard(results, screener_result: dict = None,
                    mode: str = "online") -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    html = render_dashboard(results, screener_result=screener_result, mode=mode)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    return OUT_PATH


if __name__ == "__main__":
    # 离线自测：构造样例
    from signal_engine import StockResult

    demo = [
        StockResult(symbol="300034", name="钢研高纳", offline=False,
                    market_score=0.6, money_score=0.3, sector_score=0.2,
                    news_score=0.0, composite=0.42, signal="偏多",
                    signal_emoji="🟢", notes=["站上MA20且上行", "触及布林下轨"],
                    risk_pass=True, risk_reason="ok", last_price=18.60),
        StockResult(symbol="300174", name="元力股份", offline=False,
                    market_score=-0.3, money_score=-0.5, sector_score=-0.1,
                    news_score=0.0, composite=-0.32, signal="减仓",
                    signal_emoji="🔴", notes=["主力净流出"],
                    risk_pass=True, risk_reason="ok", last_price=25.10),
        StockResult(symbol="688786", name="悦安新材", offline=True,
                    notes=["离线合成数据，仅验证引擎，未产生真实信号"]),
    ]
    p = write_dashboard(demo, mode="offline")
    print("written:", p)

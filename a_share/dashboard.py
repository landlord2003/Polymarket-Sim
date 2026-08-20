"""生成自包含 HTML 看板（零外部依赖，浏览器双击即看）

用法（在 run_daily.py 内调用）：
    from dashboard import render_dashboard
    html = render_dashboard(results, screener_result=screener_result, mode="online")
    # 写入 output/dashboard.html

设计：深色主题、内联 CSS、无 CDN/无 JS 依赖，离线可看。
"""

from __future__ import annotations

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
    rows = []
    for r in results:
        notes = "；".join(r.notes) or "—"
        price = f"{r.last_price:.2f}" if r.last_price else "-"
        risk = "" if r.risk_pass else f'<div class="risk">⛔ 风控拦截：{escape(r.risk_reason)}</div>'
        # 离线行同样完整渲染（只加样式与标注），否则看板一片空白像"没有结果"
        tr_cls = ' class="offline"' if r.offline else ""
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
        rows.append(
            f"<tr{tr_cls}>"
            f'<td class="code">{sym}</td>'
            f"<td>{nm}</td>"
            f'<td class="sig">{r.signal_emoji} {escape(r.signal)}</td>'
            f'<td class="score">{_bar(r.composite)}<small>{_fmt(r.composite)}</small></td>'
            f'<td class="px">{price}<br>{pct_html}</td>'
            f'<td class="src">{src_html}<br><small>{escape(ddate)}</small></td>'
            f'<td class="dims">行情{_fmt(r.market_score)} 资金{_fmt(r.money_score)}<br>'
            f"板块{_fmt(r.sector_score)} 消息{_fmt(r.news_score)}</td>"
            f'<td class="notes">{escape(notes)}{risk}</td>'
            f'<td class="acts">'
            f'<button class="dtl" onclick="showDetail(\'{sym}\',\'{nm}\')">详情</button>'
            f'<button class="buy" onclick="showTrade(\'{sym}\',\'{nm}\')">📝模拟买卖</button>'
            f'<button class="del" onclick="removeFromWatchlist(\'{sym}\')">删除</button>'
            f"</td>"
            f"</tr>"
        )
    return "\n".join(rows)


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
.wrap { max-width:1100px; margin:0 auto; padding:24px 20px 60px; }
header { border-bottom:1px solid #2a3340; padding-bottom:16px; margin-bottom:20px; }
h1 { margin:0 0 6px; font-size:22px; }
.meta { color:#8b98a5; font-size:13px; }
.badge { display:inline-block; padding:2px 10px; border-radius:12px; font-size:12px;
  margin-left:8px; }
.badge.online { background:#16341f; color:#5fd98a; }
.badge.offline { background:#3a2a16; color:#e0a85a; }
.summary { margin:14px 0 22px; display:flex; gap:10px; flex-wrap:wrap; }
.pill { padding:6px 14px; border-radius:18px; font-weight:600; font-size:13px; }
.pill.buy { background:#16341f; color:#5fd98a; }
.pill.hold { background:#332c12; color:#e0c45a; }
.pill.sell { background:#3a1c18; color:#ef7a66; }
section { margin-bottom:28px; }
h2 { font-size:17px; border-left:3px solid #4caf50; padding-left:10px; margin:0 0 12px; }
table { width:100%; border-collapse:collapse; background:#161c24;
  border-radius:8px; overflow:hidden; }
th, td { text-align:left; padding:9px 12px; border-bottom:1px solid #232c38;
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
.score { min-width:130px; }
.bar { height:8px; background:#2a3340; border-radius:5px; overflow:hidden; }
.bar span { display:block; height:100%; border-radius:5px; }
.score small { color:#9fb0c0; font-size:12px; margin-left:6px; }
.dims { color:#9fb0c0; font-size:12px; white-space:nowrap; }
.notes { color:#c4d0db; font-size:12px; }
.risk { color:#ef7a66; font-size:12px; margin-top:4px; }
.sector { background:#121821; border:1px solid #232c38; border-radius:10px;
  padding:14px 16px; margin-bottom:14px; }
.sector h3 { margin:0 0 10px; font-size:15px; }
.off { color:#e0a85a; font-size:12px; }
.pool { color:#7fb3d5; font-size:12px; }
footer { color:#6b7888; font-size:12px; border-top:1px solid #2a3340;
  padding-top:14px; margin-top:30px; }
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
  gap:8px; margin:10px 0 14px; }
.kpi .cell { background:#161c24; border:1px solid #232c38; border-radius:8px;
  padding:8px 10px; }
.kpi .k { color:#8b7888; font-size:11px; }
.kpi .v { color:#e6e6e6; font-size:14px; font-weight:600; margin-top:3px;
  font-variant-numeric:tabular-nums; }
.chart { width:100%; height:300px; background:#0d1219; border-radius:8px; }
.flowbar { display:flex; gap:6px; flex-wrap:wrap; margin:8px 0; }
.flowbar .fb { flex:1; min-width:90px; text-align:center; border-radius:6px;
  padding:6px 4px; font-size:12px; }
.flowbar .fb .lbl { color:#9fb0c0; font-size:11px; }
.flowbar .fb .val { font-weight:700; font-variant-numeric:tabular-nums; }
.fin td, .fin th { font-size:12px; }
.trade-form { display:flex; gap:10px; align-items:center; flex-wrap:wrap;
  margin:10px 0; }
.trade-form input { width:110px; background:#0d1219; color:#e6e6e6;
  border:1px solid #2a3340; border-radius:6px; padding:6px 8px; font-size:13px; }
.trade-form .res { flex-basis:100%; color:#9fb0c0; font-size:12px; }
.tag-real { color:#5ad19a; } .tag-syn { color:#e0a85a; } .tag-na { color:#888; }
"""


def render_dashboard(results, screener_result: dict = None,
                     mode: str = "online", show_watchlist: bool = True) -> str:
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
    wl_section = (
        "<section><h2>👁 自选股信号总览</h2>"
        "<table><thead><tr><th>代码</th><th>名称</th><th>信号</th>"
        "<th>综合分</th><th>最新价<br><small>涨跌</small></th>"
        "<th>数据源<br><small>数据日</small></th>"
        "<th>四维</th><th>明细/规则</th><th>操作</th></tr></thead>"
        f"<tbody>{wl}</tbody></table></section>"
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
        f"<header><h1>📊 A股四维度信号看板</h1>"
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
        + _modal_js()
        + "</body></html>"
    )


def _modal_html() -> str:
    return (
        '<div class="modal-mask" id="modalMask" '
        'onclick="if(event.target===this)closeModal()">'
        '<div class="modal" id="modalBox"></div></div>'
    )


def _fmt_money(x, unit: float = 1.0, nd: int = 2, suffix: str = "") -> str:
    if x is None:
        return '<span class="tag-na">—</span>'
    v = x / unit
    return f"{v:,.{nd}f}{suffix}"


def _modal_js() -> str:
    return """
<script>
function closeModal(){ document.getElementById('modalMask').classList.remove('show'); }
function openModal(html){ document.getElementById('modalBox').innerHTML = html;
  document.getElementById('modalMask').classList.add('show'); }

function showDetail(symbol, name){
  fetch('/api/stock_detail?symbol='+symbol+'&format=json')
    .then(r=>r.json()).then(d=>{
      if(d.error){ openModal('<button class="close" onclick="closeModal()">关闭</button><h2>'+symbol+' '+name+'</h2><div class="sub" style="color:#ef7a66">'+d.error+'</div>'); return; }
      const s = d.snapshot||{};
      const f = d.fund_flow||{};
      const fin = d.financials||{};
      const cls = (s.pct||0)>0?'up':((s.pct||0)<0?'down':'flat');
      const sign = (s.pct||0)>0?'+':'';
      const srcTag = d.synthetic ? '<span class="tag-syn">⚠️ 合成数据</span>'
                    : '<span class="tag-real">✅ '+(d.source||'真实行情')+'</span>';
      let kpi = '<div class="kpi">';
      kpi += '<div class="cell"><div class="k">现价</div><div class="v '+cls+'">'+(s.price!=null? s.price.toFixed(2):'—')+'</div></div>';
      kpi += '<div class="cell"><div class="k">涨跌幅</div><div class="v '+cls+'">'+(s.pct!=null? sign+s.pct.toFixed(2)+'%':'—')+'</div></div>';
      kpi += '<div class="cell"><div class="k">今开/昨收</div><div class="v">'+(s.open!=null?s.open.toFixed(2):'—')+' / '+(s.prev_close!=null?s.prev_close.toFixed(2):'—')+'</div></div>';
      kpi += '<div class="cell"><div class="k">最高/最低</div><div class="v">'+(s.high!=null?s.high.toFixed(2):'—')+' / '+(s.low!=null?s.low.toFixed(2):'—')+'</div></div>';
      kpi += '<div class="cell"><div class="k">振幅</div><div class="v">'+(s.amplitude!=null?s.amplitude.toFixed(2)+'%':'—')+'</div></div>';
      kpi += '<div class="cell"><div class="k">换手率</div><div class="v">'+(s.turnover!=null?s.turnover.toFixed(2)+'%':'—')+'</div></div>';
      kpi += '<div class="cell"><div class="k">量比</div><div class="v">'+(s.vol_ratio!=null?s.vol_ratio.toFixed(2):'—')+'</div></div>';
      kpi += '<div class="cell"><div class="k">市盈率</div><div class="v">'+(s.pe!=null?s.pe.toFixed(2):'—')+'</div></div>';
      kpi += '<div class="cell"><div class="k">市净率</div><div class="v">'+(s.pb!=null?s.pb.toFixed(2):'—')+'</div></div>';
      kpi += '<div class="cell"><div class="k">总市值(亿)</div><div class="v">'+(s.mktcap!=null?(s.mktcap/1e8).toFixed(2):'—')+'</div></div>';
      kpi += '<div class="cell"><div class="k">流通市值(亿)</div><div class="v">'+(s.float_mktcap!=null?(s.float_mktcap/1e8).toFixed(2):'—')+'</div></div>';
      kpi += '</div>';
      let flow = '<div class="flowbar">';
      const fb = (lbl,val)=> { if(val==null) return ''; const c=val>0?'up':(val<0?'down':'flat');
        const sg=val>0?'+':''; return '<div class="fb" style="background:#161c24"><div class="lbl">'+lbl+'</div><div class="val '+c+'">'+sg+(val/1e8).toFixed(2)+'亿</div></div>'; };
      flow += fb('主力净流入', f.main) + fb('超大单', f.huge) + fb('大单', f.big) + fb('中单', f.mid) + fb('小单', f.retail);
      flow += '</div>';
      let finHtml = '<table class="fin"><tr><th>报告期</th><th>营收(亿)</th><th>归母净利(亿)</th><th>ROE</th><th>毛利率</th><th>净利同比</th></tr>';
      if(fin.report_date){ finHtml += '<tr><td>'+fin.report_date+'</td>'+
        '<td>'+(fin.revenue!=null?(fin.revenue/1e8).toFixed(2):'—')+'</td>'+
        '<td>'+(fin.net_profit!=null?(fin.net_profit/1e8).toFixed(2):'—')+'</td>'+
        '<td>'+(fin.roe!=null?fin.roe.toFixed(2)+'%':'—')+'</td>'+
        '<td>'+(fin.gross_margin!=null?fin.gross_margin.toFixed(2)+'%':'—')+'</td>'+
        '<td>'+(fin.profit_yoy!=null?fin.profit_yoy.toFixed(2)+'%':'—')+'</td></tr>'; }
      else { finHtml += '<tr><td colspan="6" class="tag-na">财务数据暂不可用（需联网取 F10）</td></tr>'; }
      finHtml += '</table>';
      let news = '<ul style="color:#9fb0c0;font-size:12px;line-height:1.7">';
      if(d.news && d.news.length){ d.news.forEach(t=>news+='<li>'+t+'</li>'); }
      else { news += '<li class="tag-na">暂无新闻（需联网）</li>'; }
      news += '</ul>';
      const html = '<button class="close" onclick="closeModal()">关闭</button>'
        + '<button class="add" data-sym="'+symbol+'" data-name="'+name.replace(/"/g, "")+'" onclick="addToWatchlist(this.dataset.sym, this.dataset.name)">＋加入自选股</button>'
        + '<h2>'+symbol+' '+name+'</h2>'
        + '<div class="sub">'+srcTag+' ｜ 数据日 '+(d.data_date||'—')+'</div>'
        + kpi
        + '<div id="kchart" class="chart"></div>'
        + '<h3 style="font-size:14px;margin:14px 0 4px">资金流向</h3>'+flow
        + '<h3 style="font-size:14px;margin:14px 0 4px">财务摘要</h3>'+finHtml
        + '<h3 style="font-size:14px;margin:14px 0 4px">相关新闻</h3>'+news;
      openModal(html);
      // 画 K 线（涨红跌绿）
      const chart = echarts.init(document.getElementById('kchart'));
      const dl = (d.kline||[]).map(x=>x.date);
      const ohlc = (d.kline||[]).map(x=>[x.open,x.close,x.low,x.high]);
      chart.setOption({
        backgroundColor:'#0d1219',
        grid:{left:55,right:18,top:16,bottom:28},
        tooltip:{trigger:'axis'},
        xAxis:{type:'category',data:dl,axisLine:{lineStyle:{color:'#445'}},axisLabel:{color:'#8b98a5',fontSize:10}},
        yAxis:{scale:true,axisLine:{lineStyle:{color:'#445'}},axisLabel:{color:'#8b98a5'},splitLine:{lineStyle:{color:'#1c2530'}}},
        dataZoom:[{type:'inside'},{type:'slider',height:14,bottom:6}],
        series:[{type:'candlestick',data:ohlc,
          itemStyle:{color:'#ff5b5b',color0:'#2ecc71',borderColor:'#ff5b5b',borderColor0:'#2ecc71'}}]
      });
      window.addEventListener('resize',()=>chart.resize());
    }).catch(e=>openModal('<button class="close" onclick="closeModal()">关闭</button><h2>加载失败</h2><div class="sub" style="color:#ef7a66">'+e+'</div>'));
}

function showTrade(symbol, name){
  fetch('/api/stock_detail?symbol='+symbol+'&format=json')
    .then(r=>r.json()).then(d=>{
      const px = (d.snapshot&&d.snapshot.price)|| (d.kline&&d.kline.length? d.kline[d.kline.length-1].close : 0);
      const html = '<button class="close" onclick="closeModal()">关闭</button>'
        + '<h2>📝 模拟买卖 · '+symbol+' '+name+'</h2>'
        + '<div class="sub">🧪 模拟盘 · 零资金 · 不构成投资建议 ｜ 当前价 '+(px? px.toFixed(2):'—')+'</div>'
        + '<div class="trade-form">'
        + '<label>方向 <select id="tSide"><option value="buy">买入</option><option value="sell">卖出</option></select></label>'
        + '<label>价格 <input id="tPrice" type="number" step="0.01" value="'+(px?px.toFixed(2):'')+'"></label>'
        + '<label>数量(股,100倍数) <input id="tQty" type="number" step="100" value="100"></label>'
        + '<button data-sym="'+symbol+'" data-name="'+name.replace(/"/g, "")+'" onclick="submitTrade(this.dataset.sym, this.dataset.name)">确认</button>'
        + '</div><div class="res" id="tRes"></div>'
        + '<div id="tBook"></div>';
      openModal(html);
      refreshBook();
    });
}

function submitTrade(symbol, name){
  const side = document.getElementById('tSide').value;
  const price = parseFloat(document.getElementById('tPrice').value);
  const qty = parseInt(document.getElementById('tQty').value,10);
  fetch('/api/trade',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({symbol,name,side,price,qty})})
    .then(r=>r.json()).then(j=>{
      const el = document.getElementById('tRes');
      el.innerHTML = j.ok? '<span style="color:#5fd98a">✅ '+j.msg+'</span>'
                         : '<span style="color:#ef7a66">⛔ '+j.msg+'</span>';
      refreshBook();
    }).catch(e=>{ document.getElementById('tRes').innerHTML='<span style="color:#ef7a66">'+e+'</span>'; });
}

function refreshBook(){
  fetch('/api/portfolio?format=json').then(r=>r.json()).then(j=>{
    if(!j.book){ return; }
    const b = j.book;
    let h = '<div class="kpi" style="grid-template-columns:repeat(auto-fill,minmax(130px,1fr))">';
    h += '<div class="cell"><div class="k">总资产</div><div class="v">¥'+(b.total_asset/1).toLocaleString()+'</div></div>';
    h += '<div class="cell"><div class="k">可用资金</div><div class="v">¥'+(b.cash/1).toLocaleString()+'</div></div>';
    h += '<div class="cell"><div class="k">总盈亏</div><div class="v '+(b.total_pnl>=0?'up':'down')+'">'+(b.total_pnl>=0?'+':'')+b.total_pnl.toLocaleString()+' ('+(b.total_pct>=0?'+':'')+b.total_pct+'%)</div></div>';
    h += '<div class="cell"><div class="k">已实现</div><div class="v">¥'+(b.realized_pnl/1).toLocaleString()+'</div></div>';
    h += '</div>';
    if(b.positions && b.positions.length){
      h += '<table class="fin"><tr><th>代码</th><th>名称</th><th>数量</th><th>成本</th><th>现价</th><th>浮盈</th><th>浮盈%</th></tr>';
      b.positions.forEach(p=>{ const c=p.float_pnl>=0?'up':'down';
        h += '<tr><td>'+p.symbol+'</td><td>'+p.name+'</td><td>'+p.qty+'</td><td>'+p.cost_price.toFixed(2)+'</td><td>'+(p.current||'-')+'</td><td class="'+c+'">'+(p.float_pnl>=0?'+':'')+p.float_pnl.toFixed(2)+'</td><td class="'+c+'">'+(p.float_pct>=0?'+':'')+p.float_pct+'%</td></tr>'; });
      h += '</table>';
    } else { h += '<div class="tag-na" style="margin-top:8px">当前无持仓</div>'; }
    const box = document.getElementById('tBook'); if(box) box.innerHTML = h;
  });
}

function addToWatchlist(symbol, name){
  fetch('/api/watchlist',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'add',symbol:symbol,name:name})})
    .then(r=>r.json()).then(j=>{
      if(j.ok){ alert('已加入自选股：'+symbol+' '+name);
        if(parent && parent.document && parent.document.getElementById('board'))
          parent.document.getElementById('board').src='/api/board?t='+Date.now(); }
      else { alert('加入失败：'+(j.msg||'未知错误')); }
    }).catch(e=>alert('加入失败：'+e));
}
function removeFromWatchlist(symbol){
  if(!confirm('确认从自选股删除 '+symbol+' ？')) return;
  fetch('/api/watchlist',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'remove',symbol:symbol})})
    .then(r=>r.json()).then(j=>{
      if(j.ok){ if(parent && parent.document && parent.document.getElementById('board'))
          parent.document.getElementById('board').src='/api/board?t='+Date.now(); }
      else { alert('删除失败：'+(j.msg||'未知错误')); }
    }).catch(e=>alert('删除失败：'+e));
}
</script>
"""


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

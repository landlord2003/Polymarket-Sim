# -*- coding: utf-8 -*-
"""实时模拟交易监视器（动态看板 v2）— 接真实引擎 RigorVirtualBook.market_make。

与 v1 的区别（重要，关乎你看到的亏损是否真实）：
  v1 用 random.choice 随机决定成交方向 -> 人为注入逆向选择 -> 假亏损。
  v2 直接调用验证过的 RigorVirtualBook.market_make（与四维扫描同一套逻辑）：
     被动挂单，库存>0则对冲卖出、库存<=0则建仓买入，吃完整价差 + 走簿滑点 + 库存偏置。
  行情仍为【合成随机游走】（非真实 Polymarket），用于演示实时盯盘/成交效果。

端口 8787。启动: python a_share/sim_server.py
"""
import json
import math
import os
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))
from sim_rigor import RigorVirtualBook, rigor_params_from_config  # noqa: E402

PORT = 8787
LOCK = threading.Lock()

# ============ 状态 ============
STATE = {
    "running": True,
    "round": 0,
    "cash": 10000.0,
    "realized": 0.0,
    "equity": 10000.0,
    "inv_notional": 0.0,
    "n_markets": 0,
    "params": {"mm": 0.02, "adverse": 0.15, "tick": 0.002, "size": 100, "inventory_skew": 0.5},
    "quotes": {},
    "positions": [],
    "equity_curve": [],
}

# ============ 合成市场池（随机游走 mid + 采样价差/流动性） ============
NMARKETS = 12
LIQ_POOL = [18443, 22110, 9800, 54000, 12300, 33000, 7600, 41000, 15000, 27500]
MARKETS = {}
random.seed(20260830)
for i in range(NMARKETS):
    MARKETS["M%02d" % (i + 1)] = {
        "mid": round(random.uniform(0.2, 0.8), 4),
        "spread": round(0.004 + 0.056 * random.random(), 4),
        "liq": random.choice(LIQ_POOL),
    }

# ============ 真实引擎实例 ============
book = RigorVirtualBook(rigor=rigor_params_from_config())
book._save = lambda: None                      # 实时循环不落盘，提速
book._record_volume = lambda *a, **k: None     # 禁日上限文件 I/O，提速
book.max_skew = 300                            # 允许 size 维度（生产真实上限 300）


def step():
    """跑一轮：对全部合成市场各调一次真实 market_make（被动做市 + 自动建/平）。"""
    p = book.rigor
    adverse = float(p.get("adverse_frac", 0.15))
    skew = float(p.get("inventory_skew", 0.0))
    size = STATE["params"]["size"]
    with LOCK:
        STATE["round"] += 1
        rnd = STATE["round"]
        quotes = {}
        for kid, m in MARKETS.items():
            # 价格随机游走（合成行情，非真实 Polymarket）
            m["mid"] = max(0.05, min(0.95, m["mid"] + random.gauss(0, 0.003)))
            mid = m["mid"]
            spread = m["spread"]
            half = round(spread / 2.0, 4)
            bid = max(0.02, round(mid - half, 4))
            ask = min(0.98, round(mid + half, 4))
            if ask <= bid:
                ask = min(0.98, bid + 0.001)
            # 计算我们将挂出的双边报价（与引擎同公式，仅用于看板展示）
            off = skew * spread
            buy_base = bid + adverse * spread
            sell_base = ask - adverse * spread
            inv = int(book.inventory.get(kid, 0))
            if inv > 0:
                sell_base = min(ask, sell_base + off)
                buy_base = max(bid, buy_base - off)
            elif inv < 0:
                buy_base = max(bid, buy_base - off)
                sell_base = min(ask, sell_base + off)
            quotes[kid] = {
                "mid": round(mid, 4), "bid": bid, "ask": ask,
                "our_buy": round(buy_base, 4), "our_sell": round(sell_base, 4),
                "inv": inv,
            }
            opp = {
                "buy_ask": bid, "sell_bid": ask, "liquidity": m["liq"],
                "buy_id": kid, "sell_id": kid, "question": kid,
                "end_date": None, "buy_venue": "poly", "sell_venue": "poly",
            }
            try:
                book.market_make(opp, size)
            except Exception:
                pass
        # 快照到 STATE
        STATE["cash"] = round(book.cash, 2)
        STATE["realized"] = round(book.realized_pnl, 2)
        STATE["equity"] = book.equity_marked()
        inv = {k: v for k, v in book.inventory.items() if v != 0}
        STATE["n_markets"] = len(inv)
        STATE["inv_notional"] = book.inventory_notional()
        STATE["quotes"] = quotes
        # 最近成交（取引擎 positions 末尾，新->旧）
        pos = []
        for e in book.positions[-40:][::-1]:
            pos.append({
                "ts": round(e.get("ts", time.time()), 1),
                "mkt": e.get("mkt") or e.get("event_id") or "-",
                "side": e.get("side", ""),
                "entry": e.get("entry"),
                "size": e.get("size"),
                "pnl": e.get("pnl"),
                "slip": e.get("slip"),
                "cash_after": e.get("cash_after"),
            })
        STATE["positions"] = pos
        STATE["equity_curve"].append(round(STATE["equity"], 2))
        if len(STATE["equity_curve"]) > 600:
            STATE["equity_curve"].pop(0)


def loop():
    while True:
        with LOCK:
            if not STATE["running"]:
                break
        step()
        time.sleep(1.2)


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>实时模拟交易监视器 v2（真实引擎）</title>
<style>
:root{--bg:#0f1420;--panel:#192033;--ink:#e6ecf5;--mut:#9aa7bd;--red:#ff5b6e;--grn:#39d98a;--blue:#5aa9ff;--line:#2a3450}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{padding:16px 22px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;flex-wrap:wrap}
header h1{margin:0;font-size:19px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--grn);box-shadow:0 0 8px var(--grn);animation:pulse 1.4s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.badge{font-size:12px;padding:3px 10px;border-radius:20px;background:#1f2940;color:var(--mut);border:1px solid var(--line)}
.banner{background:#241b12;border:1px solid #5a4422;color:#e8c98a;padding:8px 14px;margin:12px 22px 0;border-radius:8px;font-size:12.5px}
.wrap{padding:14px 22px;max-width:1180px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:12px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:13px 16px}
.card .k{color:var(--mut);font-size:12px}.card .v{font-size:21px;font-weight:700;margin-top:4px}
.v.g{color:var(--grn)}.v.r{color:var(--red)}.v.b{color:var(--blue)}
.grid{display:grid;grid-template-columns:1.3fr 1fr;gap:16px}
@media(max-width:880px){.grid{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:16px}
.panel h2{margin:0 0 10px;font-size:15px}
canvas{width:100%;height:240px;background:#0b1018;border:1px solid var(--line);border-radius:8px;display:block}
.scroll{max-height:400px;overflow:auto}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{border:1px solid var(--line);padding:5px 8px;text-align:center}
th{background:#1f2940;color:var(--mut);position:sticky;top:0}
td.l{text-align:left}.buy{color:var(--grn)}.sell{color:var(--red)}
.note{color:var(--mut);font-size:12px;margin-top:8px}
</style></head>
<body>
<header>
  <span class="dot"></span><h1>实时模拟交易监视器 v2</h1>
  <span class="badge" id="rnd">round 0</span>
  <span class="badge">引擎: RigorVirtualBook.market_make（真实定价+走簿滑点）</span>
</header>
<div class="banner">⚠️ 行情为<b>合成随机游走</b>（非真实 Polymarket）。这里跑的是<b>验证过的真实做市引擎</b>，但成交环境是模拟的——用于演示实时盯盘/成交，不等同于实盘结论。</div>
<div class="wrap">
  <div class="cards" id="cards"></div>
  <div class="grid">
    <div class="panel"><h2>盯市权益曲线 (实时)</h2><canvas id="cv" width="640" height="240"></canvas>
      <div class="note">equity = 现金 + 未平仓库存 × last_mid（含未实现盈亏）</div></div>
    <div class="panel"><h2>市场盘口 (本轮报价)</h2>
      <table id="mkt"><thead><tr><th>市场</th><th>mid</th><th>我们买</th><th>我们卖</th><th>库存</th></tr></thead><tbody></tbody></table>
      <div class="note">绿=我们买价(bid+adverse·spread)，红=我们卖价(ask−adverse·spread)，含库存偏置</div></div>
  </div>
  <div class="panel"><h2>最近成交 (实时)</h2>
    <div class="scroll"><table id="trd"><thead><tr><th>时间</th><th>市场</th><th>方向</th><th>成交价</th><th>量</th><th>本笔锁利</th><th>滑点</th><th>现金</th></tr></thead><tbody></tbody></table></div>
    <div class="note">方向 BUY=建仓，SELL=对冲/平仓；仅平仓轮次有「本笔锁利」</div></div>
</div>
<script>
function fmt(n){return (n==null)?'-':Number(n).toLocaleString('en-US',{maximumFractionDigits:2})}
async function tick(){
  try{
    const r=await fetch('/api/state'); const s=await r.json();
    document.getElementById('rnd').textContent='round '+s.round;
    const eq=s.equity; const pnl=eq-10000;
    const cards=[
      {k:'轮次',v:s.round,b:''},
      {k:'现金',v:'$'+fmt(s.cash),b:''},
      {k:'已实现盈亏',v:(s.realized>=0?'+$':'-$')+fmt(Math.abs(s.realized)),b:s.realized>=0?'g':'r'},
      {k:'盯市权益',v:'$'+fmt(eq),b:pnl>=0?'g':'r'},
      {k:'浮动盈亏',v:(pnl>=0?'+$':'-$')+fmt(Math.abs(pnl)),b:pnl>=0?'g':'r'},
      {k:'持仓市场数',v:s.n_markets,b:'b'},
      {k:'库存名义',v:'$'+fmt(s.inv_notional),b:''},
    ];
    document.getElementById('cards').innerHTML=cards.map(c=>`<div class="card"><div class="k">${c.k}</div><div class="v ${c.b}">${c.v}</div></div>`).join('');
    const cv=document.getElementById('cv'); const ctx=cv.getContext('2d');
    const W=cv.width,H=cv.height; ctx.clearRect(0,0,W,H);
    const ec=s.equity_curve; if(ec.length>1){
      const mn=Math.min.apply(null,ec), mx=Math.max.apply(null,ec); const pad=(mx-mn)*0.15||1;
      const lo=mn-pad, hi=mx+pad;
      ctx.strokeStyle='#2a3450';ctx.beginPath();ctx.moveTo(0,H/2);ctx.lineTo(W,H/2);ctx.stroke();
      ctx.strokeStyle='#39d98a';ctx.lineWidth=2;ctx.beginPath();
      ec.forEach((v,i)=>{const x=i/(ec.length-1)*W; const y=H-((v-lo)/(hi-lo))*H; i?ctx.lineTo(x,y):ctx.moveTo(x,y);});
      ctx.stroke();
      ctx.fillStyle='#9aa7bd';ctx.font='11px sans-serif';
      ctx.fillText('$'+fmt(hi),4,12);ctx.fillText('$'+fmt(lo),4,H-4);
    }
    const tb=document.getElementById('mkt').querySelector('tbody');
    tb.innerHTML=Object.keys(s.quotes).map(k=>{const q=s.quotes[k];
      return `<tr><td>${k}</td><td>${q.mid.toFixed(4)}</td><td class="buy">${q.our_buy.toFixed(4)}</td><td class="sell">${q.our_sell.toFixed(4)}</td><td>${q.inv}</td></tr>`;}).join('');
    const t2=document.getElementById('trd').querySelector('tbody');
    t2.innerHTML=s.positions.map(t=>`<tr><td>${t.ts}</td><td>${t.mkt}</td><td class="${t.side=='buy'?'buy':'sell'}">${(t.side||'').toUpperCase()}</td><td>${t.entry!=null?Number(t.entry).toFixed(4):'-'}</td><td>${t.size}</td><td>${t.pnl!=null?'$'+fmt(t.pnl):'-'}</td><td>${t.slip!=null?Number(t.slip).toFixed(4):'-'}</td><td>$${fmt(t.cash_after)}</td></tr>`).join('');
  }catch(e){}
}
setInterval(tick,2000); tick();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/state":
            with LOCK:
                snap = {
                    "round": STATE["round"], "cash": STATE["cash"],
                    "realized": STATE["realized"], "equity": STATE["equity"],
                    "n_markets": STATE["n_markets"], "inv_notional": STATE["inv_notional"],
                    "params": STATE["params"], "quotes": STATE["quotes"],
                    "positions": STATE["positions"], "equity_curve": STATE["equity_curve"],
                }
            body = json.dumps(snap, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


def main():
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print("[sim_server v2] listening on http://127.0.0.1:%d  (Ctrl+C to stop)" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        with LOCK:
            STATE["running"] = False
        srv.shutdown()


if __name__ == "__main__":
    main()

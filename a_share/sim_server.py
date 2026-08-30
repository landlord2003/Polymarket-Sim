# -*- coding: utf-8 -*-
"""实时模拟交易监视器（动态看板）— 自包含，纯标准库，无第三方依赖。

功能：
  - 后台线程持续跑 MM 做市模拟循环（真实定价逻辑：吃完整价差 + 库存偏置报价）
  - HTTP GET /api/state 暴露实时状态（JSON）
  - HTTP GET /        返回动态看板（canvas 权益曲线 + 实时成交表），每 2s 轮询刷新
端口 8787。启动: python a_share/sim_server.py
"""
import json, math, random, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = 8787
LOCK = threading.Lock()

# ---- 模拟全局状态 ----
STATE = {
    "running": True,
    "round": 0,
    "cash": 10000.0,
    "realized": 0.0,
    "positions": {},          # token_id -> {"net": int, "avg": float}
    "equity_curve": [],       # 盯市权益采样
    "trades": [],             # 最近成交（倒序，最多 40）
    "params": {"mm": 0.02, "adverse": 0.15, "tick": 0.001, "size": 100, "inventory_skew": 0.5},
    "last_mid": {},           # token_id -> mid（盯市用）
}

# 合成市场池：id -> 当前 mid（随机游走）
MARKETS = {}
for i in range(12):
    mid = round(random.uniform(0.15, 0.85), 4)
    MARKETS["M%02d" % (i + 1)] = {"mid": mid, "spread": round(random.uniform(0.012, 0.03), 4)}


def step():
    """跑一轮模拟做市：挑一个市场，按真实定价逻辑双边报价，随机一边被吃掉成交。"""
    p = STATE["params"]
    mm = p["mm"]; adv = p["adverse"]; tick = p["tick"]; size = p["size"]; skew = p["inventory_skew"]
    with LOCK:
        STATE["round"] += 1
        rnd = STATE["round"]
        # 随机挑市场，mid 随机游走
        kid = random.choice(list(MARKETS.keys()))
        m = MARKETS[kid]
        m["mid"] = max(0.05, min(0.95, m["mid"] + random.gauss(0, 0.01)))
        mid = m["mid"]; spread = m["spread"]
        bid = mid - spread / 2.0
        ask = mid + spread / 2.0
        STATE["last_mid"][kid] = mid

        inv = STATE["positions"].get(kid, {}).get("net", 0)
        off = skew * spread
        buy_base = bid + (adv + 0.0) * spread
        sell_base = ask - (adv + 0.0) * spread
        if inv > 0:
            sell_base = min(ask, sell_base + off); buy_base = max(bid, buy_base - off)
        elif inv < 0:
            buy_base = max(bid, buy_base - off); sell_base = min(ask, sell_base + off)
        # 量化到 tick
        buy_px = round(max(tick, round(buy_base / tick) * tick), 4)
        sell_px = round(min(1 - tick, round(sell_base / tick) * tick), 4)

        # 模拟被动成交：本轮随机一边被吃（对等概率），价格=报价
        side = random.choice(["buy", "sell"])
        px = buy_px if side == "buy" else sell_px
        pos = STATE["positions"].setdefault(kid, {"net": 0, "avg": 0.0})
        if side == "buy":
            # 我们 BUY 成交：付钱，库存 +size
            cost = px * size
            STATE["cash"] -= cost
            new_net = pos["net"] + size
            pos["avg"] = (pos["avg"] * pos["net"] + px * size) / new_net if new_net else 0.0
            pos["net"] = new_net
        else:
            # 我们 SELL 成交：收钱，库存 -size；若有正值库存则实现盈亏
            rev = px * size
            STATE["cash"] += rev
            if pos["net"] >= size:
                STATE["realized"] += (px - pos["avg"]) * size
                pos["net"] -= size
            else:
                # 平空/反手：实现盈亏（空头的反向）
                if pos["net"] > 0:
                    STATE["realized"] += (px - pos["avg"]) * pos["net"]
                    remain = size - pos["net"]
                    pos["net"] = -remain
                    pos["avg"] = px
                else:
                    new_net = pos["net"] - size
                    STATE["realized"] += (pos["avg"] - px) * size  # 空头平仓盈亏
                    pos["net"] = new_net
                    if pos["net"] == 0:
                        pos["avg"] = 0.0

        # 盯市权益
        equity = STATE["cash"]
        for tid, pp in STATE["positions"].items():
            if pp["net"] != 0:
                lm = STATE["last_mid"].get(tid, mid)
                equity += pp["net"] * lm
        STATE["equity_curve"].append(round(equity, 2))
        if len(STATE["equity_curve"]) > 600:
            STATE["equity_curve"].pop(0)

        trade = {
            "round": rnd, "market": kid, "mid": round(mid, 4),
            "bid": round(bid, 4), "ask": round(ask, 4),
            "our_buy": buy_px, "our_sell": sell_px,
            "filled_side": side, "fill_px": px, "size": size,
            "net": pos["net"], "cash": round(STATE["cash"], 2),
            "equity": round(equity, 2),
        }
        STATE["trades"].insert(0, trade)
        if len(STATE["trades"]) > 40:
            STATE["trades"].pop()


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
<title>实时模拟交易监视器</title>
<style>
:root{--bg:#0f1420;--panel:#192033;--ink:#e6ecf5;--mut:#9aa7bd;--red:#ff5b6e;--grn:#39d98a;--blue:#5aa9ff;--line:#2a3450}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{padding:16px 22px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;flex-wrap:wrap}
header h1{margin:0;font-size:19px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--grn);box-shadow:0 0 8px var(--grn);animation:pulse 1.4s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.badge{font-size:12px;padding:3px 10px;border-radius:20px;background:var(--panel2,#1f2940);color:var(--mut);border:1px solid var(--line)}
.wrap{padding:18px 22px;max-width:1180px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:13px 16px}
.card .k{color:var(--mut);font-size:12px}.card .v{font-size:22px;font-weight:700;margin-top:4px}
.v.g{color:var(--grn)}.v.r{color:var(--red)}.v.b{color:var(--blue)}
.grid{display:grid;grid-template-columns:1.3fr 1fr;gap:16px}
@media(max-width:880px){.grid{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:16px}
.panel h2{margin:0 0 10px;font-size:15px}
canvas{width:100%;height:240px;background:#0b1018;border:1px solid var(--line);border-radius:8px;display:block}
.scroll{max-height:380px;overflow:auto}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{border:1px solid var(--line);padding:5px 8px;text-align:center}
th{background:#1f2940;color:var(--mut);position:sticky;top:0}
td.l{text-align:left}.buy{color:var(--grn)}.sell{color:var(--red)}
.note{color:var(--mut);font-size:12px;margin-top:8px}
</style></head>
<body>
<header>
  <span class="dot"></span><h1>实时模拟交易监视器</h1>
  <span class="badge" id="rnd">round 0</span>
  <span class="badge">MM 做市 · 真实定价逻辑 · 每 1.2s 一轮</span>
</header>
<div class="wrap">
  <div class="cards" id="cards"></div>
  <div class="grid">
    <div class="panel"><h2>盯市权益曲线 (实时)</h2><canvas id="cv" width="640" height="240"></canvas>
      <div class="note">equity = 现金 + 未平仓库存 × last_mid</div></div>
    <div class="panel"><h2>市场盘口 (本轮报价)</h2>
      <table id="mkt"><thead><tr><th>市场</th><th>mid</th><th>我们买</th><th>我们卖</th><th>库存</th></tr></thead><tbody></tbody></table>
      <div class="note">buy_base/sell_base 含库存偏置(±off)；绿=我们买价，红=我们卖价</div></div>
  </div>
  <div class="panel"><h2>最近成交 (实时)</h2>
    <div class="scroll"><table id="trd"><thead><tr><th>轮</th><th>市场</th><th>mid</th><th>买价</th><th>卖价</th><th>成交方向</th><th>成交价</th><th>量</th><th>净库存</th><th>现金</th><th>权益</th></tr></thead><tbody></tbody></table></div>
  </div>
</div>
<script>
function fmt(n){return (n==null)?'-':Number(n).toLocaleString('en-US',{maximumFractionDigits:2})}
async function tick(){
  try{
    const r=await fetch('/api/state'); const s=await r.json();
    document.getElementById('rnd').textContent='round '+s.round;
    const eq=s.equity_curve.length?s.equity_curve[s.equity_curve.length-1]:s.cash;
    const pnl=eq-10000;
    const cards=[
      {k:'轮次',v:s.round,b:''},
      {k:'现金',v:'$'+fmt(s.cash),b:''},
      {k:'已实现盈亏',v:'$'+fmt(s.realized),b:s.realized>=0?'g':'r'},
      {k:'盯市权益',v:'$'+fmt(eq),b:''},
      {k:'浮动盈亏',v:(pnl>=0?'+$':'-$')+fmt(Math.abs(pnl)),b:pnl>=0?'g':'r'},
      {k:'持仓市场数',v:Object.keys(s.positions).filter(k=>s.positions[k].net!=0).length,b:'b'},
    ];
    document.getElementById('cards').innerHTML=cards.map(c=>`<div class="card"><div class="k">${c.k}</div><div class="v ${c.b}">${c.v}</div></div>`).join('');
    // 权益曲线
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
    // 市场盘口
    const tb=document.getElementById('mkt').querySelector('tbody');
    const kids=Object.keys(s.last_mid).slice(0,12);
    tb.innerHTML=kids.map(k=>{const net=(s.positions[k]||{}).net||0; const t=s._quotes?null:null;
      return `<tr><td>${k}</td><td>${s.last_mid[k].toFixed(4)}</td><td class="buy">${s._q?'-':'-'}</td><td class="sell">-</td><td>${net}</td></tr>`;}).join('');
    // 用 trades 最新一轮补全报价
    if(s.trades.length){
      const last=s.trades[0];
      tb.innerHTML=kids.map(k=>{
        const tr=s.trades.find(x=>x.market===k)||last;
        const net=(s.positions[k]||{}).net||0;
        return `<tr><td>${k}</td><td>${s.last_mid[k].toFixed(4)}</td><td class="buy">${tr.our_buy.toFixed(4)}</td><td class="sell">${tr.our_sell.toFixed(4)}</td><td>${net}</td></tr>`;
      }).join('');
    }
    // 成交表
    const t2=document.getElementById('trd').querySelector('tbody');
    t2.innerHTML=s.trades.map(t=>`<tr><td>${t.round}</td><td>${t.market}</td><td>${t.mid.toFixed(4)}</td><td class="buy">${t.our_buy.toFixed(4)}</td><td class="sell">${t.our_sell.toFixed(4)}</td><td class="${t.filled_side=='buy'?'buy':'sell'}">${t.filled_side.toUpperCase()}</td><td>${t.fill_px.toFixed(4)}</td><td>${t.size}</td><td>${t.net}</td><td>$${fmt(t.cash)}</td><td>$${fmt(t.equity)}</td></tr>`).join('');
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
                    "realized": STATE["realized"], "params": STATE["params"],
                    "positions": STATE["positions"], "equity_curve": STATE["equity_curve"],
                    "trades": STATE["trades"], "last_mid": STATE["last_mid"],
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
            self.send_response(404); self.end_headers()

    def log_message(self, *a):
        pass


def main():
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print("[sim_server] listening on http://127.0.0.1:%d  (Ctrl+C to stop)" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        with LOCK:
            STATE["running"] = False
        srv.shutdown()


if __name__ == "__main__":
    main()

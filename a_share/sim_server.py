# -*- coding: utf-8 -*-
"""实时模拟交易监视器 v3 — 真实 Polymarket 盘口驱动 + 真实引擎做市 + 真实内容展示。

与 v2 的区别（关键）：
  v2 行情是【合成随机游走】。
  v3 行情改为【真实 Polymarket 盘口】：用标准库 urllib 直连 gamma-api.polymarket.com
      (polymarket.fetch_poly_quotes)，每 ~90s 刷新真实二元市场盘口(yes_bid/yes_ask/
      no_bid/no_ask/liquidity/token_id)，对每个真实市场在 YES token 上双边做市。
  成交引擎仍是验证过的 RigorVirtualBook.market_make（被动报价 + 走簿滑点 + 库存偏置）。
  全程 DRY_RUN（影子账本），零网络 POST、零真钱。

看板新增「Polymarket 真实行情」区：像 A 股看板那样展示真实市场
  (question + YES/NO 真实买卖盘口 + 流动性 + 类别)，可按类别过滤。

端口 8787。启动: python a_share/sim_server.py
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))
from sim_rigor import RigorVirtualBook, rigor_params_from_config  # noqa: E402
import polymarket as P  # noqa: E402

PORT = 8787
LOCK = threading.Lock()
MM_N = 20          # 同时做市的真实市场数（取流动性最高者）
MM_REFRESH = 75    # 每 75 轮(~90s)刷新一次真实盘口池

# ============ 状态 ============
STATE = {
    "running": True,
    "round": 0,
    "cash": 10000.0,
    "realized": 0.0,
    "equity": 10000.0,
    "round_pnl": 0.0,
    "inv_notional": 0.0,
    "n_markets": 0,
    "live_count": 0,
    "mm_count": 0,
    "last_refresh": 0.0,
    "params": {"mm": 0.02, "adverse": 0.15, "tick": 0.002, "size": 100, "inventory_skew": 0.5},
    "quotes": {},
    "positions": [],
    "equity_curve": [],
}

# ============ 真实行情池（urllib 直连 Gamma） ============
MARKETS_LIVE = None   # fetch_poly_quotes 返回的实时二元盘口列表
MM_SET = []           # 当前做市标的 token 集合（固定，避免建仓不平仓）


def classify(q):
    """按题目文本把市场分到类别（复用 polymarket 关键词表）。"""
    ql = (q or "").lower()
    for tag, re_ in P._CAT_RE.items():
        if re_.search(ql):
            return tag
    return "other"


# 合规红线（中国部署，必须过滤政治/地缘/军事敏感市场）。polymarket._is_blocked
# 漏了 invade/iran 等措辞，这里补强。
BLOCK_EXTRA = ["iran", "invade", "invasion", "russia", "ukraine", "israel",
               "taiwan", "geopolit", "nuclear", "sanction", "election",
               "president", "putin", "trump", "biden", "xi ", "kremlin",
               "nato", "missile", "military", "war", "army", "gaza",
               "palestine", "china", "ccp", "communist",
               # 中东航运咽喉（涉伊朗/胡塞冲突，地缘敏感）
               "hormuz", "mandeb", "bab el-mandeb", "red sea", "yemen",
               "houthis", "houthi", "suez", "gulf", "opec"]
def is_blocked(q):
    return P._is_blocked(q, None) or any(k in (q or "").lower() for k in BLOCK_EXTRA)


def select_mm(rows):
    """从真实盘口池挑选做市标的：流动性够、价格居中、价差够赚。"""
    if not rows:
        return []
    cand = []
    for m in rows:
        if not isinstance(m, dict) or "error" in m:
            continue
        if is_blocked(m.get("question", "")):
            continue
        yb = m.get("yes_bid")
        ya = m.get("yes_ask")
        if yb is None or ya is None or yb <= 0 or ya <= yb:
            continue
        mid = (yb + ya) / 2.0
        sp = ya - yb
        liq = float(m.get("liquidity") or 0)
        if liq < 4000:
            continue
        if mid < 0.12 or mid > 0.88:
            continue
        if sp < 0.01:
            continue
        cand.append((liq, m))
    cand.sort(key=lambda x: -x[0])
    return [m for _, m in cand[:MM_N]]


# ============ 真实引擎实例 ============
book = RigorVirtualBook(rigor=rigor_params_from_config())
book._save = lambda: None                      # 实时循环不落盘，提速
book._record_volume = lambda *a, **k: None     # 禁日上限文件 I/O，提速
book._save_caps = lambda: None                 # 禁日成交上限持久化 I/O，提速
book.max_skew = 300                            # 允许 size 维度（生产真实上限 300）
book.fee_rate = 0.005                           # Polymarket 真实低交易费（做市赚价差为主）


def step():
    """跑一轮：刷新真实盘口(每~90s) -> 对固定做市标的集各调一次真实 market_make。"""
    global MARKETS_LIVE, MM_SET
    # 刷新真实盘口池
    refresh = False
    with LOCK:
        refresh = (STATE["round"] % MM_REFRESH == 0)
    if refresh or not MARKETS_LIVE:
        try:
            MARKETS_LIVE = P.fetch_poly_quotes(limit=300, force=True)
        except Exception:
            MARKETS_LIVE = MARKETS_LIVE or []
        # 重选做市标的（固定集合，直到下个刷新周期；避免建仓后掉出导致不平仓）
        MM_SET = [m["token_id"] for m in select_mm(MARKETS_LIVE)]
    by_tok = {m.get("token_id"): m for m in (MARKETS_LIVE or [])
              if isinstance(m, dict) and "error" not in m}
    size = STATE["params"]["size"]
    adverse = float(book.rigor.get("adverse_frac", 0.15))
    skew = float(book.rigor.get("inventory_skew", 0.0))
    quotes = {}
    round_pnl = 0.0
    for tok in MM_SET:
        m = by_tok.get(tok)
        if not m:
            continue
        qtext = m.get("question", "")
        if is_blocked(qtext):
            continue
        yb = m.get("yes_bid")
        ya = m.get("yes_ask")
        if yb is None or ya is None or yb <= 0 or ya <= yb:
            continue
        mid = (yb + ya) / 2.0
        spread = ya - yb
        opp = {
            "buy_ask": yb, "sell_bid": ya,
            "liquidity": m.get("liquidity") or 0,
            "buy_id": tok, "sell_id": tok,
            "question": qtext,
            "end_date": None,   # 同轮建平无持仓时间风险，不计时间衰减惩罚(extra)
            "buy_venue": "poly", "sell_venue": "poly",
        }
        # 同轮双边建平：先买建仓、再卖平仓，库存归零，纯捕获价差（零漂移风险）。
        # 这正是离线四维扫描正 EV 的真实对应——同轮盘口不变，锁利 = spread·(1-2·adverse)·size − fee。
        try:
            r1 = book.market_make(opp, size)
            if r1.get("ok") and isinstance(r1.get("pnl"), (int, float)):
                round_pnl += r1["pnl"]
        except Exception:
            pass
        try:
            r2 = book.market_make(opp, size)
            if r2.get("ok") and isinstance(r2.get("pnl"), (int, float)):
                round_pnl += r2["pnl"]
        except Exception:
            pass
        # 展示用：我们计算出的双边报价（与引擎同公式）
        off = skew * spread
        buy_base = yb + adverse * spread
        sell_base = ya - adverse * spread
        quotes[tok] = {
            "question": qtext[:62],
            "mid": round(mid, 4),
            "yes_bid": yb, "yes_ask": ya,
            "our_buy": round(buy_base, 4), "our_sell": round(sell_base, 4),
            "inv": 0, "liq": round(float(m.get("liquidity") or 0), 0),
        }
    # 快照到 STATE
    with LOCK:
        STATE["round"] += 1
        STATE["cash"] = round(book.cash, 2)
        STATE["realized"] = round(book.realized_pnl, 2)
        STATE["equity"] = round(book.cash, 2)   # 库存恒0，盯市权益=现金
        STATE["round_pnl"] = round(round_pnl, 2)
        STATE["n_markets"] = 0
        STATE["inv_notional"] = 0.0
        STATE["quotes"] = quotes
        STATE["live_count"] = len(MARKETS_LIVE) if MARKETS_LIVE else 0
        STATE["mm_count"] = len(MM_SET)
        STATE["last_refresh"] = time.time()
        # 最近成交（取引擎 positions 末尾，新->旧）
        pos = []
        for e in book.positions[-40:][::-1]:
            pos.append({
                "ts": round(e.get("ts", time.time()), 1),
                "mkt": (e.get("question") or e.get("mkt") or "-")[:26],
                "side": e.get("side", ""),
                "entry": e.get("entry"),
                "size": e.get("size"),
                "pnl": e.get("pnl"),
                "slip": e.get("slip"),
                "cash_after": e.get("cash_after"),
            })
        STATE["positions"] = pos
        # 限制引擎 positions 内存增长
        if len(book.positions) > 2000:
            book.positions = book.positions.__class__(list(book.positions)[-2000:])
        STATE["equity_curve"].append(round(STATE["equity"], 2))
        if len(STATE["equity_curve"]) > 600:
            STATE["equity_curve"].pop(0)


def loop():
    # 启动即拉一次真实盘口
    try:
        global MARKETS_LIVE
        MARKETS_LIVE = P.fetch_poly_quotes(limit=300, force=True)
    except Exception:
        pass
    while True:
        with LOCK:
            if not STATE["running"]:
                break
        step()
        time.sleep(1.2)


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Polymarket 实时模拟交易监视器 v3（真实盘口）</title>
<style>
:root{--bg:#0f1420;--panel:#192033;--ink:#e6ecf5;--mut:#9aa7bd;--red:#ff5b6e;--grn:#39d98a;--blue:#5aa9ff;--line:#2a3450;--amber:#e8c98a}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1200px 600px at 85% -10%,#16203a 0%,var(--bg) 60%);color:var(--ink);font:14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;min-height:100vh}
/* 顶部滚动行情条 */
.ticker{overflow:hidden;white-space:nowrap;background:#0a0f1a;border-bottom:1px solid var(--line);padding:7px 0;position:relative}
.ticker .run{display:inline-block;padding-left:100%;animation:marquee 45s linear infinite}
.ticker .run span{display:inline-block;padding:0 28px;font-size:13px;color:var(--mut)}
.ticker .run b{color:var(--ink);font-weight:600}
.ticker .run .up{color:var(--grn)}.ticker .run .dn{color:var(--red)}
@keyframes marquee{to{transform:translateX(-100%)}}
header{padding:16px 22px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;flex-wrap:wrap;background:linear-gradient(90deg,#141c30,#101627)}
header h1{margin:0;font-size:19px}
.gtitle{background:linear-gradient(90deg,#5aa9ff,#39d98a,#5aa9ff);background-size:200% auto;-webkit-background-clip:text;background-clip:text;color:transparent;animation:slide 5s linear infinite;font-weight:800}
@keyframes slide{to{background-position:200% center}}
.beat{width:10px;height:10px;border-radius:50%;background:var(--grn);box-shadow:0 0 10px var(--grn);animation:beat 1.1s infinite;flex:none}
.beat.hot{background:var(--amber);box-shadow:0 0 13px var(--amber);animation:beat .45s infinite}
@keyframes beat{0%,100%{transform:scale(1);opacity:1}30%{transform:scale(1.55);opacity:.55}}
.badge{font-size:12px;padding:3px 10px;border-radius:20px;background:#1f2940;color:var(--mut);border:1px solid var(--line);transition:all .3s}
.banner{background:linear-gradient(90deg,#16261c,#13251b);border:1px solid #2c5a3c;color:#a8e6c0;padding:8px 14px;margin:12px 22px 0;border-radius:8px;font-size:12.5px}
.wrap{padding:14px 22px;max-width:1240px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:12px 0}
.card{background:linear-gradient(160deg,var(--panel),#141b2c);border:1px solid var(--line);border-radius:12px;padding:13px 16px;transition:transform .25s,box-shadow .25s,border-color .25s}
.card:hover{transform:translateY(-3px);box-shadow:0 8px 22px rgba(90,169,255,.18);border-color:#34466e}
.card .k{color:var(--mut);font-size:12px}.card .v{font-size:21px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums;transition:color .4s}
.v.g{color:var(--grn)}.v.r{color:var(--red)}.v.b{color:var(--blue)}
.tabs{display:flex;gap:8px;margin:14px 0 4px}
.tab{background:#1f2940;border:1px solid var(--line);color:var(--mut);padding:8px 15px;border-radius:8px;cursor:pointer;font-size:13px;transition:all .2s}
.tab:hover{color:var(--ink)}
.tab.on{background:linear-gradient(90deg,#2c3a5e,#34487a);color:#fff;border-color:#3a4d7a;box-shadow:0 0 14px rgba(90,169,255,.25)}
.grid{display:grid;grid-template-columns:1.3fr 1fr;gap:16px}
@media(max-width:880px){.grid{grid-template-columns:1fr}}
.panel{background:linear-gradient(160deg,var(--panel),#141b2c);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:16px;animation:fade .35s ease}
@keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.panel h2{margin:0 0 10px;font-size:15px}
canvas{width:100%;height:240px;background:#0b1018;border:1px solid var(--line);border-radius:8px;display:block}
.scroll{max-height:420px;overflow:auto}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{border:1px solid var(--line);padding:5px 8px;text-align:center}
th{background:#1f2940;color:var(--mut);position:sticky;top:0;z-index:1}
td.l{text-align:left}.buy{color:var(--grn)}.sell{color:var(--red)}
/* 新成交行动画 */
@keyframes flashG{0%{background:rgba(57,217,138,.40)}100%{background:transparent}}
@keyframes flashR{0%{background:rgba(255,91,110,.34)}100%{background:transparent}}
tr.row-new.flash-up td{animation:flashG 1.2s ease-out}
tr.row-new.flash-down td{animation:flashR 1.2s ease-out}
.latest{display:flex;align-items:center;gap:10px;background:#0b1018;border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:12px;font-size:13px}
.latest .up{color:var(--grn);font-weight:700}.latest .down{color:var(--red);font-weight:700}
.note{color:var(--mut);font-size:12px;margin-top:8px}
select{background:#1f2940;color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:13px}
.tag{font-size:11px;padding:1px 7px;border-radius:10px;background:#27324d;color:var(--mut)}
</style></head>
<body>
<div class="ticker"><div class="run" id="tick">正在加载真实 Polymarket 盘口…</div></div>
<header>
  <span class="beat" id="beat"></span>
  <h1 class="gtitle">Polymarket 实时模拟交易监视器 v3</h1>
  <span class="badge" id="rnd">round 0</span>
  <span class="badge" id="live">真实盘口: 0</span>
  <span class="badge" id="status">● 连接中…</span>
  <span class="badge">引擎: RigorVirtualBook.market_make</span>
</header>
<div class="banner">✅ 行情来自<b>真实 Polymarket 盘口</b>（urllib 直连 Gamma，每 ~90s 刷新，已合规过滤政治/地缘/军事等敏感类）。每个真实市场由<b>验证过的做市引擎</b>在真实盘口上<b>同轮双边建平</b>（买@YES买价+adverse·spread，卖@YES卖价−adverse·spread，库存归零），纯捕获价差、零库存漂移风险。全程 <b>DRY_RUN 影子账本、零真钱</b>——演示真实行情下的策略表现，不等同于实盘结论。</div>
<div class="wrap">
  <div class="cards" id="cards"></div>
  <div class="tabs">
    <div class="tab on" data-t="live">📡 Polymarket 真实行情</div>
    <div class="tab" data-t="sim">🤖 模拟交易实时</div>
    <div class="tab" data-t="quote">📊 盘口 + 我们报价</div>
  </div>

  <div class="panel" id="p-live">
    <h2>Polymarket 真实行情榜（实时拉取）
      <select id="cat" style="float:right"><option value="all">全部类别</option><option value="crypto">crypto</option><option value="economy">economy</option><option value="finance">finance</option><option value="sports">sports</option><option value="tech">tech</option><option value="science">science</option><option value="entertainment">entertainment</option><option value="other">other</option></select>
    </h2>
    <div class="scroll"><table id="mkt"><thead><tr><th>市场(问题)</th><th>类别</th><th>YES 买</th><th>YES 卖</th><th>NO 买</th><th>NO 卖</th><th>流动性</th></tr></thead><tbody></tbody></table></div>
    <div class="note">YES = 结果代币隐含概率；买/卖为 Gamma 真实最优买卖盘口；流动性为该市场 USDC 深度。点其他 tab 看模拟成交。</div>
  </div>

  <div class="panel" id="p-sim" style="display:none">
    <div class="latest"><span class="beat" id="beat2"></span><span style="color:var(--mut)">最新成交：</span><span id="latest-txt">等待第一笔…</span></div>
    <div class="grid">
      <div><h2>盯市权益曲线 (实时)</h2><canvas id="cv" width="640" height="240"></canvas>
        <div class="note">equity = 现金 + 未平仓库存 × last_mid（含未实现盈亏）</div></div>
      <div><h2>本轮锁利 / 累计锁利</h2>
        <div class="card" style="margin-bottom:10px"><div class="k">本轮锁利</div><div class="v b" id="ninv">$0</div></div>
        <div class="card"><div class="k">累计锁利</div><div class="v" id="invn">$0</div></div>
      </div>
    </div>
    <h2>最近成交 (实时)</h2>
    <div class="scroll"><table id="trd"><thead><tr><th>时间</th><th>市场</th><th>方向</th><th>成交价</th><th>量</th><th>本笔锁利</th><th>滑点</th><th>现金</th></tr></thead><tbody></tbody></table></div>
    <div class="note">方向 BUY=建仓，SELL=对冲/平仓；仅平仓轮次有「本笔锁利」。每笔在真实盘口价位成交。</div>
  </div>

  <div class="panel" id="p-quote" style="display:none">
    <h2>真实盘口 + 我们的双边报价（本轮做市标的）</h2>
    <div class="scroll"><table id="qtab"><thead><tr><th>市场(问题)</th><th>真实 mid</th><th>YES 买</th><th>YES 卖</th><th>我们买</th><th>我们卖</th><th>库存</th><th>流动性</th></tr></thead><tbody></tbody></table></div>
    <div class="note">绿=我们买价(yes_bid+adverse·spread)，红=我们卖价(yes_ask−adverse·spread)，含库存偏置；库存≠0 时双边推离 mid 抑制追单、鼓励平仓。</div>
  </div>
</div>
<script>
function fmt(n){return (n==null)?'-':Number(n).toLocaleString('en-US',{maximumFractionDigits:2})}
function money(n){return '$'+Number(n).toLocaleString('en-US',{maximumFractionDigits:2})}
function smoney(n){return (n>=0?'+$':'-$')+Number(Math.abs(n)).toLocaleString('en-US',{maximumFractionDigits:2})}
// 数字滚动动画
function tween(el,to,render){
  const from=el._cur!=null?el._cur:to; el._cur=to;
  const dur=550,t0=performance.now();
  if(el._raf)cancelAnimationFrame(el._raf);
  function step(now){const k=Math.min(1,(now-t0)/dur),e=1-Math.pow(1-k,3),v=from+(to-from)*e;
    el.textContent=render(v); if(k<1)el._raf=requestAnimationFrame(step);}
  el._raf=requestAnimationFrame(step);
}
function setMoney(id,val,signed){
  const el=document.getElementById(id); if(!el)return;
  el.className='v '+(val>0?'g':val<0?'r':'b');
  tween(el,val, signed?smoney:money);
}
function setNum(id,val){
  const el=document.getElementById(id); if(!el)return;
  el.className='v b'; tween(el,val,v=>Number(v).toLocaleString('en-US',{maximumFractionDigits:0}));
}
// 卡片骨架（只建一次，后续只更新数值，保证滚动动画连续）
document.getElementById('cards').innerHTML=
  ['<div class="card"><div class="k">轮次</div><div class="v b" id="c-round">0</div></div>',
   '<div class="card"><div class="k">现金</div><div class="v b" id="c-cash">$0</div></div>',
   '<div class="card"><div class="k">累计锁利</div><div class="v b" id="c-real">$0</div></div>',
   '<div class="card"><div class="k">本轮锁利</div><div class="v b" id="c-rpnl">$0</div></div>',
   '<div class="card"><div class="k">盯市权益</div><div class="v b" id="c-eq">$0</div></div>',
   '<div class="card"><div class="k">真实盘口</div><div class="v b" id="c-live">0</div></div>',
   '<div class="card"><div class="k">做市市场</div><div class="v b" id="c-mm">0</div></div>'].join('');
let curTab='live', seen=new Set(), prevRound=0;
function flashBeat(id){const b=document.getElementById(id); if(!b)return; b.classList.add('hot'); setTimeout(()=>b.classList.remove('hot'),650);}
function tickState(){
  fetch('/api/state').then(r=>r.json()).then(s=>{
    document.getElementById('rnd').textContent='round '+s.round;
    document.getElementById('live').textContent='真实盘口 '+s.live_count+' · 做市 '+s.mm_count;
    const st=document.getElementById('status');
    if(s.round!==prevRound){prevRound=s.round; st.textContent='● 撮合中 · round '+s.round; flashBeat('beat');}
    else st.textContent='● 实时运行 · round '+s.round;
    const eq=s.equity;
    setNum('c-round',s.round); setMoney('c-cash',s.cash,false);
    setMoney('c-real',s.realized,true); setMoney('c-rpnl',s.round_pnl,true);
    setMoney('c-eq',eq,false); setNum('c-live',s.live_count); setNum('c-mm',s.mm_count);
    document.getElementById('ninv').textContent=(s.round_pnl>=0?'+$':'-$')+fmt(Math.abs(s.round_pnl));
    document.getElementById('invn').textContent=(s.realized>=0?'+$':'-$')+fmt(Math.abs(s.realized));
    // 权益曲线（渐变填充 + 末端发光点）
    const cv=document.getElementById('cv'),ctx=cv.getContext('2d'),W=cv.width,H=cv.height;ctx.clearRect(0,0,W,H);
    const ec=s.equity_curve;
    if(ec.length>1){
      const mn=Math.min.apply(null,ec),mx=Math.max.apply(null,ec),pad=(mx-mn)*0.18||1,lo=mn-pad,hi=mx+pad;
      ctx.strokeStyle='#2a3450';ctx.beginPath();ctx.moveTo(0,H/2);ctx.lineTo(W,H/2);ctx.stroke();
      const X=i=>i/(ec.length-1)*W, Y=v=>H-((v-lo)/(hi-lo))*H;
      ctx.beginPath();ctx.moveTo(0,Y(ec[0]));
      ec.forEach((v,i)=>ctx.lineTo(X(i),Y(v)));
      const grad=ctx.createLinearGradient(0,0,0,H);
      grad.addColorStop(0,'rgba(57,217,138,.30)');grad.addColorStop(1,'rgba(57,217,138,0)');
      ctx.lineTo(W,H);ctx.lineTo(0,H);ctx.closePath();ctx.fillStyle=grad;ctx.fill();
      ctx.strokeStyle=eq>=10000?'#39d98a':'#ff5b6e';ctx.lineWidth=2.2;ctx.beginPath();
      ec.forEach((v,i)=>{i?ctx.lineTo(X(i),Y(v)):ctx.moveTo(X(i),Y(v));});ctx.stroke();
      const lx=X(ec.length-1),ly=Y(ec[ec.length-1]);
      ctx.beginPath();ctx.arc(lx,ly,6,0,7);ctx.fillStyle=eq>=10000?'#39d98a':'#ff5b6e';ctx.shadowColor=ctx.fillStyle;ctx.shadowBlur=12;ctx.fill();ctx.shadowBlur=0;
      ctx.fillStyle='#9aa7bd';ctx.font='11px sans-serif';ctx.fillText('$'+fmt(hi),4,12);ctx.fillText('$'+fmt(lo),4,H-4);
    }
    if(curTab==='sim'){
      const t2=document.getElementById('trd').querySelector('tbody');
      let fresh=0;
      const rows=s.positions.map(t=>{
        const key=t.ts+'|'+t.mkt+'|'+t.side+'|'+t.entry;
        const isNew=!seen.has(key); if(isNew){seen.add(key);fresh++;}
        if(seen.size>400)seen.clear();
        const cls=(t.side==='buy'?'flash-up':'flash-down');
        return `<tr class="${isNew?'row-new '+cls:cls}"><td>${t.ts}</td><td class="l">${t.mkt}</td><td class="${t.side==='buy'?'buy':'sell'}">${(t.side||'').toUpperCase()}</td><td>${t.entry!=null?Number(t.entry).toFixed(4):'-'}</td><td>${t.size}</td><td>${t.pnl!=null?'$'+fmt(t.pnl):'-'}</td><td>${t.slip!=null?Number(t.slip).toFixed(4):'-'}</td><td>$${fmt(t.cash_after)}</td></tr>`;
      }).join('');
      t2.innerHTML=rows;
      if(fresh>0){
        flashBeat('beat2');
        const t=s.positions[0];
        if(t){
          const up=t.pnl!=null && t.pnl>0;
          document.getElementById('latest-txt').innerHTML=
            `<b class="${t.side==='buy'?'buy':'sell'}">${t.side.toUpperCase()}</b> ${t.mkt} @${Number(t.entry).toFixed(4)} `+
            (t.pnl!=null?`· 锁利 <span class="${up?'up':'down'}">$${fmt(t.pnl)}</span>`:'· 建仓');
        }
      }
    }
    if(curTab==='quote'){
      const qt=document.getElementById('qtab').querySelector('tbody');
      qt.innerHTML=Object.keys(s.quotes).map(k=>{const q=s.quotes[k];
        return `<tr><td class="l">${q.question}</td><td>${q.mid.toFixed(4)}</td><td>${q.yes_bid.toFixed(4)}</td><td>${q.yes_ask.toFixed(4)}</td><td class="buy">${q.our_buy.toFixed(4)}</td><td class="sell">${q.our_sell.toFixed(4)}</td><td>${q.inv}</td><td>$${fmt(q.liq)}</td></tr>`;}).join('');
    }
  }).catch(()=>{});
}
let liveCache=null;
function tickLive(){
  if(curTab!=='live') return;
  fetch('/api/markets').then(r=>r.json()).then(d=>{
    liveCache=d; renderLive();
    const items=d.markets.slice(0,60).map(m=>{
      const up=Number(m.yes_ask)>=Number(m.yes_bid);
      return `<span><b>${m.question.slice(0,42)}</b> YES <span class="${up?'up':'dn'}">${Number(m.yes_bid).toFixed(3)}/${Number(m.yes_ask).toFixed(3)}</span> · 量 $${fmt(m.liquidity)}</span>`;
    }).join('');
    document.getElementById('tick').innerHTML=items+items;
  }).catch(()=>{});
}
function renderLive(){
  if(!liveCache) return;
  const cat=document.getElementById('cat').value;
  const rows=liveCache.markets.filter(m=>cat==='all'||m.tag===cat).slice(0,120);
  const tb=document.getElementById('mkt').querySelector('tbody');
  tb.innerHTML=rows.map(m=>`<tr><td class="l">${m.question}</td><td><span class="tag">${m.tag}</span></td><td class="buy">${Number(m.yes_bid).toFixed(4)}</td><td class="sell">${Number(m.yes_ask).toFixed(4)}</td><td class="buy">${Number(m.no_bid).toFixed(4)}</td><td class="sell">${Number(m.no_ask).toFixed(4)}</td><td>$${fmt(m.liquidity)}</td></tr>`).join('');
}
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  curTab=t.dataset.t;
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
  t.classList.add('on');
  document.getElementById('p-live').style.display=curTab==='live'?'block':'none';
  document.getElementById('p-sim').style.display=curTab==='sim'?'block':'none';
  document.getElementById('p-quote').style.display=curTab==='quote'?'block':'none';
  if(curTab==='live') tickLive();
});
document.getElementById('cat').onchange=renderLive;
setInterval(tickState,2000); setInterval(tickLive,30000);
tickState(); tickLive();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/state":
            with LOCK:
                snap = {
                    "round": STATE["round"], "cash": STATE["cash"],
                    "realized": STATE["realized"], "equity": STATE["equity"],
                    "round_pnl": STATE["round_pnl"],
                    "n_markets": STATE["n_markets"], "inv_notional": STATE["inv_notional"],
                    "live_count": STATE["live_count"], "mm_count": STATE["mm_count"],
                    "params": STATE["params"], "quotes": STATE["quotes"],
                    "positions": STATE["positions"], "equity_curve": STATE["equity_curve"],
                }
            body = json.dumps(snap, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/markets":
            rows = MARKETS_LIVE or []
            out = []
            for m in rows:
                if not isinstance(m, dict) or "error" in m:
                    continue
                q = m.get("question") or ""
                if is_blocked(q):
                    continue
                out.append({
                    "question": (q[:90] + ("…" if len(q) > 90 else "")),
                    "tag": classify(q),
                    "yes_bid": m.get("yes_bid"), "yes_ask": m.get("yes_ask"),
                    "no_bid": m.get("no_bid"), "no_ask": m.get("no_ask"),
                    "liquidity": round(float(m.get("liquidity") or 0), 0),
                    "token_id": str(m.get("token_id")),
                })
            body = json.dumps({"ts": time.time(), "count": len(out),
                               "markets": out}, ensure_ascii=False).encode("utf-8")
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
    print("[sim_server v3] listening on http://127.0.0.1:%d  (Ctrl+C to stop)" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        with LOCK:
            STATE["running"] = False
        srv.shutdown()


if __name__ == "__main__":
    main()

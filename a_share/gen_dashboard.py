# -*- coding: utf-8 -*-
"""生成单文件可交互模拟器看板 sim_dashboard.html（零外部依赖）。

数据源（全部为本机已产出的真实结果）：
  - mm_sweep4d_results.json  四维参数扫描（mm x adv x tick x size）
  - live_preflight_report.json  实时行情 DRY_RUN 预飞（104 单 / 832 校验）
  - mm_equity_curve.svg       盯市权益曲线
  - READINESS_REPORT.md       就绪度（关键点硬编码进看板）
"""
import json, base64, os

ROOT = os.path.dirname(os.path.abspath(__file__))
A = ROOT

sweep = json.load(open(os.path.join(A, "mm_sweep4d_results.json"), encoding="utf-8"))
pref = json.load(open(os.path.join(A, "live_preflight_report.json"), encoding="utf-8"))

svg_b64 = ""
svgp = os.path.join(A, "mm_equity_curve.svg")
if os.path.exists(svgp):
    svg_b64 = base64.b64encode(open(svgp, "rb").read()).decode("ascii")

# 指标概览（Python 端算，避免 JS 复杂）
mms = list(sweep.keys())
n_total = n_robust = n_neg = 0
for mm in sweep:
    for adv in sweep[mm]:
        for tick in sweep[mm][adv]:
            for size in sweep[mm][adv][tick]:
                r = sweep[mm][adv][tick][size]
                if not r:
                    continue
                n_total += 1
                if r["ci_low"] > 0:
                    n_robust += 1
                elif r["ev"] < 0:
                    n_neg += 1
robust_pct = round(100.0 * n_robust / n_total, 1) if n_total else 0
cs = pref.get("checksums", {})
overview = {
    "n_total": n_total, "n_robust": n_robust, "robust_pct": robust_pct, "n_neg": n_neg,
    "mode": pref.get("mode", ""),
    "total_orders": cs.get("total_orders", 0),
    "check_pass": cs.get("check_pass", 0),
    "check_fail": cs.get("check_fail", 0),
    "check_na": cs.get("check_na", 0),
    "final_usdc": cs.get("final_usdc", 0),
    "start_usdc": pref.get("start_usdc", 0),
}

TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Polymarket MM/套利 模拟器看板</title>
<style>
  :root{ --bg:#0f1420; --panel:#192033; --panel2:#1f2940; --ink:#e6ecf5; --mut:#9aa7bd;
         --red:#ff5b6e; --grn:#39d98a; --yel:#ffd166; --blue:#5aa9ff; --line:#2a3450; }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,"Segoe UI",Roboto,"Microsoft YaHei",sans-serif}
  header{padding:18px 22px;border-bottom:1px solid var(--line);display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
  header h1{margin:0;font-size:20px}
  .badge{font-size:12px;padding:3px 10px;border-radius:20px;background:var(--panel2);color:var(--mut);border:1px solid var(--line)}
  .badge.live{color:#fff;background:#2a6df0;border-color:#2a6df0}
  nav{display:flex;gap:6px;padding:10px 22px;border-bottom:1px solid var(--line);flex-wrap:wrap}
  nav button{background:var(--panel);color:var(--mut);border:1px solid var(--line);padding:7px 14px;border-radius:8px;cursor:pointer;font-size:13px}
  nav button.on{background:var(--blue);color:#06101f;border-color:var(--blue);font-weight:600}
  .wrap{padding:20px 22px;max-width:1180px;margin:0 auto}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:18px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
  .card .k{color:var(--mut);font-size:12px}
  .card .v{font-size:24px;font-weight:700;margin-top:4px}
  .card .v.g{color:var(--grn)} .card .v.r{color:var(--red)} .card .v.y{color:var(--yel)} .card .v.b{color:var(--blue)}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:16px}
  .panel h2{margin:0 0 12px;font-size:16px}
  .row{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
  select{background:var(--panel2);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:13px}
  label{color:var(--mut);font-size:13px}
  table{border-collapse:collapse;width:100%;font-size:13px}
  th,td{border:1px solid var(--line);padding:6px 9px;text-align:center}
  th{background:var(--panel2);color:var(--mut);position:sticky;top:0}
  td.l{text-align:left}
  .hl{font-weight:700}
  .ok{color:var(--grn)} .bad{color:var(--red)} .na{color:var(--mut)}
  .heat td{cursor:default}
  .heat td:hover{outline:2px solid var(--blue)}
  input[type=text]{background:var(--panel2);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:13px;min-width:220px}
  .legend{display:flex;gap:10px;align-items:center;margin:8px 0;color:var(--mut);font-size:12px}
  .sw{width:14px;height:14px;border-radius:3px;display:inline-block;vertical-align:-2px}
  .note{color:var(--mut);font-size:12px;margin-top:8px;line-height:1.7}
  .scroll{max-height:430px;overflow:auto}
  .pill{font-size:11px;padding:2px 8px;border-radius:20px;border:1px solid var(--line)}
  ul.rd{margin:6px 0 0;padding-left:18px;line-height:1.8}
  ul.rd li{margin:2px 0}
  img.curve{width:100%;background:#0b1018;border:1px solid var(--line);border-radius:8px}
  .hidden{display:none}
</style>
</head>
<body>
<header>
  <h1>Polymarket MM / 套利 模拟器看板</h1>
  <span class="badge" id="modeBadge"></span>
  <span class="badge">本机真实模拟结果 · 零真钱</span>
</header>
<nav>
  <button data-tab="t1" class="on">① 参数扫描热力图</button>
  <button data-tab="t2">② 实时预飞校验</button>
  <button data-tab="t3">③ 盯市权益曲线</button>
  <button data-tab="t4">④ 实战就绪度</button>
</nav>

<div class="wrap">
  <div class="cards" id="cards"></div>

  <section id="t1" class="panel">
    <h2>四维参数扫描 · 期望收益(EV/轮) 热力图</h2>
    <div class="row">
      <label>最小价差 mm_min_spread:</label>
      <select id="selMM"></select>
      <label>下单规模 size:</label>
      <select id="selSize"></select>
      <span class="note" id="heatHint"></span>
    </div>
    <div class="legend">
      颜色: <span class="sw" style="background:var(--red)"></span>负EV
      <span class="sw" style="background:var(--yel)"></span>≈0
      <span class="sw" style="background:var(--grn)"></span>正EV(越绿越稳) ·
      单元格 = EV/轮；悬停看胜率与置信区间
    </div>
    <div id="heatWrap" class="scroll"></div>
    <div class="note">说明:adverse_frac 越低、价差越宽 → 越稳健。绿色单元格 = 95% 置信区间下界 &gt; 0（统计显著正期望）。</div>
  </section>

  <section id="t2" class="panel hidden">
    <h2>实时行情 DRY_RUN 预飞 · 订单构造校验</h2>
    <div class="row">
      <label>过滤市场:</label>
      <input type="text" id="filtMkt" placeholder="留空=全部；可输入 market_id 或关键词"/>
      <span class="note">校验项: token_id格式 / price合法 / tickSize / side / 重复单幂等 / 影子成交 / 余额 / 价格区间护栏</span>
    </div>
    <div class="scroll">
      <table id="prefTable">
        <thead><tr><th>市场ID</th><th>问题</th><th>腿</th><th>方向</th><th>价</th><th>量</th><th>tick</th><th>校验</th><th>影子成交</th><th>余额余</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </section>

  <section id="t3" class="panel hidden">
    <h2>盯市权益曲线 (equity_marked)</h2>
    <img class="curve" src="data:image/svg+xml;base64,__SVG_B64__" alt="equity curve"/>
    <div class="note">来源: mm_reconcile 真实对账（已实现锁利 + 未平仓库存按 last_mid 盯市）。</div>
  </section>

  <section id="t4" class="panel hidden">
    <h2>实战就绪度 (READINESS)</h2>
    <ul class="rd">
      <li>🟢 <b>MM 做市算法</b>: 买腿吃完整价差、库存偏置报价、盯市权益 — 最窄门槛即正期望。</li>
      <li>🟢 <b>四维扫描</b>: __ROBUST__ 个组合稳健正 EV(95% CI 下界&gt;0)，生产默认 mm=0.02 落在稳健区。</li>
      <li>🟢 <b>DRY_RUN 端到端</b>: 影子账本成交、对账自洽，零网络零真钱。</li>
      <li>🟢 <b>实时行情预飞</b>: __ORDERS__ 张真实订单 / __PASS__ 项校验 PASS / __FAIL__ 项 FAIL（LIVE 模式直连 Gamma）。</li>
      <li>🟢 <b>纯套利自动执行链</b>: 合成真划分验证可端到端运行（逐 token 下单，非合成单）。</li>
      <li>🟢 <b>对账/熔断</b>: daily 对账 + 幂等去重 + 资金阈值 + 重试。</li>
      <li>🔴 <b>接真钱剩余缺口</b>(均非本机可补): ① 翻转 DRY_RUN=0 ② 设 POLY_PK(+可选 POLY_FUNDER) ③ EOA 一次性 approve USDC+条件代币给 3 合约 ④ 限额 $50–100 起步盯对账。</li>
    </ul>
    <div class="note">详细见 a_share/READINESS_REPORT.md 与 LIVE_FIRST_RUN_SOP.md。本看板为静态快照，数据来自本机已完成的模拟运行。</div>
  </section>
</div>

<script>
const SWEEP = __SWEEP_DATA__;
const PREF = __PREF_DATA__;
const OV = __OVERVIEW__;

function el(id){return document.getElementById(id);}

// 指标卡
(function(){
  el('modeBadge').textContent = (OV.mode==='LIVE'?'● LIVE 实时行情':'○ DRY_RUN 影子');
  if(OV.mode==='LIVE') el('modeBadge').classList.add('live');
  const cards=[
    {k:'扫描组合数',v:OV.n_total,b:''},
    {k:'稳健正EV(ci_low>0)',v:OV.n_robust+' ('+OV.robust_pct+'%)',b:'g'},
    {k:'负EV组合',v:OV.n_neg,b:OV.n_neg?'r':'g'},
    {k:'预飞订单数',v:OV.total_orders,b:'b'},
    {k:'校验 PASS / FAIL',v:OV.check_pass+' / '+OV.check_fail,b:OV.check_fail?'r':'g'},
    {k:'预飞余额变化',v:'$'+OV.start_usdc+'→$'+OV.final_usdc,b:''},
  ];
  el('cards').innerHTML = cards.map(c=>`<div class="card"><div class="k">${c.k}</div><div class="v ${c.b}">${c.v}</div></div>`).join('');
})();

// Tab 切换
document.querySelectorAll('nav button').forEach(b=>{
  b.onclick=()=>{
    document.querySelectorAll('nav button').forEach(x=>x.classList.remove('on'));
    b.classList.add('on');
    ['t1','t2','t3','t4'].forEach(t=>el(t).classList.toggle('hidden', t!==b.dataset.tab));
  };
});

// ---- 扫描热力图 ----
const mms=Object.keys(SWEEP);
const advs=[...new Set([].concat(...mms.map(m=>Object.keys(SWEEP[m])))].map(Number)).sort((a,b)=>a-b);
const ticks=[...new Set([].concat(...mms.map(m=>[].concat(...Object.keys(SWEEP[m]).map(a=>Object.keys(SWEEP[m][a])))))].map(Number)).sort((a,b)=>a-b);
const sizes=[...new Set([].concat(...mms.map(m=>[].concat(...Object.keys(SWEEP[m]).map(a=>[].concat(...Object.keys(SWEEP[m][a]).map(t=>Object.keys(SWEEP[m][a][t])))))])).map(Number)].sort((a,b)=>a-b);

function fillSel(sel,arr,sel0){ sel.innerHTML=arr.map(x=>`<option ${String(x)===String(sel0)?'selected':''}>${x}</option>`).join(''); }
fillSel(el('selMM'),mms,mms[Math.max(0,mms.length-3)]);
fillSel(el('selSize'),sizes,sizes[sizes.length-1]);

function colorFor(ev){
  if(ev<0) return `rgba(255,91,110,${Math.min(0.9,0.25+Math.abs(ev)/4)})`;
  if(ev<0.3) return `rgba(255,209,102,${0.35+ev})`;
  return `rgba(57,217,138,${Math.min(0.92,0.35+ev/6)})`;
}
function getR(mm,adv,tick,size){ return (SWEEP[mm]&&SWEEP[mm][adv]&&SWEEP[mm][adv][tick]&&SWEEP[mm][adv][tick][size])||null; }

function renderHeat(){
  const mm=el('selMM').value, size=el('selSize').value;
  let html='<table class="heat"><thead><tr><th>adv \\ tick</th>'+ticks.map(t=>`<th>${t}</th>`).join('')+'</tr></thead><tbody>';
  let mn=1e9,mx=-1e9;
  advs.forEach(a=>ticks.forEach(t=>{const r=getR(mm,a,t,size); if(r){mn=Math.min(mn,r.ev);mx=Math.max(mx,r.ev);}}));
  advs.forEach(a=>{
    html+=`<tr><th>${a}</th>`;
    ticks.forEach(t=>{
      const r=getR(mm,a,t,size);
      if(!r){html+='<td style="color:var(--mut)">—</td>';return;}
      const ev=r.ev, w=r.win*100, cl=colorFor(ev);
      html+=`<td style="background:${cl}" title="mm=${mm} adv=${a} tick=${t} size=${size}\nEV=${ev.toFixed(3)}/轮  胜率=${w.toFixed(1)}%\n95%CI=[${r.ci_low.toFixed(3)}, ${r.ci_high.toFixed(3)}]\nmed=${r.med.toFixed(3)}  区间=[${r.pnl_min.toFixed(2)}, ${r.pnl_max.toFixed(2)}]">${ev.toFixed(2)}</td>`;
    });
    html+='</tr>';
  });
  html+='</tbody></table>';
  el('heatWrap').innerHTML=html;
  el('heatHint').textContent=`当前 mm=${mm}, size=${size} | EV 范围 [${mn.toFixed(2)}, ${mx.toFixed(2)}]`;
}
el('selMM').onchange=renderHeat; el('selSize').onchange=renderHeat;
renderHeat();

// ---- 预飞表 ----
function renderPref(){
  const q=(el('filtMkt').value||'').trim().toLowerCase();
  const rows=PREF.orders.filter(o=>{
    if(!q) return true;
    return (o.market_id||'').toLowerCase().includes(q) || (o.question||'').toLowerCase().includes(q);
  });
  const tb=el('prefTable').querySelector('tbody');
  tb.innerHTML=rows.map(o=>{
    const fails=(o.checks||[]).filter(c=>c.status==='FAIL').length;
    const nas=(o.checks||[]).filter(c=>c.status==='NA').length;
    const np=(o.checks||[]).length;
    const chk=fails?`<span class="bad">${fails} FAIL</span>`:(nas?`<span class="na">${np-nas}/${np} PASS (${nas} NA)</span>`:`<span class="ok">${np} PASS</span>`);
    const sf=o.shadow_fill||{};
    const fill=sf.ok?`<span class="ok">@${Number(sf.avg_fill||0).toFixed(4)}</span>`:`<span class="bad">✗</span>`;
    return `<tr><td>${o.market_id}</td><td class="l">${(o.question||'').slice(0,40)}</td><td>${o.leg}</td><td>${o.side}</td><td>${o.price}</td><td>${o.size}</td><td>${o.tick}</td><td>${chk}</td><td>${fill}</td><td>$${Number(o.cash_after||0).toFixed(2)}</td></tr>`;
  }).join('');
}
el('filtMkt').oninput=renderPref;
renderPref();
</script>
</body>
</html>
"""

html = (TEMPLATE
        .replace("__SWEEP_DATA__", json.dumps(sweep, ensure_ascii=False))
        .replace("__PREF_DATA__", json.dumps(pref, ensure_ascii=False))
        .replace("__OVERVIEW__", json.dumps(overview, ensure_ascii=False))
        .replace("__SVG_B64__", svg_b64)
        .replace("__ROBUST__", str(overview["n_robust"]))
        .replace("__ORDERS__", str(overview["total_orders"]))
        .replace("__PASS__", str(overview["check_pass"]))
        .replace("__FAIL__", str(overview["check_fail"])))

out = os.path.join(A, "sim_dashboard.html")
with open(out, "w", encoding="utf-8") as f:
    f.write(html)
print("WROTE", out, "bytes=", len(html))
print("overview=", overview)

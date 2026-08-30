# -*- coding: utf-8 -*-
"""
生成「实况 + 统计」报告，一次输出两份：
  - HTML：暗色主题、自包含，双击即可阅读（用于看）
  - Markdown：纯文本表格，便于归档到 Obsidian / 贴进笔记（用于存）

用法：
    .venv/Scripts/python.exe a_share/sim_report.py
    .venv/Scripts/python.exe a_share/sim_report.py --out-dir output --top 15
"""
import argparse
import datetime
import json
import os
import sys
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
BASE = "http://127.0.0.1:8787"


def _get(path, base):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base + path, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def _money(v):
    try:
        return "$" + format(float(v), ",.2f")
    except Exception:
        return "-"


def _sgn(v):
    try:
        v = float(v)
        return ("+$" if v >= 0 else "-$") + format(abs(v), ",.2f")
    except Exception:
        return "-"


def _num(v, nd=0):
    try:
        return format(float(v), ",.%df" % nd)
    except Exception:
        return "-"


def _cls(v):
    """中国习惯：盈利/涨=红(up)，亏损/跌=绿(dn)。"""
    try:
        return "up" if float(v) >= 0 else "dn"
    except Exception:
        return ""


# ------------------------------ HTML ------------------------------
CSS = """
:root{--bg:#070a0f;--panel:#111824;--ink:#dfe7f2;--mut:#7e8aa0;--line:#1c2738;
--up:#ff5b6e;--dn:#2ee6a6;--acc:#46b0ff;--gold:#e8c98a}
*{box-sizing:border-box}
body{margin:0;padding:0 0 40px;background:var(--bg);color:var(--ink);
font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1120px;margin:0 auto;padding:0 26px}
header{padding:26px 0 16px;border-bottom:1px solid var(--line);margin-bottom:22px}
h1{margin:0 0 6px;font-size:23px;font-weight:700}
.sub{color:var(--mut);font-size:13px}
h2{font-size:16px;margin:30px 0 12px;padding-left:11px;border-left:3px solid var(--acc);font-weight:600}
h3{font-size:14px;color:var(--mut);margin:18px 0 8px;font-weight:500}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:16px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:13px 15px}
.card .k{color:var(--mut);font-size:12px}
.card .v{font-size:20px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums}
.up{color:var(--up)}.dn{color:var(--dn)}.b{color:var(--acc)}.gold{color:var(--gold)}
table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 4px}
th,td{border:1px solid var(--line);padding:7px 10px;text-align:right}
th{background:#101a28;color:var(--mut);text-align:right;font-weight:500}
td:first-child,th:first-child{text-align:left}
tr:nth-child(even) td{background:#0c131d}
.note{color:var(--mut);font-size:12.5px;line-height:1.7;margin-top:8px}
.warn{background:#1b1207;border:1px solid #3a2a10;color:#e8c98a;padding:12px 15px;
border-radius:9px;font-size:13px;line-height:1.75;margin-top:10px}
.warn b{color:#f3c96b}
.box{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:14px 16px;margin:12px 0}
.kv{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px dashed var(--line);font-size:13px}
.kv:last-child{border-bottom:none}
.kv .k{color:var(--mut)}
.kv .v{font-variant-numeric:tabular-nums}
footer{margin-top:34px;padding-top:14px;border-top:1px solid var(--line);color:var(--mut);font-size:12px}
@media print{body{background:#fff;color:#111}
:root{--bg:#fff;--panel:#f7f8fa;--ink:#111;--mut:#555;--line:#ccc}}
"""


def build_html(st, s, mkts, top_n, ts):
    f = st.get("fill") or {}
    mode_txt = "真实做市（库存管理）" if st.get("mode") == "inv" else "同轮双边建平（乐观对照）"
    fills_on = f.get("on", False)
    fill_rate = f.get("rate", 0) if fills_on else 100.0

    def card(k, v, cls="b"):
        return '<div class="card"><div class="k">%s</div><div class="v %s">%s</div></div>' % (k, cls, v)

    cards = "".join([
        card("盯市权益", _money(st.get("equity")), "b"),
        card("累计锁利", _sgn(s.get("realized")), _cls(s.get("realized"))),
        card("浮动盈亏", _sgn(st.get("unrealized")), _cls(st.get("unrealized"))),
        card("峰值盈利", _sgn(s.get("peak_profit")), _cls(s.get("peak_profit"))),
        card("历史最大回撤", "%.2f%%" % s.get("max_drawdown_pct", 0), "dn"),
        card("挂单成交率", "%.1f%%" % fill_rate, "b"),
        card("胜率(平仓)", "%.1f%%" % s.get("win_rate", 0), "b"),
        card("总成交", _num(s.get("trades_total")), "b"),
    ])

    # 按类别
    tag_rows = ""
    for k, v in sorted((s.get("per_tag") or {}).items(),
                       key=lambda kv: -kv[1]["pnl"]):
        tag_rows += ("<tr><td>%s</td><td>%s</td><td>%s</td><td class='%s'>%s</td></tr>"
                     % (k, _num(v.get("n")), _num(v.get("win")),
                        _cls(v.get("pnl")), _sgn(v.get("pnl"))))

    # 按日
    day_rows = ""
    for k, v in sorted((s.get("per_day") or {}).items()):
        day_rows += "<tr><td>%s</td><td class='%s'>%s</td></tr>" % (k, _cls(v), _sgn(v))

    # 当前敞口
    inv_rows = ""
    qs = st.get("quotes") or {}
    opens = [(k, v) for k, v in qs.items() if int(v.get("inv") or 0) != 0]
    opens.sort(key=lambda kv: -abs(int(kv[1].get("inv") or 0)))
    if opens:
        for k, v in opens:
            inv = int(v.get("inv") or 0)
            inv_rows += ("<tr><td>%s</td><td class='%s'>%d</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                         % (str(v.get("question"))[:40], _cls(inv), inv,
                            _num(v.get("mid"), 4), _num(v.get("yes_bid"), 4),
                            _num(v.get("yes_ask"), 4)))
    else:
        inv_rows = "<tr><td colspan='5' style='text-align:center;color:var(--mut)'>当前无未平仓敞口</td></tr>"

    # 盘口样本
    mkt_rows = ""
    for m in (mkts.get("markets") or [])[:top_n]:
        mkt_rows += ("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                     % (str(m.get("question"))[:52], m.get("tag"),
                        _num(m.get("yes_bid"), 4), _num(m.get("yes_ask"), 4),
                        _num(m.get("no_bid"), 4), _money(m.get("liquidity"))))

    bt = s.get("best_trade") or {}
    wt = s.get("worst_trade") or {}

    html = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Polymarket 模拟盘实况与统计报告 · %s</title>
<style>%s</style></head><body><div class="wrap">
<header>
  <h1>Polymarket 模拟盘 · 实况与统计报告</h1>
  <div class="sub">生成时间 %s &nbsp;·&nbsp; 运行起点 %s &nbsp;·&nbsp; 已运行 %s 分钟
  &nbsp;·&nbsp; 模式：%s</div>
</header>

<h2>核心指标</h2>
<div class="cards">%s</div>

<h2>资金与盈亏</h2>
<div class="box">
  <div class="kv"><span class="k">初始权益</span><span class="v">%s</span></div>
  <div class="kv"><span class="k">当前现金</span><span class="v">%s</span></div>
  <div class="kv"><span class="k">盯市权益（现金 + 库存按市价）</span><span class="v b">%s</span></div>
  <div class="kv"><span class="k">已实现盈亏（全部平仓笔之和）</span><span class="v %s">%s</span></div>
  <div class="kv"><span class="k">浮动盈亏（未平仓部分）</span><span class="v %s">%s</span></div>
  <div class="kv"><span class="k">权益峰值</span><span class="v">%s</span></div>
  <div class="kv"><span class="k">峰值盈利</span><span class="v %s">%s</span></div>
  <div class="kv"><span class="k">当前回撤 / 历史最大回撤</span><span class="v">%.2f%% / %.2f%%</span></div>
  <div class="kv"><span class="k">未平仓敞口名义</span><span class="v">%s（%d 个市场）</span></div>
</div>

<h2>成交统计</h2>
<div class="box">
  <div class="kv"><span class="k">总成交笔数</span><span class="v">%s</span></div>
  <div class="kv"><span class="k">建仓(BUY) / 平仓(SELL)</span><span class="v">%s / %s</span></div>
  <div class="kv"><span class="k">盈利平仓笔数</span><span class="v">%s</span></div>
  <div class="kv"><span class="k">胜率（平仓口径）</span><span class="v b">%.1f%%</span></div>
  <div class="kv"><span class="k">单笔平均锁利</span><span class="v">%s</span></div>
  <div class="kv"><span class="k">成交频率</span><span class="v">%s 笔/小时</span></div>
  <div class="kv"><span class="k">挂单次数 / 实际成交次数</span><span class="v">%s / %s</span></div>
  <div class="kv"><span class="k">挂单成交率</span><span class="v b">%.1f%%</span></div>
</div>

<h2>按类别盈亏</h2>
<table><thead><tr><th>类别</th><th>笔数</th><th>盈利笔数</th><th>锁利</th></tr></thead><tbody>%s</tbody></table>

<h2>按日盈亏</h2>
<table><thead><tr><th>日期</th><th>锁利</th></tr></thead><tbody>%s</tbody></table>

<h2>极值</h2>
<div class="box">
  <div class="kv"><span class="k">最佳市场</span><span class="v up">%s　%s</span></div>
  <div class="kv"><span class="k">最差市场</span><span class="v dn">%s　%s</span></div>
  <div class="kv"><span class="k">最佳单笔</span><span class="v up">%s　%s</span></div>
  <div class="kv"><span class="k">最差单笔</span><span class="v dn">%s　%s</span></div>
</div>

<h2>当前未平仓敞口</h2>
<table><thead><tr><th>市场</th><th>持仓(份)</th><th>中间价</th><th>YES 买</th><th>YES 卖</th></tr></thead><tbody>%s</tbody></table>
<div class="note">正数=多头（看涨结果），负数=空头。敞口按最新中间价盯市，计入浮动盈亏。</div>

<h2>真实盘口样本（流动性 Top %d）</h2>
<table><thead><tr><th>市场</th><th>类别</th><th>YES 买</th><th>YES 卖</th><th>NO 买</th><th>流动性</th></tr></thead><tbody>%s</tbody></table>
<div class="note">数据来自 Polymarket Gamma 实时接口，已过滤政治/地缘/军事等敏感市场（本次合规 %d 个）。</div>

<h2>参数与方法</h2>
<div class="box">
  <div class="kv"><span class="k">模拟模式</span><span class="v">%s</span></div>
  <div class="kv"><span class="k">挂单成交模型</span><span class="v">%s</span></div>
  <div class="kv"><span class="k">成交率基数 FILL_BASE</span><span class="v">%s</span></div>
  <div class="kv"><span class="k">挂单位置 adverse_frac</span><span class="v">%s</span></div>
  <div class="kv"><span class="k">单笔规模 size</span><span class="v">%s 份</span></div>
  <div class="kv"><span class="k">手续费率</span><span class="v">%.2f%%</span></div>
  <div class="kv"><span class="k">止损 / 全局库存上限</span><span class="v">5%% / $1,500</span></div>
</div>

<h2>读这份报告时要注意</h2>
<div class="warn">
<b>1. 成交率是假设，不是实测。</b>模型按「价格改善幅度」给挂单成交概率，
FILL_BASE 是拍的参数。真实的成交率<b>只能用真钱小额挂单测出来</b>，
这里的所有盈亏都建立在这个假设上。<br/>
<b>2. 未覆盖逆向选择。</b>真实做市中，愿意主动吃你单子的人往往掌握信息优势——
成交的时刻常是价格要朝你不利方向动的时刻。这是做市商真实的头号亏损来源，当前模型完全没有建模。<br/>
<b>3. 盘口在短周期内极其稳定。</b>实测间隔 8 秒连续拉取，300 个市场的买卖价变化为 0，
所以短周期内库存的价格波动风险很小；真正的风险在于<b>结算风险</b>（这些市场多在当天几小时内到期）和上面的逆向选择。<br/>
<b>4. 全程零真钱。</b>DRY_RUN 影子账本，没有任何真实下单。<br/>
<b>5. 收益不等于实盘。</b>按成交率 51%% 的假设，加回成交真实性后收益约为「假设必然成交」乐观上界的 21%%。
</div>

<footer>本报告由 a_share/sim_report.py 自动生成 · 数据源 Polymarket Gamma · 模拟引擎 RigorVirtualBook</footer>
</div></body></html>""" % (
        ts, CSS, ts, s.get("run_start"), s.get("duration_min"), mode_txt,
        cards,
        _money(s.get("initial_equity")), _money(st.get("cash")),
        _money(st.get("equity")),
        _cls(s.get("realized")), _sgn(s.get("realized")),
        _cls(st.get("unrealized")), _sgn(st.get("unrealized")),
        _money(s.get("peak")),
        _cls(s.get("peak_profit")), _sgn(s.get("peak_profit")),
        s.get("drawdown_pct", 0), s.get("max_drawdown_pct", 0),
        _money(st.get("inv_notional")), st.get("n_markets", 0),
        _num(s.get("trades_total")),
        _num(s.get("buys")), _num(s.get("sells")),
        _num(s.get("win")), s.get("win_rate", 0),
        _money(s.get("avg_pnl")), _num(s.get("trades_per_hour")),
        _num(f.get("attempts")), _num(f.get("hits")), fill_rate,
        tag_rows, day_rows,
        str(s.get("best_market", ["-", 0])[0])[:30], _sgn(s.get("best_market", ["-", 0])[1]),
        str(s.get("worst_market", ["-", 0])[0])[:30], _sgn(s.get("worst_market", ["-", 0])[1]),
        _sgn(bt.get("pnl")), str(bt.get("mkt"))[:26],
        _sgn(wt.get("pnl")), str(wt.get("mkt"))[:26],
        inv_rows,
        top_n, mkt_rows, len(mkts.get("markets") or []),
        mode_txt,
        ("开（按价格改善幅度判定）" if fills_on else "关（假设必然成交）"),
        f.get("base"), (st.get("params") or {}).get("adverse"),
        (st.get("params") or {}).get("size"),
        (0.005 * 100),
    )
    return html


# ------------------------------ Markdown ------------------------------
def build_md(st, s, mkts, top_n, ts):
    f = st.get("fill") or {}
    fills_on = f.get("on", False)
    fill_rate = f.get("rate", 0) if fills_on else 100.0
    mode_txt = "真实做市（库存管理）" if st.get("mode") == "inv" else "同轮双边建平（乐观对照）"

    L = []
    A = L.append
    A("# Polymarket 模拟盘 · 实况与统计报告")
    A("")
    A("- 生成时间：%s" % ts)
    A("- 运行起点：%s（已运行 %s 分钟）" % (s.get("run_start"), s.get("duration_min")))
    A("- 模拟模式：%s" % mode_txt)
    A("- 挂单成交模型：%s" % ("开" if fills_on else "关"))
    A("")
    A("## 核心指标")
    A("")
    A("| 指标 | 数值 |")
    A("|---|---|")
    A("| 盯市权益 | %s |" % _money(st.get("equity")))
    A("| 累计锁利 | %s |" % _sgn(s.get("realized")))
    A("| 浮动盈亏 | %s |" % _sgn(st.get("unrealized")))
    A("| 峰值盈利 | %s |" % _sgn(s.get("peak_profit")))
    A("| 历史最大回撤 | %.2f%% |" % s.get("max_drawdown_pct", 0))
    A("| 挂单成交率 | %.1f%% |" % fill_rate)
    A("| 胜率（平仓） | %.1f%% |" % s.get("win_rate", 0))
    A("| 总成交 | %s 笔 |" % _num(s.get("trades_total")))
    A("")
    A("## 成交统计")
    A("")
    A("| 项目 | 数值 |")
    A("|---|---|")
    A("| 建仓 / 平仓 | %s / %s |" % (_num(s.get("buys")), _num(s.get("sells"))))
    A("| 盈利平仓 | %s 笔 |" % _num(s.get("win")))
    A("| 单笔平均锁利 | %s |" % _money(s.get("avg_pnl")))
    A("| 成交频率 | %s 笔/小时 |" % _num(s.get("trades_per_hour")))
    A("| 挂单 / 成交 | %s / %s |" % (_num(f.get("attempts")), _num(f.get("hits"))))
    A("| 未平仓敞口 | %s（%d 个市场） |" % (_money(st.get("inv_notional")), st.get("n_markets", 0)))
    A("")
    A("## 按类别盈亏")
    A("")
    A("| 类别 | 笔数 | 盈利笔数 | 锁利 |")
    A("|---|---|---|---|")
    for k, v in sorted((s.get("per_tag") or {}).items(), key=lambda kv: -kv[1]["pnl"]):
        A("| %s | %s | %s | %s |" % (k, _num(v.get("n")), _num(v.get("win")), _sgn(v.get("pnl"))))
    A("")
    A("## 按日盈亏")
    A("")
    A("| 日期 | 锁利 |")
    A("|---|---|")
    for k, v in sorted((s.get("per_day") or {}).items()):
        A("| %s | %s |" % (k, _sgn(v)))
    A("")
    A("## 极值")
    A("")
    A("- 最佳市场：%s　%s" % (str(s.get("best_market", ["-", 0])[0])[:40],
                             _sgn(s.get("best_market", ["-", 0])[1])))
    A("- 最差市场：%s　%s" % (str(s.get("worst_market", ["-", 0])[0])[:40],
                             _sgn(s.get("worst_market", ["-", 0])[1])))
    bt = s.get("best_trade") or {}
    wt = s.get("worst_trade") or {}
    A("- 最佳单笔：%s　%s" % (_sgn(bt.get("pnl")), str(bt.get("mkt"))[:34]))
    A("- 最差单笔：%s　%s" % (_sgn(wt.get("pnl")), str(wt.get("mkt"))[:34]))
    A("")
    A("## 当前未平仓敞口")
    A("")
    qs = st.get("quotes") or {}
    opens = [(k, v) for k, v in qs.items() if int(v.get("inv") or 0) != 0]
    if opens:
        A("| 市场 | 持仓(份) | 中间价 | YES 买 | YES 卖 |")
        A("|---|---|---|---|---|")
        for k, v in sorted(opens, key=lambda kv: -abs(int(kv[1].get("inv") or 0))):
            A("| %s | %d | %s | %s | %s |" % (str(v.get("question"))[:40], int(v.get("inv")),
                                              _num(v.get("mid"), 4), _num(v.get("yes_bid"), 4),
                                              _num(v.get("yes_ask"), 4)))
    else:
        A("当前无未平仓敞口。")
    A("")
    A("## 真实盘口样本（流动性 Top %d）" % top_n)
    A("")
    A("| 市场 | 类别 | YES 买 | YES 卖 | NO 买 | 流动性 |")
    A("|---|---|---|---|---|---|")
    for m in (mkts.get("markets") or [])[:top_n]:
        A("| %s | %s | %s | %s | %s | %s |" % (str(m.get("question"))[:46], m.get("tag"),
                                               _num(m.get("yes_bid"), 4), _num(m.get("yes_ask"), 4),
                                               _num(m.get("no_bid"), 4), _money(m.get("liquidity"))))
    A("")
    A("## 读这份报告时要注意")
    A("")
    A("1. **成交率是假设，不是实测。** 模型按「价格改善幅度」给挂单成交概率，"
      "FILL_BASE 是拍的参数。真实成交率只能用真钱小额挂单测出来。")
    A("2. **未覆盖逆向选择。** 愿意主动吃你单子的人往往掌握信息优势，"
      "成交时刻常是价格要朝你不利方向动的时刻。这是做市商真实的头号亏损来源，当前模型没有建模。")
    A("3. **盘口在短周期内极稳定。** 实测 8 秒内 300 个市场买卖价变化为 0，"
      "库存价格波动风险很小；真正的风险是结算风险与逆向选择。")
    A("4. **全程零真钱。** DRY_RUN 影子账本，无真实下单。")
    A("5. **收益不等于实盘。** 按成交率 51% 假设，加回成交真实性后收益约为乐观上界的 21%。")
    A("")
    A("---")
    A("由 `a_share/sim_report.py` 自动生成 · 数据源 Polymarket Gamma · 引擎 RigorVirtualBook")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=os.path.join(ROOT, "output"))
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--top", type=int, default=15, help="盘口样本条数")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    print("从 %s 拉取实况 ..." % base)
    try:
        st = _get("/api/state", base)
        s = _get("/api/stats", base)
        mkts = _get("/api/markets", base)
    except Exception as e:
        print("错误：拿不到数据（%s）。服务是否在运行？" % e)
        return 1

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    os.makedirs(args.out_dir, exist_ok=True)
    html_path = os.path.join(args.out_dir, "sim_report_%s.html" % stamp)
    md_path = os.path.join(args.out_dir, "sim_report_%s.md" % stamp)

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(build_html(st, s, mkts, args.top, ts))
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(build_md(st, s, mkts, args.top, ts))

    print()
    print("已生成报告：")
    print("  HTML     %s  (%.0f KB)" % (html_path, os.path.getsize(html_path) / 1024))
    print("  Markdown %s  (%.0f KB)" % (md_path, os.path.getsize(md_path) / 1024))
    print()
    print("  运行起点 %s | 轮次 %s | 权益 %s | 累计锁利 %s | 成交率 %.1f%%"
          % (s.get("run_start"), s.get("round"), _money(st.get("equity")),
             _sgn(s.get("realized")), (st.get("fill") or {}).get("rate", 0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

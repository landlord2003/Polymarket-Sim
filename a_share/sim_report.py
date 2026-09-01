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
import csv
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


# ------------------------------ 成交流水读取 / CSV 导出 ------------------------------
def load_trades(n, base=None):
    """从 trades.jsonl 读取成交流水，返回最后 n 笔（n<=0 表示全部）。找不到返回 []。

    优先读 a_share/data/trades.jsonl（服务器落盘位置），回退 output/、data/。
    """
    base = base or ROOT
    cand = [
        os.path.join(base, "a_share", "data", "trades.jsonl"),
        os.path.join(base, "output", "trades.jsonl"),
        os.path.join(base, "data", "trades.jsonl"),
    ]
    path = next((p for p in cand if os.path.exists(p)), None)
    if not path:
        return []
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        return []
    if n and n > 0:
        rows = rows[-n:]
    return rows


def write_trades_csv(trades, path):
    """把成交流水导出为 CSV（含可读时间列）。成功返回 True。"""
    cols = ["time", "ts", "round", "mkt", "tag", "side", "entry",
            "size", "pnl", "slip", "cash_after", "q"]
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for t in trades:
                row = dict(t)
                try:
                    row["time"] = datetime.datetime.fromtimestamp(
                        float(t.get("ts") or 0)).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    row["time"] = ""
                w.writerow(row)
        return True
    except Exception as e:
        print("CSV 写入失败: %s" % e)
        return False


def _date_of(ts):
    """Unix 时间戳 -> 本地日期字符串 YYYY-MM-DD（解析失败返回空）。"""
    try:
        return datetime.datetime.fromtimestamp(float(ts or 0)).strftime("%Y-%m-%d")
    except Exception:
        return ""


def parse_time_to_ts(s):
    """把时间字符串解析为 unix 秒（本地时区）。支持：
    'YYYY-MM-DDTHH:MM' / 'YYYY-MM-DD HH:MM:SS' / 'YYYY-MM-DD'。
    解析失败或为空返回 None。
    """
    if not s:
        return None
    s = str(s).strip().replace("T", " ")
    fmts = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]
    for fmt in fmts:
        try:
            return datetime.datetime.strptime(s, fmt).timestamp()
        except Exception:
            pass
    return None


def filter_trades(trades, since_round=None, until_round=None, date=None,
                  since_ts=None, until_ts=None, limit=None):
    """按轮次区间 / 日期 / 时间区间 / 最近 N 笔 过滤成交流水。某参数为 None 则不过滤该项。

    - since_round: 仅保留 round >= since_round（某轮之后）
    - until_round: 仅保留 round <= until_round
    - date:        仅保留成交本地日期 == date(YYYY-MM-DD) 的成交
    - since_ts:    仅保留 ts >= since_ts（unix 秒，本地时间区间起点）
    - until_ts:    仅保留 ts <= until_ts（unix 秒，本地时间区间终点）
    - limit:       取过滤结果中最后 limit 笔（即「最近 N 笔」）；<=0 或 None 不过滤
    """
    out = trades
    if since_round is not None:
        out = [t for t in out if (t.get("round") or 0) >= since_round]
    if until_round is not None:
        out = [t for t in out if (t.get("round") or 0) <= until_round]
    if date:
        out = [t for t in out if _date_of(t.get("ts")) == date]
    if since_ts is not None:
        out = [t for t in out if (t.get("ts") or 0) >= since_ts]
    if until_ts is not None:
        out = [t for t in out if (t.get("ts") or 0) <= until_ts]
    if limit and limit > 0:
        out = out[-limit:]
    return out


def summarize_by_tag(trades):
    """按类别 tag 汇总锁利（治本分类，与 §6.5 同源）。

    返回 [(tag, n, total_pnl, avg_pnl, pct)] 按 total_pnl 降序。
    tag 为空归入 (未分类)；占比 = 该类别锁利 / 全部锁利合计（合计为 0 时记 0）。
    """
    agg = {}
    total = 0.0
    for t in trades:
        tag = (t.get("tag") or "").strip() or "(未分类)"
        try:
            p = float(t.get("pnl") or 0)
        except Exception:
            p = 0.0
        a = agg.setdefault(tag, {"n": 0, "pnl": 0.0})
        a["n"] += 1
        a["pnl"] += p
        total += p
    rows = []
    for tag, a in agg.items():
        n = a["n"]
        pnl = a["pnl"]
        avg = pnl / n if n else 0.0
        pct = (pnl / total * 100.0) if total else 0.0
        rows.append((tag, n, pnl, avg, pct))
    rows.sort(key=lambda r: -r[2])
    return rows


def archive_trades(trades, out_dir, mode="all", days_back=1):
    """把成交流水按日期归档为 CSV（供审计留存，免手动导出）。

    - mode='all'  : 对每个出现的日期写一个 trades_YYYY-MM-DD.csv（已存在则跳过，幂等）
    - mode='daily': 仅写 (今天-days_back) 那一天；当天无成交返回 []
    返回写入/跳过的文件路径列表。
    """
    os.makedirs(out_dir, exist_ok=True)
    if mode == "daily":
        target = (datetime.datetime.now() - datetime.timedelta(days=days_back)).strftime("%Y-%m-%d")
        rows = [t for t in trades if _date_of(t.get("ts")) == target]
        if not rows:
            return []
        path = os.path.join(out_dir, "trades_%s.csv" % target)
        if os.path.exists(path):
            return [path]  # 幂等：已归档则跳过
        write_trades_csv(rows, path)
        return [path]
    by_date = {}
    for t in trades:
        d = _date_of(t.get("ts"))
        if d:
            by_date.setdefault(d, []).append(t)
    res = []
    for d in sorted(by_date):
        path = os.path.join(out_dir, "trades_%s.csv" % d)
        if os.path.exists(path):
            res.append(path)
            continue
        if write_trades_csv(by_date[d], path):
            res.append(path)
    return res


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


def build_html(st, s, mkts, top_n, ts, trades=None, tag_trades=None):
    f = st.get("fill") or {}
    mode_txt = "真实做市（库存管理）" if st.get("mode") == "inv" else "同轮双边建平（乐观对照）"
    fills_on = f.get("on", False)
    fill_rate = f.get("rate", 0) if fills_on else 100.0

    def card(k, v, cls="b"):
        return '<div class="card"><div class="k">%s</div><div class="v %s">%s</div></div>' % (k, cls, v)

    cards = "".join([
        card("盯市权益", _money(st.get("equity")), "b"),
        card("累计锁利", _sgn(s.get("realized")), _cls(s.get("realized"))),
        card("逆向选择损耗", _sgn(-(s.get("adverse_sel_loss") or 0)), "dn"),
        card("已结算锁利", _sgn(s.get("settled_pnl") or 0), _cls(s.get("settled_pnl") or 0)),
        card("结算敞口(风险)", _sgn(-(s.get("settlement_exposure") or 0)), "dn"),
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

    # 最近成交明细（治本补充：逐笔交易详细内容）
    trades = trades if trades is not None else (st.get("positions") or [])
    n_trades = len(trades)
    trade_rows = ""

    # 按类别锁利汇总（治本分类 tag，源自成交流水 trades.jsonl；全量历史，不限于上方明细样本）
    _tag_src = tag_trades if tag_trades is not None else trades
    _tag_summary = summarize_by_tag(_tag_src)
    tag_summary_rows = ""
    for _tg, _tn, _tp, _ta, _tpct in _tag_summary:
        tag_summary_rows += ("<tr><td>%s</td><td>%s</td><td class='%s'>%s</td>"
                              "<td>%.1f%%</td><td class='%s'>%s</td></tr>"
                              % (_tg, _num(_tn), _cls(_tp), _sgn(_tp),
                                 _tpct, _cls(_ta), _sgn(_ta)))
    if tag_summary_rows:
        tag_summary_html = ("<h2>按类别锁利汇总（成交流水 trades.jsonl）</h2>"
            "<table><thead><tr><th>类别</th><th>成交笔数</th><th>锁利合计</th>"
            "<th>占总额比</th><th>笔均锁利</th></tr></thead><tbody>%s</tbody></table>"
            "<div class='note'>按成交流水 trades.jsonl 的治本分类 tag 汇总（与 §6.5 同源）。"
            "占比 = 该类别锁利 / 全部锁利合计；颜色按中国习惯：红=盈利、绿=亏损。</div>"
            % tag_summary_rows)
    else:
        tag_summary_html = ("<h2>按类别锁利汇总（成交流水 trades.jsonl）</h2>"
            "<div class='note'>（暂无成交流水，先跑出成交后才有数据）</div>")
    for t in trades:
        side = (t.get("side") or "").upper()
        sc = "up" if side == "BUY" else "dn"
        trade_rows += ("<tr><td>%s</td><td>%s</td><td>%s</td><td class='%s'>%s</td>"
                       "<td>%s</td><td>%s</td><td class='%s'>%s</td><td>%s</td><td>%s</td></tr>"
                       % (str(t.get("ts")), str(t.get("mkt"))[:40], t.get("tag"),
                          sc, side, _num(t.get("entry"), 4), _num(t.get("size")),
                          _cls(t.get("pnl")), _sgn(t.get("pnl")),
                          _num(t.get("slip"), 4), _money(t.get("cash_after"))))

    bt = s.get("best_trade") or {}
    wt = s.get("worst_trade") or {}

    # 盈亏归因瀑布（P1-3）
    _a = s.get("attribution") or {}
    attr_rows = ""
    if _a:
        _arows = [
            ("毛价差捕获（做市两腿价差收入）", _a.get("gross_spread", 0)),
            ("走簿滑点（成交冲击盘口）", _a.get("walk_the_book", 0)),
            ("手续费", _a.get("fees", 0)),
            ("逆向选择损耗", _a.get("adverse_selection", 0)),
            ("结算净损益", _a.get("settlement", 0)),
        ]
        _body = "".join(
            "<tr><td>%s</td><td class='%s'>%s</td></tr>" % (nm, _cls(v), _sgn(v))
            for nm, v in _arows)
        attr_rows = ("<h2>盈亏归因（瀑布）</h2>"
                     "<table><thead><tr><th>分量</th><th>金额</th></tr></thead><tbody>%s</tbody>"
                     "<tfoot><tr><td><b>净锁利</b></td><td class='b %s'>%s</td></tr></tfoot></table>"
                     "<div class='note'>恒等式：毛价差 − 滑点 − 手续费 − 逆向选择 + 结算 = 净锁利。"
                     "各分量来自真实成交记录（fee/slip/adverse/settled 累加器），不编造。</div>" % (
                         _body, _cls(_a.get("net", 0)), _sgn(_a.get("net", 0))))

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

%s

<h2>最近成交明细（最近 %d 笔）</h2>
<table><thead><tr><th>时间</th><th>市场</th><th>类别</th><th>方向</th><th>成交价</th><th>量</th><th>本笔锁利</th><th>滑点</th><th>现金</th></tr></thead><tbody>%s</tbody></table>
<div class="note">逐笔来自服务器侧 TRADES 落盘流水（完整记录含全部字段与类别，存于 data/trades.jsonl，自动轮转归档）。
BUY=建仓（锁利 0），SELL=平仓（显示本笔锁利）；颜色按中国习惯：红=盈利/涨、绿=亏损/跌。</div>

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

%s

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
        top_n, mkt_rows, len(mkts.get("markets") or []), tag_summary_html, n_trades, trade_rows,
        mode_txt,
        ("开（按价格改善幅度判定）" if fills_on else "关（假设必然成交）"),
        f.get("base"), (st.get("params") or {}).get("adverse"),
        (st.get("params") or {}).get("size"),
        (0.005 * 100),
        attr_rows,
    )
    return html


# ------------------------------ Markdown ------------------------------
def build_md(st, s, mkts, top_n, ts, trades=None, tag_trades=None):
    f = st.get("fill") or {}
    fills_on = f.get("on", False)
    fill_rate = f.get("rate", 0) if fills_on else 100.0
    mode_txt = "真实做市（库存管理）" if st.get("mode") == "inv" else "同轮双边建平（乐观对照）"

    L = []
    A = L.append
    trades = trades if trades is not None else (st.get("positions") or [])
    n_trades = len(trades)
    _tag_src = tag_trades if tag_trades is not None else trades
    _tag_summary = summarize_by_tag(_tag_src)
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
    A("| 逆向选择损耗 | %s |" % _sgn(-(s.get("adverse_sel_loss") or 0)))
    A("| 已结算锁利 | %s |" % _sgn(s.get("settled_pnl") or 0))
    A("| 结算敞口(风险) | %s |" % _sgn(-(s.get("settlement_exposure") or 0)))
    A("| 浮动盈亏 | %s |" % _sgn(st.get("unrealized")))
    A("| 峰值盈利 | %s |" % _sgn(s.get("peak_profit")))
    A("| 历史最大回撤 | %.2f%% |" % s.get("max_drawdown_pct", 0))
    A("| 挂单成交率 | %.1f%% |" % fill_rate)
    A("| 胜率（平仓） | %.1f%% |" % s.get("win_rate", 0))
    A("| 总成交 | %s 笔 |" % _num(s.get("trades_total")))
    A("")
    A("## 盈亏归因（瀑布）")
    A("")
    A("恒等式：**毛价差 − 滑点 − 手续费 − 逆向选择 + 结算 = 净锁利**。各分量来自真实成交记录，不编造。")
    A("")
    _a = s.get("attribution") or {}
    if _a:
        A("| 分量 | 金额 |")
        A("|---|---|")
        A("| 毛价差捕获（做市两腿价差收入） | %s |" % _sgn(_a.get("gross_spread", 0)))
        A("| 走簿滑点（成交冲击盘口） | %s |" % _sgn(_a.get("walk_the_book", 0)))
        A("| 手续费 | %s |" % _sgn(_a.get("fees", 0)))
        A("| 逆向选择损耗 | %s |" % _sgn(_a.get("adverse_selection", 0)))
        A("| 结算净损益 | %s |" % _sgn(_a.get("settlement", 0)))
        A("| **净锁利** | **%s** |" % _sgn(_a.get("net", 0)))
    else:
        A("（暂无归因数据）")
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
    A("## 按类别锁利汇总（成交流水 trades.jsonl）")
    A("")
    if _tag_summary:
        A("按成交流水 trades.jsonl 的治本分类 tag 汇总（与 §6.5 同源）；占比 = 该类别锁利 / 全部锁利合计。")
        A("")
        A("| 类别 | 成交笔数 | 锁利合计 | 占总额比 | 笔均锁利 |")
        A("|---|---|---|---|---|")
        for _tg, _tn, _tp, _ta, _tpct in _tag_summary:
            A("| %s | %s | %s | %.1f%% | %s |" % (_tg, _num(_tn), _sgn(_tp), _tpct, _sgn(_ta)))
    else:
        A("（暂无成交流水，先跑出成交后才有数据）")
    A("")
    A("## 最近成交明细（最近 %d 笔）" % n_trades)
    A("")
    if trades:
        A("| 时间 | 市场 | 类别 | 方向 | 成交价 | 量 | 本笔锁利 | 滑点 | 现金 |")
        A("|---|---|---|---|---|---|---|---|---|")
        for t in trades:
            side = (t.get("side") or "").upper()
            A("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                str(t.get("ts")), str(t.get("mkt"))[:34], t.get("tag"), side,
                _num(t.get("entry"), 4), _num(t.get("size")),
                _sgn(t.get("pnl")), _num(t.get("slip"), 4), _money(t.get("cash_after"))))
    else:
        A("（暂无成交记录）")
    A("")
    A("> 逐笔来自服务器侧 TRADES 落盘流水，完整记录存于 `data/trades.jsonl`（自动轮转归档）。"
      "BUY=建仓（锁利 0），SELL=平仓（显示本笔锁利）。")
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
    ap.add_argument("--trades", type=int, default=40,
                    help="报告内逐笔成交明细条数（0=全部，从 trades.jsonl 读取）")
    ap.add_argument("--csv", action="store_true",
                    help="额外导出全量成交 CSV（trades_export_<时间戳>.csv）")
    ap.add_argument("--since-round", type=int, default=None,
                    help="CSV/报告仅含该轮次之后的成交")
    ap.add_argument("--until-round", type=int, default=None,
                    help="CSV/报告仅含该轮次之前的成交")
    ap.add_argument("--date", default=None,
                    help="CSV/报告仅含该日期成交(YYYY-MM-DD)")
    ap.add_argument("--archive-dir", default=os.path.join(ROOT, "output", "audit"),
                    help="定时归档输出目录（默认 output/audit）")
    ap.add_argument("--archive", action="store_true",
                    help="按日期归档全量成交 CSV 到 --archive-dir（已存在则跳过，幂等）")
    ap.add_argument("--archive-daily", action="store_true",
                    help="仅归档前一天成交 CSV（配合 cron/计划任务每日跑）")
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

    # 读取成交流水（--trades 控制报告内条数；0=全部），并按区间/日期过滤
    _flt = dict(since_round=args.since_round, until_round=args.until_round, date=args.date)
    trades = filter_trades(load_trades(args.trades), **_flt)
    # 类别锁利汇总用全量历史（同样按区间/日期过滤），不限于上方明细样本
    tag_trades = filter_trades(load_trades(0), **_flt)

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(build_html(st, s, mkts, args.top, ts, trades=trades, tag_trades=tag_trades))
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(build_md(st, s, mkts, args.top, ts, trades=trades, tag_trades=tag_trades))

    print()
    print("已生成报告：")
    print("  HTML     %s  (%.0f KB)" % (html_path, os.path.getsize(html_path) / 1024))
    print("  Markdown %s  (%.0f KB)" % (md_path, os.path.getsize(md_path) / 1024))
    if args.trades and args.trades > 0:
        print("  逐笔明细  最近 %d 笔（筛选后 %d 笔）" % (min(args.trades, len(trades)), len(trades)))
    else:
        print("  逐笔明细  全部 %d 笔（筛选后）" % len(trades))
    if args.csv:
        csv_path = os.path.join(args.out_dir, "trades_export_%s.csv" % stamp)
        _csv_rows = filter_trades(load_trades(0), **_flt)
        if write_trades_csv(_csv_rows, csv_path):
            print("  CSV      %s  (%.0f KB, 筛选后 %d 笔)"
                  % (csv_path, os.path.getsize(csv_path) / 1024, len(_csv_rows)))
    # 定时归档（每日打包前一日 / 全量按日期）
    if args.archive or args.archive_daily:
        _all_tr = load_trades(0)
        if args.archive_daily:
            _w = archive_trades(_all_tr, args.archive_dir, mode="daily", days_back=1)
            print("  每日归档  %s" % (_w if _w else "（前一天无成交 / 已归档，跳过）"))
        if args.archive:
            _w = archive_trades(_all_tr, args.archive_dir, mode="all")
            print("  全量归档  %d 个日期文件 -> %s" % (len(_w), args.archive_dir))
    print()
    print("  运行起点 %s | 轮次 %s | 权益 %s | 累计锁利 %s | 成交率 %.1f%%"
          % (s.get("run_start"), s.get("round"), _money(st.get("equity")),
             _sgn(s.get("realized")), (st.get("fill") or {}).get("rate", 0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

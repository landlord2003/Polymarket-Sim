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
        if r.offline:
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
        if r.offline:
            rows.append(
                f'<tr class="offline"><td>{escape(r.symbol)}</td>'
                f"<td>{escape(r.name)}</td><td>⚠️ 离线</td>"
                f'<td colspan="4">合成数据，未产生真实信号</td></tr>'
            )
            continue
        notes = "；".join(r.notes) or "—"
        price = f"{r.last_price:.2f}" if r.last_price else "-"
        risk = "" if r.risk_pass else f'<div class="risk">⛔ 风控拦截：{escape(r.risk_reason)}</div>'
        rows.append(
            f"<tr>"
            f'<td class="code">{escape(r.symbol)}</td>'
            f"<td>{escape(r.name)}</td>"
            f'<td class="sig">{r.signal_emoji} {escape(r.signal)}</td>'
            f'<td class="score">{_bar(r.composite)}<small>{_fmt(r.composite)}</small></td>'
            f"<td>{price}</td>"
            f'<td class="dims">行情{_fmt(r.market_score)} 资金{_fmt(r.money_score)}<br>'
            f"板块{_fmt(r.sector_score)} 消息{_fmt(r.news_score)}</td>"
            f'<td class="notes">{escape(notes)}{risk}</td>'
            f"</tr>"
        )
    return "\n".join(rows)


def _screener_blocks(screener_result: dict) -> str:
    if not screener_result:
        return ""
    blocks = []
    for label, blk in screener_result.items():
        off = blk.get("offline")
        tag = ' <span class="off">离线样本·非真实</span>' if off else ""
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
                f"</tr>"
            )
        blocks.append(
            f'<div class="sector">'
            f"<h3>🧩 {escape(label)} {tag}</h3>"
            f'<table><thead><tr><th>#</th><th>代码</th><th>名称</th>'
            f"<th>强度</th><th>最新价</th><th>备注</th></tr></thead>"
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
footer { color:#6b7888; font-size:12px; border-top:1px solid #2a3340;
  padding-top:14px; margin-top:30px; }
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
        "<th>综合分</th><th>最新价</th><th>四维</th><th>明细/规则</th></tr></thead>"
        f"<tbody>{wl}</tbody></table></section>"
        if show_watchlist else ""
    )
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>A股信号看板</title><style>" + CSS + "</style></head><body>"
        "<div class=\"wrap\">"
        f"<header><h1>📊 A股四维度信号看板</h1>"
        f'<div class="meta">生成时间 {ts}'
        f'<span class="badge {badge_cls}">{badge_txt}</span></div>'
        f"{summary}</header>"
        f"{wl_section}"
        f"{scr_section}"
        "<footer>信号由量化引擎生成，仅供研究参考，不构成投资建议。"
        "A股手动决策、手动下单、风险自担。在可视面板点击「运行」即可刷新本看板。</footer>"
        "</div></body></html>"
    )


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

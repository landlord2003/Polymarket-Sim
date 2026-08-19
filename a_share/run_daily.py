"""A股每日信号扫描 + 钉钉/微信推送（手动执行）

用法：
  python a_share/run_daily.py                 # 联网取数，真实信号
  python a_share/run_daily.py --offline       # 合成数据，仅验证引擎与报告（不推送真实信号）
  python a_share/run_daily.py --screener      # 额外跑五板块自动选股初筛
  python a_share/run_daily.py --screener --offline

流程：watchlist(含个性化规则) → 四维度打分 → 风控闸门 → Markdown 日报 → 推送
      [--screener] 五板块(新能源/新材料/AI/机器人/军工) 扫描 → TopN 推荐
"""

import sys
import os
import json
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signal_engine import analyze_stock, StockResult
from notify import send_markdown, send_wecom
from screener import run_screener, build_screener_report
from dashboard import write_dashboard

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST = os.path.join(HERE, "watchlist.json")


def load_watchlist(path: str = WATCHLIST) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_report(results: list) -> str:
    today = datetime.today().strftime("%Y-%m-%d")
    lines = [f"# 📊 A股四维度信号日报 {today}\n"]
    buy, hold, sell = [], [], []
    for r in results:
        if not r.signal:
            lines.append(f"## ⚠️ {r.name or r.symbol}({r.symbol}) — 无数据\n")
            continue
        lines.append(
            f"## {r.signal_emoji} {r.name or r.symbol}({r.symbol}) — {r.signal}"
            f"{'（合成数据）' if r.offline else ''}\n"
            f"- 最新价：{r.last_price:.2f}\n"
            f"- 行情 {r.market_score:+.2f} ｜ 资金 {r.money_score:+.2f} ｜ "
            f"板块 {r.sector_score:+.2f} ｜ 消息 {r.news_score:+.2f}\n"
            f"- 综合 {r.composite:+.2f}\n"
            f"- 风控：{'通过' if r.risk_pass else '拦截 ' + r.risk_reason}\n"
            f"- 明细：{'；'.join(r.notes)}\n"
        )
        if r.signal in ("买入", "偏多"):
            buy.append(r.name or r.symbol)
        elif r.signal in ("减仓", "卖出", "暂停"):
            sell.append(r.name or r.symbol)
        else:
            hold.append(r.name or r.symbol)
    lines.append(
        f"## 🧭 汇总\n- 🟢 关注/买入：{', '.join(buy) or '无'}\n"
        f"- 🟡 观望/持有：{', '.join(hold) or '无'}\n"
        f"- 🔴 减仓/卖出：{', '.join(sell) or '无'}\n"
        f"\n> 信号仅供研究，手动决策，风险自担。"
    )
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="合成数据验证，不推送真实信号")
    ap.add_argument("--screener", action="store_true", help="额外跑五板块自动选股初筛")
    ap.add_argument("--no-html", action="store_true", help="不生成 HTML 看板")
    args = ap.parse_args()

    watch = load_watchlist()
    weights = watch.get("weights")  # 顶层四维度权重，一处可调
    holding = watch.get("holding", False)  # 顶层持仓状态：False=空仓，规则按回补参考解读
    results = []
    offline_any = args.offline
    for item in watch["watchlist"]:
        sym = item["symbol"]
        name = item.get("name", "")
        rules = item.get("rules")
        if args.offline:
            results.append(StockResult(
                symbol=sym, name=name, offline=True,
                notes=["离线合成数据，仅验证引擎，未产生真实信号"]))
            continue
        r = analyze_stock(sym, name, rules=rules, weights=weights, holding=holding)
        if r.offline:
            offline_any = True
        results.append(r)

    report = build_report(results)

    screener_report = ""
    scr_result = None
    if args.screener:
        scr_result = run_screener(offline=args.offline)
        screener_report = build_screener_report(scr_result)
        report += "\n\n" + screener_report

    # —— HTML 看板（默认生成，--no-html 关闭）——
    if not args.no_html:
        mode = "offline" if (offline_any or args.offline) else "online"
        html_path = write_dashboard(results, scr_result, mode=mode)
        print(f"\n[html] 看板已生成：{html_path}")

    print(report)
    if screener_report:
        print("\n===== 选股初筛已生成（见上）=====")

    if offline_any and not args.offline:
        print("\n[warn] 部分标的离线取数失败，报告含非真实信号，已跳过推送。")
        return
    if args.offline:
        print("\n[offline] 离线验证模式，未推送。")
        return

    title = f"A股信号日报 {datetime.today().strftime('%Y-%m-%d')}"
    send_markdown(title, report)
    send_wecom(report)


if __name__ == "__main__":
    main()

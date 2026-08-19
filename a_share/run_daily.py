"""A股每日信号扫描 + 钉钉/微信推送（手动执行）

用法：
  python a_share/run_daily.py            # 联网取数，真实信号
  python a_share/run_daily.py --offline  # 合成数据，仅验证引擎与报告（不推送真实信号）

流程：watchlist → 四维度打分 → 风控闸门 → Markdown 日报 → 推送
"""

import sys
import os
import json
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signal_engine import analyze_stock, StockResult
from notify import send_markdown, send_wecom

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST = os.path.join(HERE, "watchlist.json")


def load_watchlist(path: str = WATCHLIST) -> list:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["watchlist"]


def build_report(results: list) -> str:
    today = datetime.today().strftime("%Y-%m-%d")
    lines = [f"# 📊 A股四维度信号日报 {today}\n"]
    buy, hold, sell = [], [], []
    for r in results:
        if r.offline:
            lines.append(f"## ⚠️ {r.name or r.symbol}({r.symbol}) — 离线\n- {r.notes[0]}\n")
            continue
        lines.append(
            f"## {r.signal_emoji} {r.name or r.symbol}({r.symbol}) — {r.signal}\n"
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
    args = ap.parse_args()

    watch = load_watchlist()
    results = []
    offline_any = args.offline
    for item in watch:
        sym = item["symbol"]
        name = item.get("name", "")
        if args.offline:
            results.append(StockResult(
                symbol=sym, name=name, offline=True,
                notes=["离线合成数据，仅验证引擎，未产生真实信号"]))
            continue
        r = analyze_stock(sym, name)
        if r.offline:
            offline_any = True
        results.append(r)

    report = build_report(results)
    print(report)

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

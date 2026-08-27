# -*- coding: utf-8 -*-
"""Phase 6 流水线：模拟盘自动交易 + 反馈迭代 + 钉钉推送（轻量，无 Redis/Rust）。

把「拉行情 -> 扫描 -> RigorVirtualBook 严谨度成交 -> 逐笔日志 -> sim_feedback
反馈迭代 -> 组装钉钉消息推送」串成一条可定时运行的流水线，对应改进计划.md 的
Phase 6（轻量事件总线 / 自动化流水线，仅做自动轮动 + 手机推送时启用）。

红线：不碰真实下单/钱包；钉钉推送走 notify.py（读 .env 的 DINGTALK_WEBHOOK/
SECRET），强制走本地代理 127.0.0.1:18081 出网。
"""
from __future__ import annotations

import os
import sys
import json
import time
import argparse

# 强制走本地代理出网（Polymarket 行情 + 钉钉推送都需要）
_PROXY = "http://127.0.0.1:18081"
for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy",
          "ALL_PROXY", "all_proxy"):
    os.environ[k] = _PROXY

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))  # 项目根，使 core 包可导入

import sim_trader          # noqa: E402  (导入即设置代理)
import sim_feedback        # noqa: E402
import notify             # noqa: E402


def run_pipeline(runs=6, push_dingtalk=False, verbose=False,
                 book=None, reset=False):
    """跑 N 轮模拟 + 反馈，可选推送钉钉。返回 (summary_rows, feedback_last)。"""
    params = dict(sim_trader.DEFAULT_PARAMS)
    rigor = sim_trader.rigor_params_from_config()
    book_path = book or sim_trader.DEFAULT_BOOK

    if reset:
        from sim_rigor import RigorVirtualBook
        RigorVirtualBook(book_path).reset()
        caps = os.path.join(_HERE, "sim_daily_caps.json")
        try:
            os.remove(caps)
        except Exception:
            pass
        print("[pipeline] 已重置账本与日成交上限状态")

    log_path = os.path.join(sim_trader.LOG_DIR,
                            "trades_%s.jsonl" % time.strftime("%Y%m%d"))
    summary_rows = []
    with open(log_path, "a", encoding="utf-8") as logf:
        for i in range(runs):
            run_id = "pipe_%s_%d" % (time.strftime("%Y%m%d_%H%M%S"), i)
            r = sim_trader.run_once(params, book_path, run_id, logf,
                                    verbose=verbose, rigor=rigor)
            summary_rows.append(r)
            if not verbose:
                v = r.get("view", {})
                print("[%s] quotes=%s scanned=%s exec=%s cash=$%.2f pnl=$%.2f"
                      % (run_id, r.get("quotes"), r.get("scanned"),
                         r.get("executed"), v.get("cash", 0),
                         v.get("realized_pnl", 0)))

    # 持久化本轮汇总（与 sim_trader.main 同格式，供反馈/查阅）
    sum_path = os.path.join(sim_trader.LOG_DIR,
                            "summary_%s.json" % time.strftime("%Y%m%d"))
    with open(sum_path, "w", encoding="utf-8") as f:
        json.dump(summary_rows, f, ensure_ascii=False, indent=2)

    # 反馈迭代
    sim_feedback.main()

    # 读最新反馈 + 汇总，组装消息
    fb_path = os.path.join(sim_feedback.LOG_DIR,
                           "feedback_%s.json" % time.strftime("%Y%m%d"))
    sp_path = os.path.join(sim_trader.LOG_DIR,
                           "summary_%s.json" % time.strftime("%Y%m%d"))
    feedback_last = {}
    if os.path.exists(fb_path):
        try:
            feedback_last = json.load(open(fb_path, encoding="utf-8"))[-1]
        except Exception:
            pass
    summary_rows_file = []
    if os.path.exists(sp_path):
        try:
            summary_rows_file = json.load(open(sp_path, encoding="utf-8"))
        except Exception:
            pass

    md = build_markdown(runs, summary_rows, feedback_last)
    print("=== 钉钉消息内容 ===")
    print(md)
    if push_dingtalk:
        resp = notify.send_markdown("Polymarket 模拟盘运行报告", md)
        print("[pipeline] 钉钉推送:", "成功" if resp and resp.get("errcode") == 0
              else "失败/未配置")
    else:
        print("（未推送，加 --push-dingtalk 发送）")
    return summary_rows, feedback_last


def build_markdown(runs, summary_rows, feedback_last):
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    a = feedback_last.get("analysis", {}) if feedback_last else {}
    mm = a.get("mm", {})
    pc = a.get("pure_candidates", {})
    sk = mm.get("skips", {})
    last_view = summary_rows[-1].get("view", {}) if summary_rows else {}
    cum_pnl = last_view.get("realized_pnl", 0.0)
    cash = last_view.get("cash", 0.0)
    total_exec = sum(r.get("executed", 0) for r in summary_rows)
    ver = feedback_last.get("methodology_version", "-")
    notes = feedback_last.get("suggestions", [])
    lines = [
        "## Polymarket 模拟盘运行报告",
        "> 虚拟资金 · 真实行情 · 严谨度模型（不涉真实下单）",
        "",
        "**时间**：%s  " % now_str,
        "**本轮**：%d 轮，累计执行 %d 笔做市；虚拟本金 $%.2f" % (
            runs, total_exec, cash),
        "",
        "**做市（严谨度模型）**",
        "- 累计实现盈亏：**$%.2f**" % mm.get("pnl", 0.0),
        "- 胜率 %.0f%% ｜ 净胜率(扣亏损笔) %.0f%%" % (
            mm.get("win_rate", 0) * 100, mm.get("net_win_rate", 0) * 100),
        "- 滑点总成本 $%.2f" % mm.get("slip_cost", 0.0),
        "- 门控跳过：深度 %d / 时间衰减 %d / 单市场日上限 %d 笔" % (
            sk.get("depth", 0), sk.get("time", 0), sk.get("cap", 0)),
        "",
        "**纯套利候选**：%d 个（平均 edge %.4f，平均成交率 %.0f%%）" % (
            pc.get("count", 0), pc.get("avg_edge", 0),
            pc.get("avg_fill_ratio", 0) * 100),
        "> 完备性无法自动验证，均待人工确认，未自动执行",
        "",
        "**方法论版本**：%s" % ver,
    ]
    if notes:
        lines.append("**建议**：")
        for n in notes[:3]:
            lines.append("- %s" % n)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=6)
    ap.add_argument("--push-dingtalk", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--book", default=None)
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    run_pipeline(runs=a.runs, push_dingtalk=a.push_dingtalk,
                 verbose=a.verbose, book=a.book, reset=a.reset)


if __name__ == "__main__":
    main()

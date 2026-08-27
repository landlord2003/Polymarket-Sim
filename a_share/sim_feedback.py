# -*- coding: utf-8 -*-
"""模拟盘反馈迭代机制：分析逐笔日志，评估稳定性，输出可调参建议与方法论版本。

读 a_share/sim_logs/trades_YYYYMMDD.jsonl -> 统计成交/胜率/锁利/纯套利候选 ->
给出方法论版本(methodology_version)与下轮调参建议(proposed_params)。
历史反馈追加写入 feedback_YYYYMMDD.json，形成方法论演进轨迹。
"""
from __future__ import annotations

import os
import sys
import json
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(_HERE, "sim_logs")
sys.path.insert(0, _HERE)

# 与 sim_trader.DEFAULT_PARAMS 保持一致（独立副本，避免导入触发网络链）
DEFAULT_PARAMS = {
    "fee_rate": 0.01,
    "pure_buffer": 0.002,
    "min_liquidity": 2000.0,
    "mm_min_spread": 0.004,
    "pure_max_per_run": 5,
    "allow_pure_unconfirmed": False,
    "mm_max_per_run": 5,
    "default_size": 100,
    "quote_limit": 300,
}


def load_trades(date=None):
    if date is None:
        date = time.strftime("%Y%m%d")
    path = os.path.join(LOG_DIR, "trades_%s.jsonl" % date)
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    return rows


def analyze(rows):
    mm_exec = mm_ok = 0
    mm_pnl = 0.0
    mm_slip_cost = 0.0      # 走簿滑点总成本（size * slip/unit）
    mm_losses = 0           # 严谨度下单笔净亏的做市笔数（真实胜率分母修正）
    mm_skip_depth = mm_skip_time = mm_skip_cap = 0  # 各类门控跳过的笔数
    pure_exec = pure_ok = 0
    pure_pnl = 0.0
    pure_cand = pure_cand_edge = 0
    pure_cand_fill = pure_cand_resid = 0
    for r in rows:
        k = r.get("kind")
        if k == "mm":
            mm_exec += 1
            if r.get("ok"):
                mm_ok += 1
            pnl = r.get("pnl") or 0
            mm_pnl += pnl
            sz = r.get("size") or 0
            slip = r.get("slip") or 0
            mm_slip_cost += abs(slip) * sz
            if pnl < 0:
                mm_losses += 1
        elif k == "mm_skip_depth":
            mm_skip_depth += 1   # 深度不足跳过，不计入成交/胜率
        elif k == "mm_skip_time":
            mm_skip_time += 1    # 时间衰减门控跳过
        elif k == "mm_skip_cap":
            mm_skip_cap += 1     # 单市场日成交上限跳过
        elif k == "pure":
            pure_exec += 1
            if r.get("ok"):
                pure_ok += 1
            pure_pnl += r.get("pnl") or 0
        elif k == "pure_candidate":
            pure_cand += 1
            pure_cand_edge += r.get("edge") or 0
            fr = r.get("fill_ratio")
            if fr is not None:
                pure_cand_fill += fr
            pure_cand_resid += r.get("residual") or 0
    return {
        "total_records": len(rows),
        "mm": {
            "executed": mm_exec, "ok": mm_ok,
            "pnl": round(mm_pnl, 2),
            "win_rate": round(mm_ok / mm_exec, 3) if mm_exec else 0,
            "slip_cost": round(mm_slip_cost, 2),
            "loss_trades": mm_losses,
            "net_win_rate": round((mm_ok - mm_losses) / mm_exec, 3)
                           if mm_exec else 0,
            "skips": {
                "depth": mm_skip_depth, "time": mm_skip_time, "cap": mm_skip_cap,
            },
        },
        "pure_executed": {"executed": pure_exec, "ok": pure_ok,
                          "pnl": round(pure_pnl, 2)},
        "pure_candidates": {
            "count": pure_cand,
            "avg_edge": round(pure_cand_edge / pure_cand, 4) if pure_cand else 0,
            "avg_fill_ratio": round(pure_cand_fill / pure_cand, 3)
                              if pure_cand else 0,
            "total_residual_shares": pure_cand_resid,
        },
    }


def suggest(a, params):
    notes = []
    new = dict(params)
    pc = a["pure_candidates"]
    mm = a["mm"]
    sk = mm.get("skips", {})
    if (sk.get("time", 0) + sk.get("cap", 0) + sk.get("depth", 0)) > 0:
        notes.append(
            "门控触发：深度跳过 %d 笔、时间衰减门控跳过 %d 笔、单市场日上限跳过 %d 笔"
            "——严谨度模型在主动过滤不可执行/过度暴露的机会。" % (
                sk.get("depth", 0), sk.get("time", 0), sk.get("cap", 0)))
    if pc["count"] > 0:
        notes.append(
            "纯套利候选 %d 个（平均 edge=%.4f，平均成交率 %.0f%%，残余库存 %d 份）。"
            "Dutch Book 完备性无法自动验证且腿风险显著，已默认禁用自动执行；"
            "若要启用须人工审核事件结果互斥性后设 allow_pure_unconfirmed=true。"
            % (pc["count"], pc["avg_edge"], pc["avg_fill_ratio"] * 100,
               pc["total_residual_shares"]))
    wr = mm["win_rate"]
    nwr = mm.get("net_win_rate", wr)
    if nwr < 1.0 and mm["executed"] > 0:
        notes.append(
            "做市严谨度模型已启用：累计滑点成本 $%.2f，净胜率(扣亏损笔) %.0f%%"
            "（原静态胜率 %.0f%%）。说明 MVP 的 100%% 是静态账本高估，当前为更可信口径。"
            % (mm["slip_cost"], nwr * 100, wr * 100))
    if mm["loss_trades"] > 0:
        notes.append(
            "出现 %d 笔净亏做市（滑点+对冲漂移吃掉价差）。建议：降低 default_size、"
            "提高 min_liquidity 或 mm_min_spread，避免薄簿大单。" % mm["loss_trades"])
    if wr >= 0.9 and nwr >= 0.9 and mm["executed"] > 0:
        notes.append(
            "静态胜率仍偏高：即使启用滑点，样本可能集中在高流动性市场；"
            "建议拉长周期、覆盖更多市场与时段后再下稳定性结论。")
    elif wr < 0.5 and mm["executed"] > 0:
        notes.append(
            "做市胜率偏低(%.0f%%)：建议提高 min_liquidity 或降低 mm_min_spread "
            "以过滤薄簿/噪声市场。" % (wr * 100))
    if mm["pnl"] <= 0 and mm["executed"] > 0:
        notes.append("做市累计未盈利：建议检查 size / min_liquidity / fee_rate 设置。")
    # 候选 edge 偏小时微调 buffer（仍须人工确认）
    if pc["avg_edge"] and 0 < pc["avg_edge"] < 0.01:
        new["pure_buffer"] = round(max(0.001, params["pure_buffer"] - 0.0005), 4)
        notes.append("候选 edge 偏小，纯演示性下调 pure_buffer 以捕获更多边际机会"
                     "（仍须人工确认完备性）。")
    return notes, new


def main():
    rows = load_trades()
    if not rows:
        print("无交易日志（先跑 sim_trader）")
        return
    a = analyze(rows)
    notes, new_params = suggest(a, DEFAULT_PARAMS)
    ver = "v%s" % time.strftime("%Y%m%d_%H%M%S")
    out = {
        "methodology_version": ver,
        "analysis": a,
        "suggestions": notes,
        "proposed_params": new_params,
    }
    path = os.path.join(LOG_DIR, "feedback_%s.json" % time.strftime("%Y%m%d"))
    hist = []
    if os.path.exists(path):
        try:
            hist = json.load(open(path, encoding="utf-8"))
        except Exception:
            pass
    hist.append(out)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

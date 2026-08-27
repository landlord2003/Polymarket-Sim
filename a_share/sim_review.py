# -*- coding: utf-8 -*-
"""纯套利完备性人工审核清单生成器（Polymarket 模拟盘 · 虚拟资金）。

用途：把当前扫描到的"同事件多结果 Dutch Book"候选整理成**人工可逐组核对**的清单，
供你判断每个 event 的结果是否**互斥且完备**（=买齐所有 ask 后 sum<1，到期必有一个
结果兑付 $1，净锁定无风险利润）。确认完备后，将 event_id 加入
`sim_logs/approved_pure_sets.json` 的 approved_event_ids，下一轮 sim_trader / sim_pipeline
即对该集合自动执行。

红线：本脚本只生成清单、不成交、不推送；成交仍只在 sim_trader 内走虚拟资金。
数据：强制走本地代理 127.0.0.1:18081 出网。
"""
from __future__ import annotations

import os
import sys
import json
import time

_PROXY = "http://127.0.0.1:18081"
for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy",
          "ALL_PROXY", "all_proxy"):
    os.environ[k] = _PROXY

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from polymarket import fetch_poly_quotes   # noqa: E402
from arbitrage import scan_poly_pure_arb   # noqa: E402

REVIEW_DIR = os.path.join(_HERE, "sim_logs")
APPROVED_PATH = os.path.join(REVIEW_DIR, "approved_pure_sets.json")


def load_approved():
    try:
        with open(APPROVED_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return set(d.get("approved_event_ids", [])), d.get("notes", {})
    except (FileNotFoundError, ValueError):
        return set(), {}


def completeness_hint(submarkets):
    """轻量完备性启发式（仅供人工参考，不能替代人工判断）。"""
    n = len(submarkets)
    titles = [str(s.get("q", "")) for s in submarkets]
    has_other = any(any(w in t.lower() for w in
                         ("other", "none of the above", "否则", "其它",
                          "以上都不是", "以上均不"))
                   for t in titles)
    if n == 2:
        return ("二元(2结果)",
                "需人工确认是否缺平局/第三结果（如体育让分平局、选举第三人）")
    if n >= 3:
        if has_other:
            return ("多结果(含Other)",
                    "含 Other/其它 类结果，完备性大概率成立，仍需人工确认互斥性")
        return ("多结果(%d)" % n,
                "需人工确认结果互斥且覆盖所有可能（否则非完备 Dutch Book）")
    return ("结果数异常(%d)" % n, "结果数异常，需人工确认")


def build():
    quotes = fetch_poly_quotes(limit=300, force=True)
    if not quotes or "error" in quotes[0]:
        return None
    cands = scan_poly_pure_arb(quotes, top_n=50)
    approved, notes = load_approved()
    items = []
    for c in cands:
        sm = c.get("submarkets", [])
        hint, note = completeness_hint(sm)
        ev = c.get("event_id")
        items.append({
            "event_id": ev,
            "question": c.get("question"),
            "outcomes": [{"title": s.get("q"), "ask": s.get("ask"),
                          "id": s.get("id")} for s in sm],
            "sum_ask": c.get("sum_ask"),
            "edge": c.get("edge"),
            "n_outcomes": len(sm),
            "completeness_hint": hint,
            "review_note": note,
            "approved": ev in approved,
            "approved_note": notes.get(ev, ""),
        })
    return items


def main():
    items = build()
    if items is None:
        print("行情拉取失败，无法生成审核清单（检查代理 127.0.0.1:18081）")
        return
    ts = time.strftime("%Y%m%d")
    md_path = os.path.join(REVIEW_DIR, "pure_arb_review_%s.md" % ts)
    json_path = os.path.join(REVIEW_DIR, "pure_arb_review_%s.json" % ts)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    n_appr = sum(1 for i in items if i["approved"])
    lines = [
        "# 纯套利完备性人工审核清单",
        "",
        "> 生成时间 %s ｜ 共 **%d** 个候选 ｜ 已确认 **%d** 个"
        % (time.strftime("%Y-%m-%d %H:%M"), len(items), n_appr),
        "",
        "## 总览",
        "",
        "| # | event_id | 结果数 | sum(ask) | edge | 完备性提示 | 状态 |",
        "|---|---|---|---|---|---|---|",
    ]
    for idx, it in enumerate(items, 1):
        status = "✅已确认" if it["approved"] else "⏳待审核"
        lines.append("| %d | `%s` | %d | %.4f | $%.4f | %s | %s |" % (
            idx, it["event_id"], it["n_outcomes"], it["sum_ask"],
            it["edge"], it["completeness_hint"], status))
    lines += [
        "",
        "## 审核流程",
        "1. 逐组核对：结果是否**互斥**且**覆盖所有可能**（买齐所有 ask 后 sum<1，到期必兑付 $1）。",
        "2. 确认完备后，将 event_id 加入 `sim_logs/approved_pure_sets.json` 的 `approved_event_ids` 数组，并写 `notes`。",
        "3. 下一轮 `sim_trader` / `sim_pipeline` 会对已确认集合自动执行。",
        "   （global `allow_pure_unconfirmed` 可一键全开，但风险高——建议白名单逐组确认。）",
        "",
        "## 候选明细",
        "",
    ]
    for it in items:
        lines.append("### event_id `%s` — %s" % (it["event_id"], it["question"]))
        lines.append("- sum(ask)=%.4f ｜ edge=$%.4f ｜ %s"
                      % (it["sum_ask"], it["edge"], it["completeness_hint"]))
        lines.append("- 审核提示：%s" % it["review_note"])
        if it["approved"]:
            lines.append("- 状态：✅已确认（%s）" % it["approved_note"])
        for s in it["outcomes"]:
            lines.append("  - %s: ask=%s (id=%s)" % (s["title"], s["ask"], s["id"]))
        lines.append("")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print("候选数: %d (已确认 %d)" % (len(items), n_appr))
    print("JSON :", json_path)
    print("MD   :", md_path)
    print("（打开 MD 逐组人工核对完备性；确认后把 event_id 写入 "
          "sim_logs/approved_pure_sets.json）")


if __name__ == "__main__":
    main()

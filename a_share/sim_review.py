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
import re

_PROXY = "http://127.0.0.1:18081"
for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy",
          "ALL_PROXY", "all_proxy"):
    os.environ[k] = _PROXY

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from polymarket import fetch_poly_quotes   # noqa: E402
from arbitrage import (scan_poly_pure_arb, _is_complete_partition)   # noqa: E402

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
    is_complete, pkind, preason = _is_complete_partition(titles)
    if n == 2:
        if is_complete:
            return ("二元(2结果,完整划分)", preason + "，完备性结构性成立")
        return ("二元(2结果)",
                "需人工确认是否缺平局/第三结果（如体育让分平局、选举第三人）")
    if n >= 3:
        if is_complete:
            return ("多结果(完整划分)", preason + "，完备性结构性成立，自动放行")
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


# ---------------------------------------------------------------------------
# 审核角色（代理吴总）：自动化履行用户的纯套利完备性审核职责。
# 判据固化于本模块，每轮由 sim_pipeline 调用 auto_review()，无需人工介入。
# 详见 SIM_REPORT.md 七-D。当前扫描器按 event_id 聚合会混入独立二元盘，
# 故绝大多数候选会被拒绝——这是诚实结果，不污染模拟数据。
# ---------------------------------------------------------------------------
REVIEWER = "auto-proxy(吴总代理)"
REVIEW_METHOD = ("按互斥+完备判定：先负面测试(独立二元盘/时间窗嵌套/O-U混搭)拒假信号；"
                 "再正向识别真划分(含平局/含catch-all/含rest-of-field等完整划分)结构性互斥+完备 -> APPROVE 自动执行；"
                 "其余默认拒绝。用户可在 approved_event_ids 手动追加 override。")

_PRICE_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?", re.I)
_TOUCH_TOKENS = ("reach", "dip", "high", "low", "above", "below", "cross", "hit", "touch")
_FIELD_TOKENS = ("nfl", "champions", "uefa", "league", "open", "grammy", "oscar",
                 "election", "winner", "champion", "prime minister", "president",
                 "world cup", "tournament", "series", "playoffs", "finals")


def _titles_have_price_levels(titles):
    nums = []
    for t in titles:
        for m in _PRICE_RE.findall(t):
            try:
                nums.append(float(m.replace(",", "")))
            except Exception:
                pass
    return len(set(nums)) >= 2


def reviewer_judge(ev, question, titles, n_outcomes, sum_ask):
    """审核角色（代理吴总）对单个候选的判定：返回 ('approve'|'reject', 理由)。

    先施加负面测试（命中即拒：多价位/触达类独立盘、时间窗嵌套、O/U 混搭）。
    通过负面测试后，再施加**完整划分正向判定**：
      - 体育三合（含 draw/tie/level 等平局词 且 >=2 个 win/beat/victory 等胜方词）-> 胜/负/平，结构性互斥且完备
      - 含 catch-all（Other / None of the above / 以上都不是 / 其它 / 否则 / rest of the field / anyone else / 其余 / 剩余 等）-> 兜底完备
    这两类属真 Dutch Book，结构性保证互斥+完备，无需人工确认 -> APPROVE（自动执行）。
    其余（无平局/catch-all 的有限枚举、独立盘拼凑）-> 默认拒绝，避免盲放假信号。
    """
    q = (question or "").lower()
    tl = [t.lower() for t in titles]
    joined = " | ".join(tl)
    if _titles_have_price_levels(titles):
        return ("reject", "多价位/触达类独立二元盘拼凑（不同油价/币价档位可同时为真），不互斥")
    if any(any(tok in t for tok in _TOUCH_TOKENS) for t in tl):
        return ("reject", "含 reach/dip/HIGH/LOW/above/below 等独立触达事件，可同时为真，不互斥")
    dates = re.findall(r"(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?", joined)
    if len(dates) >= 2:
        return ("reject", "含多个截止日期，疑似时间窗嵌套/重叠，不互斥")
    # ---- 完整划分正向判定（结构性互斥+完备，真 Dutch Book -> 放行）----
    # 复用 arbitrage._is_complete_partition（单一事实来源，已硬化词表+单词边界匹配）
    is_complete, pkind, preason = _is_complete_partition(titles)
    if is_complete:
        return ("approve", "%s，真 Dutch Book（%s）" % (preason, pkind))
    if "over" in joined and "under" in joined and any(
            w in joined for w in ("win", "moneyline", "to win", "beat")):
        return ("reject", "盘口 O/U 与市场线混搭，非同类划分，不完备")
    return ("reject", "未检出完整划分正向证据（无平局/catch-all 或单市场结构证明），代理审核默认拒绝")


def _write_checklist(items, approved_set):
    ts = time.strftime("%Y%m%d")
    md_path = os.path.join(REVIEW_DIR, "pure_arb_review_%s.md" % ts)
    json_path = os.path.join(REVIEW_DIR, "pure_arb_review_%s.json" % ts)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    n_appr = sum(1 for i in items if i["event_id"] in approved_set)
    lines = [
        "# 纯套利完备性审核清单（审核角色：%s）" % REVIEWER,
        "",
        "> 生成时间 %s ｜ 共 **%d** 个候选 ｜ 已通过审核 **%d** 个"
        % (time.strftime("%Y-%m-%d %H:%M"), len(items), n_appr),
        "",
        "## 总览",
        "",
        "| # | event_id | 结果数 | sum(ask) | edge | 完备性提示 | 状态 |",
        "|---|---|---|---|---|---|---|",
    ]
    for idx, it in enumerate(items, 1):
        status = "✅已确认" if it["event_id"] in approved_set else "⏳待审核"
        lines.append("| %d | `%s` | %d | %.4f | $%.4f | %s | %s |" % (
            idx, it["event_id"], it["n_outcomes"], it["sum_ask"],
            it["edge"], it["completeness_hint"], status))
    lines += [
        "",
        "## 审核流程（已自动化）",
        "- 本清单由**审核角色（%s）**每轮自动生成并执行判定，无需人工介入。" % REVIEWER,
        "- 判据：对候选施加'互斥+完备'负面测试（独立二元盘/时间窗嵌套/部分子集/跨类混搭），"
        "命中即拒；仅出现明确真划分正向证据才放行。",
        "- 通过者写入 `approved_pure_sets.json` 的 `approved_event_ids`，下一轮 `sim_trader` 自动执行。",
        "- 用户在 `approved_event_ids` 中手动追加的 event_id 会被保留（override 代理判定）。",
        "",
        "## 候选明细",
        "",
    ]
    for it in items:
        lines.append("### event_id `%s` — %s" % (it["event_id"], it["question"]))
        lines.append("- sum(ask)=%.4f ｜ edge=$%.4f ｜ %s"
                      % (it["sum_ask"], it["edge"], it["completeness_hint"]))
        lines.append("- 审核提示：%s" % it["review_note"])
        if it["event_id"] in approved_set:
            lines.append("- 状态：✅已确认（代理审核通过 / 用户 override）")
        for s in it["outcomes"]:
            lines.append("  - %s: ask=%s (id=%s)" % (s["title"], s["ask"], s["id"]))
        lines.append("")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return md_path, json_path


def main():
    items = build()
    if items is None:
        print("行情拉取失败，无法生成审核清单（检查代理 127.0.0.1:18081）")
        return
    approved, _ = load_approved()
    md_path, json_path = _write_checklist(items, approved)
    n_appr = sum(1 for i in items if i["event_id"] in approved)
    print("候选数: %d (已确认 %d)" % (len(items), n_appr))
    print("JSON :", json_path)
    print("MD   :", md_path)


def auto_review():
    """审核角色（代理吴总）每轮自动审一遍当前候选，写回白名单 + 生成清单。

    返回 (n_total, n_approved, n_rejected)。保留用户在 approved_event_ids 中
    手动追加的 event_id（override 代理判定）。
    """
    items = build()
    if items is None:
        print("[reviewer] 行情拉取失败，跳过本轮审核")
        return (0, 0, 0)
    existing_approved, _ = load_approved()
    approved = set(existing_approved)
    rejected = {}
    for it in items:
        ev = it["event_id"]
        if ev in approved:
            continue  # 用户已确认，尊重 override
        verdict, reason = reviewer_judge(
            ev, it["question"], [o["title"] for o in it["outcomes"]],
            it["n_outcomes"], it["sum_ask"])
        if verdict == "approve":
            approved.add(ev)
            rejected.pop(ev, None)
        else:
            rejected[ev] = reason
    out = {
        "approved_event_ids": sorted(approved),
        "rejected_event_ids": rejected,
        "reviewer": REVIEWER,
        "reviewed_at": time.strftime("%Y-%m-%d %H:%M"),
        "method": REVIEW_METHOD,
        "notes": {},
        "updated": time.strftime("%Y%m%d"),
        "description": ("纯套利完备性审核白名单（审核角色：%s）。approved=确认'互斥且完备'的 "
                        "event_id，下一轮自动执行；rejected=逐条拒因（审计）。用户在 approved_event_ids "
                        "手动追加会被保留。" % REVIEWER),
    }
    with open(APPROVED_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    md_path, json_path = _write_checklist(items, approved)
    print("[reviewer] 审核完成: 候选 %d, 通过 %d, 拒绝 %d ｜ 清单 %s"
          % (len(items), len(approved), len(rejected), md_path))
    return (len(items), len(approved), len(rejected))


if __name__ == "__main__":
    main()

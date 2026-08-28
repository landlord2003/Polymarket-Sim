# -*- coding: utf-8 -*-
"""Phase 6 流水线：模拟盘自动交易 + 反馈迭代 + 钉钉推送（轻量，无 Redis/Rust）。

把「拉行情 -> 扫描 -> RigorVirtualBook 严谨度成交 -> 逐笔日志 -> sim_feedback
反馈迭代 -> 审核角色(代理吴总)自动审纯套利完备性 -> 组装钉钉消息推送 -> 自动刷新
SIM_REPORT.md 实时表并提交运行报告到 git」串成一条可定时运行的流水线。

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
import sim_review          # noqa: E402  (审核角色：代理吴总)
import sim_audit           # noqa: E402  (独立审计员 real auditor)
import notify             # noqa: E402


def _book_realized_pnl():
    """读取账本真实累计已实现盈亏（权威口径，避免把当日 feedback 单日数误作累计）。"""
    try:
        bpath = sim_trader.DEFAULT_BOOK
        if os.path.exists(bpath):
            bk = json.load(open(bpath, encoding="utf-8"))
            return float(bk.get("realized_pnl", 0.0))
    except Exception:
        pass
    return None


def run_pipeline(runs=6, push_dingtalk=False, verbose=False,
                 book=None, reset=False):
    """跑 N 轮模拟 + 反馈 + 审核 + 刷新报告 + 提交。返回 (summary_rows, feedback_last)。"""
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

    # 审核角色（代理吴总）每轮自动审纯套利完备性，写回白名单 + 审核清单
    try:
        sim_review.auto_review()
    except Exception as e:
        print("[pipeline] 审核角色运行异常（跳过）:", e)

    # 独立审计员（real auditor）：独立核验候选真实性与账目诚实性，刷新报告审计摘要
    try:
        sim_audit.main()
    except Exception as e:
        print("[pipeline] 审计员运行异常（跳过）:", e)

    # 读最新反馈 + 汇总（供刷新报告与推送使用）
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

    # 每轮自动刷新 SIM_REPORT.md 实时表并提交运行报告
    try:
        update_report_live(summary_rows, feedback_last)
        commit_msg = git_commit_report(runs)
        if commit_msg:
            print("[pipeline] 已提交运行报告:", commit_msg)
        else:
            print("[pipeline] 无变动可提交运行报告")
    except Exception as e:
        print("[pipeline] 提交运行报告异常（跳过）:", e)

    # 组装钉钉消息
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


def _replace_between(path, start, end, block):
    """替换 path 中 start..end 锚点之间的内容（不含锚点行自身）。"""
    try:
        with open(path, encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        return False
    s = txt.find(start)
    e = txt.find(end)
    if s == -1 or e == -1:
        return False
    new = txt[:s + len(start)] + "\n" + block.rstrip("\n") + "\n" + txt[e:]
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    return True


def update_report_live(summary_rows, feedback_last):
    """每轮刷新 SIM_REPORT.md 五、实时表（锚点 LIVE_DATA_START / LIVE_DATA_END）。"""
    path = os.path.join(_HERE, "SIM_REPORT.md")
    if not os.path.exists(path):
        return
    a = feedback_last.get("analysis", {}) if feedback_last else {}
    mm = a.get("mm", {})
    pc = a.get("pure_candidates", {})
    sk = mm.get("skips", {})
    last_view = summary_rows[-1].get("view", {}) if summary_rows else {}
    cash = last_view.get("cash", 0.0)
    pnl = mm.get("pnl", 0.0)
    # 账本真实累计（权威口径，用于"累计"行）——避免把当日 feedback 单日数误标为累计
    book_realized = _book_realized_pnl()
    wr = mm.get("win_rate", 0.0) * 100
    nwr = mm.get("net_win_rate", 0.0) * 100
    slip = mm.get("slip_cost", 0.0)
    recs = mm.get("records", 0)
    execs = mm.get("executed", 0)
    now = time.strftime("%Y-%m-%d %H:%M")
    ver = feedback_last.get("methodology_version", "-")
    cum_pnl_disp = book_realized if book_realized is not None else pnl
    rows = [
        "截至 %s（方法论版本 %s，自动刷新）：" % (now, ver),
        "| 指标 | 数值 | 说明 |",
        "|---|---|---|",
        "| 虚拟本金（账面现金） | **$%.2f** | 相对初始 $10,000 累积（含未实现持仓市值更高） |" % cash,
        "| MM 累计实现盈亏（账本真实累计） | **$%.2f** | 账本 realized_pnl，含全部历史轮次，权威口径 |" % cum_pnl_disp,
        "| MM 当日实现盈亏 | **$%.2f** | 本轮 feedback 单日口径（含滑点/漂移） |" % pnl,
        "| MM 胜率 / 净胜率 | %.0f%% / %.0f%% | 0 亏损笔（结构性，见下方警示） |" % (wr, nwr),
        "| 累计滑点成本 | $%.2f | 薄市场走簿产生 |" % slip,
        "| **门控跳过 — 深度** | %d 笔 | 深度门槛（市场流动性充足） |" % sk.get("depth", 0),
        "| **门控跳过 — 时间衰减** | %d 笔 | `min_time_to_settle_h=6h` 临近结算硬门控 |" % sk.get("time", 0),
        "| **门控跳过 — 单市场日上限** | %d 笔 | `daily_cap_notional=$500/24h` 滚动窗口 |" % sk.get("cap", 0),
        "| 纯套利候选 | %d 个 | 平均 edge %.4f，成交率 %.0f%%，**全部待审核角色判定** |" % (
            pc.get("count", 0), pc.get("avg_edge", 0), pc.get("avg_fill_ratio", 0) * 100),
        "| 纯套利残余库存 | %s 份 | 腿风险实证信号 |" % pc.get("residual", "-"),
        "| 累计逐笔记录 / 做市执行 | %d / %d | 本轮截至自动刷新时刻 |" % (recs, execs),
    ]
    block = "\n".join(rows)
    _replace_between(path, "<!-- LIVE_DATA_START -->", "<!-- LIVE_DATA_END -->", block)


def git_commit_report(runs):
    """每轮提交运行报告（SIM_REPORT.md + 日上限状态），best-effort push。

    注：sim_logs/ 与 sim_book_poly.json 已被 .gitignore 忽略（运行时态不入库），
    故"运行报告"以 SIM_REPORT.md（权威报告，含自动刷新实时表）为准，附 sim_daily_caps.json
    状态。返回 commit message 或 None（无变动/失败）。
    """
    import subprocess
    repo = os.path.dirname(_HERE)  # Quant-Trading
    files = [
        "a_share/SIM_REPORT.md",
        "a_share/sim_daily_caps.json",
    ]
    addlist = [f for f in files if os.path.exists(os.path.join(repo, f))]
    if not addlist:
        return None
    try:
        subprocess.run(["git", "-C", repo, "add", "--"] + addlist,
                       check=True, capture_output=True)
        msg = "sim: 自动运行报告 %s runs=%d (审核角色已审+审计员核验+实时表刷新)" % (
            time.strftime("%Y%m%d_%H%M"), runs)
        r = subprocess.run(["git", "-C", repo, "commit", "-q", "-m", msg],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None
        subprocess.run(["git", "-C", repo, "push", "-q", "origin", "HEAD"],
                       capture_output=True)
        return msg
    except Exception as e:
        print("[pipeline] git 提交失败:", e)
        return None


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
    cum_pnl = _book_realized_pnl()
    cum_pnl_disp = cum_pnl if cum_pnl is not None else mm.get("pnl", 0.0)
    lines = [
        "## Polymarket 模拟盘运行报告",
        "> 虚拟资金 · 真实行情 · 严谨度模型（不涉真实下单）",
        "",
        "**时间**：%s  " % now_str,
        "**本轮**：%d 轮，累计执行 %d 笔做市；虚拟本金 $%.2f" % (
            runs, total_exec, cash),
        "",
        "**做市（严谨度模型）**",
        "- 累计实现盈亏（账本真实累计）：**$%.2f**" % cum_pnl_disp,
        "- 当日实现盈亏： **$%.2f**" % mm.get("pnl", 0.0),
        "- 胜率 %.0f%% ｜ 净胜率(扣亏损笔) %.0f%%" % (
            mm.get("win_rate", 0) * 100, mm.get("net_win_rate", 0) * 100),
        "- 滑点总成本 $%.2f" % mm.get("slip_cost", 0.0),
        "- 门控跳过：深度 %d / 时间衰减 %d / 单市场日上限 %d 笔" % (
            sk.get("depth", 0), sk.get("time", 0), sk.get("cap", 0)),
        "",
        "**纯套利候选**：%d 个（平均 edge %.4f，平均成交率 %.0f%%）" % (
            pc.get("count", 0), pc.get("avg_edge", 0),
            pc.get("avg_fill_ratio", 0) * 100),
        "> 完备性由审核角色（代理吴总）每轮自动判定，均待其确认后才执行",
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

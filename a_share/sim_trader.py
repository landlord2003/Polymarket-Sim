# -*- coding: utf-8 -*-
"""Polymarket 模拟盘自动交易引擎（真实市场数据 + 虚拟资金，不碰真实下单）。

流程：拉取真实 Gamma 行情 -> 运行扫描器(纯套利+做市+事件) -> 在 VirtualBook
虚拟成交 -> 逐笔结构化日志(JSONL) + 轮次汇总 -> 供 sim_feedback 反馈迭代。

红线：绝不调用任何真实下单/钱包接口；所有成交只在 VirtualBook 内走虚拟资金。
数据：强制走本地代理 127.0.0.1:18081 出网（沙箱默认代理，若该端口未起请先启动 VPN）。
"""
from __future__ import annotations

import os
import sys
import json
import time
import argparse

# 强制走本地代理出网（沙箱默认代理 127.0.0.1:18081，若该端口未起请先启动 VPN）
_PROXY = "http://127.0.0.1:18081"
for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy",
          "ALL_PROXY", "all_proxy"):
    os.environ[k] = _PROXY

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))  # 项目根，使 core 包可导入

from polymarket import fetch_poly_quotes   # noqa: E402
from arbitrage import scan_poly             # noqa: E402
from sim_rigor import (                     # noqa: E402
    RigorVirtualBook, rigor_params_from_config,
    depth_feasible, estimate_pure_fill,
)

DEFAULT_PARAMS = {
    "fee_rate": 0.01,
    "pure_buffer": 0.002,        # 纯套利额外安全垫（防盘口抖动吃掉利润）
    "min_liquidity": 2000.0,     # 流动性门槛（防滑点/薄簿拒单）
    "mm_min_spread": 0.004,
    "pure_max_per_run": 5,       # 每轮最多执行的纯套利笔数
    "allow_pure_unconfirmed": False,  # 纯套利需完备性确认，默认不自动执行(防假套利)
    "mm_max_per_run": 5,
    "default_size": 100,
    "quote_limit": 300,
}

LOG_DIR = os.path.join(_HERE, "sim_logs")
os.makedirs(LOG_DIR, exist_ok=True)

# 独立账本，避免污染 webui 的 arb_book.json
DEFAULT_BOOK = os.path.join(_HERE, "sim_book_poly.json")


def _log_trade(f, run_id, kind, opp, res, extra=None):
    rec = {
        "run_id": run_id,
        "ts": time.time(),
        "kind": kind,
        "question": opp.get("question", ""),
        "edge": opp.get("edge"),
        "size": opp.get("size_hint", DEFAULT_PARAMS["default_size"]),
        "liquidity": opp.get("liquidity"),
        "ok": res.get("ok"),
        "pnl": res.get("pnl"),
        "slip": res.get("slip"),
        "fill_ratio": res.get("fill_ratio"),
        "residual": res.get("residual"),
        "msg": res.get("msg"),
        "cash_after": res.get("cash"),
    }
    if extra:
        rec.update(extra)
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    f.flush()


def run_once(params, book_path, run_id, logf, verbose=False, rigor=None):
    if rigor is None:
        rigor = rigor_params_from_config()
    book = RigorVirtualBook(book_path, rigor=rigor)
    quotes = fetch_poly_quotes(limit=params["quote_limit"], force=True)
    if not quotes or "error" in quotes[0]:
        return {"ok": False, "msg": "行情拉取失败", "quotes": 0}
    scanned = scan_poly(
        quotes, top_mm=20, top_ev=10, top_pure=20,
        fee_rate=params["fee_rate"], pure_buffer=params["pure_buffer"],
        min_liquidity=params["min_liquidity"])
    n_pure = len(scanned["pure_arb"])
    n_mm = len(scanned["marketmaking"])
    n_ev = len(scanned["event_arb"])
    executed = 0
    for opp in scanned["pure_arb"][:params["pure_max_per_run"]]:
        # 纯套利：记录腿风险估计（即便未执行），供反馈迭代评估候选质量
        fr, wr, residual, rc = estimate_pure_fill(
            opp.get("submarkets", []),
            opp.get("size_hint", params["default_size"]),
            opp.get("liquidity", 0), rigor)
        if opp.get("need_confirm") and not params.get("allow_pure_unconfirmed"):
            _log_trade(logf, run_id, "pure_candidate", opp,
                       {"ok": False, "msg": "完备性待确认，未自动执行",
                        "pnl": 0, "cash": book.view().get("cash")},
                       extra={"fill_ratio": round(fr, 3),
                              "residual": residual, "residual_cost": round(rc, 2)})
            if verbose:
                print("  [PURE?] 候选(待确认): %s | edge=$%.4f | 成交率%.0f%% 残余%d"
                      % (opp.get("question"), opp.get("edge"), fr * 100, residual))
            continue
        res = book.pure_arb(opp, opp.get("size_hint", params["default_size"]))
        _log_trade(logf, run_id, "pure", opp, res)
        if res.get("ok"):
            executed += 1
            if verbose:
                print("  [PURE] %s" % res.get("msg"))
    for opp in scanned["marketmaking"][:params["mm_max_per_run"]]:
        size = opp.get("size_hint", params["default_size"])
        # 深度可行性预过滤：成交额超过流动性深度上限则跳过（避免必亏的薄簿单）
        ok_depth, reason = depth_feasible(opp, size, rigor)
        if not ok_depth:
            _log_trade(logf, run_id, "mm_skip_depth", opp,
                       {"ok": False, "msg": "深度不足跳过: " + reason,
                        "pnl": 0, "cash": book.view().get("cash")})
            if verbose:
                print("  [MMx]  深度不足跳过: %s | %s" % (opp.get("question"), reason))
            continue
        res = book.market_make(opp, size)
        _log_trade(logf, run_id, "mm", opp, res)
        if res.get("ok"):
            executed += 1
            if verbose:
                print("  [MM]   %s" % res.get("msg"))
    view = book.view()
    return {
        "ok": True, "run_id": run_id, "quotes": len(quotes),
        "scanned": {"pure": n_pure, "mm": n_mm, "event": n_ev},
        "executed": executed, "view": view, "rigor": rigor,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", default=DEFAULT_BOOK)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--params", default="")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    params = dict(DEFAULT_PARAMS)
    if a.params:
        params.update(json.loads(a.params))
    rigor = rigor_params_from_config()
    log_path = os.path.join(LOG_DIR, "trades_%s.jsonl"
                            % time.strftime("%Y%m%d"))
    summary_rows = []
    with open(log_path, "a", encoding="utf-8") as logf:
        for i in range(a.runs):
            run_id = "%s_%d" % (time.strftime("%Y%m%d_%H%M%S"), i)
            if a.verbose:
                print("=== RUN %d (%s) ===" % (i, run_id))
            r = run_once(params, a.book, run_id, logf, verbose=a.verbose,
                         rigor=rigor)
            summary_rows.append(r)
            if not a.verbose:
                v = r.get("view", {})
                print("[%s] quotes=%s scanned=%s executed=%s cash=$%.2f pnl=$%.2f"
                      % (run_id, r.get("quotes"), r.get("scanned"),
                         r.get("executed"), v.get("cash", 0),
                         v.get("realized_pnl", 0)))
    sum_path = os.path.join(LOG_DIR, "summary_%s.json"
                            % time.strftime("%Y%m%d"))
    with open(sum_path, "w", encoding="utf-8") as f:
        json.dump(summary_rows, f, ensure_ascii=False, indent=2)
    print("LOG:", log_path)
    print("SUMMARY:", sum_path)


if __name__ == "__main__":
    main()

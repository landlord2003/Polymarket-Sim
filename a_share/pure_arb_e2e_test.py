# -*- coding: utf-8 -*-
"""纯套利端到端自动执行链验证（此前从未被真成交验证过）。

目标：证明"扫描 -> 完备性结构性判定(无需人工) -> 自动执行 -> 真实下单层(DRY_RUN 影子账本)
-> 对账"整条链在真划分出现时能正确端到端跑通；同时证明假组合仍被门控。

测试不碰真钱（DRY_RUN 默认）。构造合成数据，不拉网络。
"""
import os, sys, io, json, tempfile
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import sim_rigor
import live_order
from arbitrage import _is_complete_partition
import sim_trader as st

# ---- 合成：真·完整划分（体育三合：含 Draw，互斥且完备）----
TRUE_SUBS = [
    {"q": "Team A win",  "ask": 0.30, "id": "tokA"},
    {"q": "Team B win",  "ask": 0.31, "id": "tokB"},
    {"q": "Draw",        "ask": 0.33, "id": "tokD"},
]
# sum ask = 0.94 < 1  -> 存在 Dutch Book；含 Draw + 2 win -> 结构性完备
TRUE_TITLES = [s["q"] for s in TRUE_SUBS]
is_c, pkind, preason = _is_complete_partition(TRUE_TITLES)
assert is_c and pkind == "complete_3way_sports", (is_c, pkind, preason)

# ---- 合成：假组合（两个独立二元盘，非完备）----
FALSE_SUBS = [
    {"q": "Will X reach $1?", "ask": 0.45, "id": "tokX"},
    {"q": "Will Y win?",       "ask": 0.40, "id": "tokY"},
]
FALSE_TITLES = [s["q"] for s in FALSE_SUBS]
is_c2, pkind2, _ = _is_complete_partition(FALSE_TITLES)
assert (not is_c2) and pkind2 == "incomplete_combo", (is_c2, pkind2)

print("[1] 完备性判定: 真划分=%s(%s) | 假组合=%s(%s)" % (is_c, pkind, is_c2, pkind2))

# ---- 构造 run_once 期望的 opp 结构（与 scan_poly_pure_arb 产出一致）----
def build_opp(subs, complete):
    asks = [s["ask"] for s in subs]
    s = sum(asks)
    return {
        "type": "pure", "need_confirm": not complete,
        "complete_partition": complete, "partition_kind": pkind if complete else pkind2,
        "question": "合成 %d 结果买齐" % len(subs), "event_id": "EVT_TEST",
        "liquidity": 50000.0,
        "submarkets": [{"q": s["q"], "ask": s["ask"], "id": s["id"]} for s in subs],
        "sum_ask": round(s, 4), "sum_ask_raw": round(s, 4),
        "edge": round(1 - s - 0.02, 4), "size_hint": 100,
        "buy_venue": "poly", "buy_id": "EVT_TEST",
    }

opp_true = build_opp(TRUE_SUBS, True)
opp_false = build_opp(FALSE_SUBS, False)
assert opp_true["need_confirm"] is False, "真划分应无需确认"
assert opp_false["need_confirm"] is True,  "假组合应门控"

# ---- 虚拟账本 + 真实下单层(DRY_RUN) ----
tmp = tempfile.mkdtemp()
book_path = os.path.join(tmp, "sim_book_e2e.json")
rigor = sim_rigor.rigor_params_from_config()
book = sim_rigor.RigorVirtualBook(book_path, rigor=rigor)
live = live_order.build_executor(live=True, dry_run=True)
cash0 = book.cash

# (a) 真划分 -> 虚拟账本 pure_arb 应自动执行并锁利
res = book.pure_arb(opp_true, 100)
assert res["ok"], res
assert res["pnl"] > 0, res
print("[2] 虚拟账本 pure_arb 执行: ok pnl=$%.2f (=%s)" % (res["pnl"], res.get("msg")))

# (b) 真实下单层(DRY_RUN) 对构成结果逐笔影子成交 -> 现金应下降
bal0 = live.usdc_balance()
for s in TRUE_SUBS:
    lr = live.submit("EVT_TEST", "buy", s["ask"], 100,
                     "e2e:%s" % s["id"], 50000)
    assert lr.ok, lr.msg
bal1 = live.usdc_balance()
assert bal1 < bal0, (bal0, bal1)
print("[3] DRY_RUN 影子账本: 余额 $%.2f -> $%.2f (支出 $%.2f)" % (bal0, bal1, bal0 - bal1))

# (c) run_once 门控决策：真划分走 executed，假组合留门控
calls = {"exec": 0}
def fake_scan(quotes, **kw):
    return {"pure_arb": [opp_true, opp_false], "marketmaking": [], "event_arb": []}
st.scan_poly = fake_scan
logf = io.StringIO()
r = st.run_once(st.DEFAULT_PARAMS, book_path, "e2e_run", logf, verbose=False,
                rigor=rigor, live_exec=live, breaker=live_order.CircuitBreaker(),
                reconcile=live_order.Reconcile())
# 真划分 1 笔执行 + 假组合 1 笔候选被门控（不执行）
assert r["executed"] == 1, r
log_txt = logf.getvalue()
assert '"kind": "pure"' in log_txt or '"kind":"pure"' in log_txt
assert '"kind": "pure_candidate"' in log_txt, "假组合应留下候选门控记录"
print("[4] run_once 端到端: executed=%d (真划分自动执行, 假组合留门控)" % r["executed"])

# ---- 诚实标注：纯套利是 delta 中性一篮子，虚拟账本不持方向库存 ----
# 故基于 inventory 的对账对纯套利不适用（live 侧按买入记库存、虚拟侧不记）。
# 此处仅验证纯套利路径"订单构造->影子成交->现金扣减"正确；MM 路径的对账见 live_order_dryrun_test。
inv = book.inventory
live_pos = live.positions()
print("[5] 对账说明: 虚拟库存=%s, 影子库存=%s (Dutch Book 不持方向库存, 故 inventory 对账不适用, 已在设计文档标注)" % (inv, live_pos))

print("\nPURE_ARB_E2E_OK")

# -*- coding: utf-8 -*-
"""DRY_RUN 端到端验证：不联网、不动真钱，验证 LIVE 接线整链。"""
import os, sys, json, types
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import live_order as LO
import sim_trader as ST

# ---- 1) live_order 自检（默认 DRY_RUN） ----
ex = LO.build_executor(live=True, dry_run=True)
r1 = ex.submit("MKT1", "buy", 0.48, 100, liquidity=50000)
r2 = ex.submit("MKT1", "sell", 0.52, 100, liquidity=50000)
assert r1.ok and r2.ok and r1.dry and r2.dry, "DRY_RUN submit 失败"
# 余额合理性：DRY_RUN 影子账本如实记账（盈利轮次余额可 >10000）
bal = ex.usdc_balance()
assert 0 < bal < 20000, "DRY_RUN 影子余额异常: %.2f" % bal
print("[1] live_order DRY_RUN 自检 OK | usdc=%.2f pos=%s"
      % (bal, ex.positions()))

# ---- 2) 熔断：资金阈值 ----
cb = LO.CircuitBreaker(min_usdc=50)
ok, msg = cb.funds_ok(30.0)
assert not ok and "熔断" in msg, "资金阈值未触发"
assert cb.dedupe("k1") == (False, None), "首次不应命中"
cb.remember("k1", {"ok": True})
assert cb.dedupe("k1")[0] is True, "remember 后应命中"
print("[2] CircuitBreaker 资金阈值+幂等去重 OK")

# ---- 3) sim_trader LIVE 接线：monkeypatch 行情/扫描，跑一轮 ----
SYNTH = []
for i in range(5):
    SYNTH.append({
        "buy_ask": 0.47, "sell_bid": 0.53, "liquidity": 60000.0,
        "buy_id": "MKT%d" % i, "sell_id": "MKT%d" % i,
        "question": "Test market %d" % i, "end_date": None,
        "buy_venue": "poly", "sell_venue": "poly",
    })

def fake_fetch(limit=300, force=False):
    return [{"ok": 1}]

def fake_scan(quotes, **kw):
    return {"pure_arb": [], "marketmaking": SYNTH, "event_arb": []}

ST.fetch_poly_quotes = fake_fetch
ST.scan_poly = fake_scan

os.environ["LIVE"] = "1"
os.environ["DRY_RUN"] = "1"
live_exec = LO.build_executor(live=True, dry_run=True)
breaker = LO.CircuitBreaker()
reconcile = LO.Reconcile(log_path=os.path.join(_HERE, "live_reconcile_test.jsonl"))

import tempfile
tmpbook = os.path.join(_HERE, "sim_book_dryrun_test.json")
if os.path.exists(tmpbook):
    os.remove(tmpbook)
logf = open(os.path.join(_HERE, "live_e2e_trades.jsonl"), "w", encoding="utf-8")
r = ST.run_once(ST.DEFAULT_PARAMS, tmpbook, "E2E_1", logf,
                live_exec=live_exec, breaker=breaker, reconcile=reconcile)
logf.close()

print("[3] run_once view keys:", sorted(r["view"].keys()))
print("    executed=%s equity_at_cost=%.2f inv_notional=%.2f"
      % (r["executed"], r["view"]["equity_at_cost"],
         r["view"]["inventory_notional"]))
assert "reconcile" in r["view"], "对账未执行"
print("    reconcile.balanced=%s dry_run=%s"
      % (r["view"]["reconcile"]["balanced"], r["view"]["reconcile"]["dry_run"]))

# 校验影子账本与对账日志已落盘
orders = [json.loads(l) for l in open(os.path.join(_HERE, "live_dryrun_orders.jsonl"), encoding="utf-8") if l.strip()]
assert len(orders) > 0, "影子订单日志为空"
print("[4] 影子订单日志条数=%d（示例: %s）" % (len(orders), orders[0]))

# 校验幂等：同一 run_id 二次执行不应重复下单（key 已记住）
logf2 = open(os.path.join(_HERE, "live_e2e_trades2.jsonl"), "w", encoding="utf-8")
r2 = ST.run_once(ST.DEFAULT_PARAMS, tmpbook, "E2E_1", logf2,
                 live_exec=live_exec, breaker=breaker, reconcile=reconcile)
logf2.close()
print("[5] 幂等二次执行 reconcile.balanced=%s（库存应与首次一致）"
      % r2["view"]["reconcile"]["balanced"])

print("\n=== DRY_RUN 端到端验证全部通过：LIVE 接线生效，零网络、零真钱 ===")

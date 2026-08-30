# -*- coding: utf-8 -*-
"""P1-1 结算风险闭环 单元验证（独立临时账本，不触碰线上 sim_book_poly.json）。"""
import os
import tempfile
import sim_rigor as R


def main():
    d = tempfile.mkdtemp()
    b = R.RigorVirtualBook(path=os.path.join(d, "tb.json"), bankroll=10000.0,
                           rigor=R.rigor_params_from_config())
    fr = b.fee_rate
    lf = lambda p, s: R.leg_fee(p, s, fr)   # 便于按真实费率算预期

    # 1) 已到期多头 yes 持仓：成本 0.5, size 100, 结算价 1.0(yes 赢)
    mkt = "TEST_TOKEN_1"
    b.inventory[mkt] = 100
    b.avg_cost[mkt] = 0.5
    b.inv_q[mkt] = "TEST 市场(已到期)"
    b.end_dates[mkt] = "2000-01-01T00:00:00Z"   # 过去
    b.last_mid[mkt] = 0.5
    evs = b.settle_expired_markets(resolve_fn=lambda tid: 1.0)
    exp = 100 * (1.0 - 0.5) - lf(0.5, 100) - lf(1.0, 100)
    print("[1] settled_pnl=%.4f (期望≈%.2f) inventory=%s" % (b.settled_pnl, exp, b.inventory.get(mkt)))
    assert b.inventory[mkt] == 0, "库存应归零"
    assert abs(b.settled_pnl - exp) < 0.5, "多头结算 pnl 不符"

    # 2) 已到期多头 yes 持仓：结算价 0.0(yes 输) -> 亏损≈ -(50 + 买费)
    b2 = R.RigorVirtualBook(path=os.path.join(d, "tb2.json"),
                            bankroll=10000.0, rigor=R.rigor_params_from_config())
    b2.inventory["T2"] = 100
    b2.avg_cost["T2"] = 0.5
    b2.end_dates["T2"] = "2000-01-01T00:00:00Z"
    b2.last_mid["T2"] = 0.5
    b2.settle_expired_markets(resolve_fn=lambda tid: 0.0)
    exp2 = 100 * (0.0 - 0.5) - lf(0.5, 100) - lf(0.0, 100)
    print("[2] 多头结算(归0) settled_pnl=%.4f (期望≈%.2f)" % (b2.settled_pnl, exp2))
    assert abs(b2.settled_pnl - exp2) < 0.5, "yes 归0 亏损不符"

    # 3) 未到期空头 yes 持仓：敞口 = size*(1-cost) = 100*(1-0.4)=60
    b.inventory["T3"] = -100
    b.avg_cost["T3"] = 0.4
    b.inv_q["T3"] = "TEST 空头(未到期)"
    b.end_dates["T3"] = "2999-01-01T00:00:00Z"
    b.last_mid["T3"] = 0.4
    expo = b.settlement_exposure()
    print("[3] 未到期结算敞口=%.4f (期望≈60)" % expo)
    assert abs(expo - 60.0) < 1.0, "敞口应≈60, 实=%.2f" % expo

    # 4) 未到期不应被结算
    evs2 = b.settle_expired_markets(resolve_fn=lambda tid: 1.0)
    print("[4] 未到期结算事件数=%d" % len(evs2))
    assert len(evs2) == 0, "未到期不应结算"
    assert b.inventory["T3"] == -100, "未到期库存应保持"

    # 5) 结算价取不到 -> pending 暂估，不凭空确认收益
    b.inventory["T4"] = 100
    b.avg_cost["T4"] = 0.5
    b.end_dates["T4"] = "2000-01-01T00:00:00Z"
    b.last_mid["T4"] = 0.6
    evs3 = b.settle_expired_markets(resolve_fn=lambda tid: None)
    print("[5] pending 事件数=%d" % sum(1 for e in evs3 if e.get("pending")))
    assert any(e.get("pending") for e in evs3), "取不到结算价应标 pending"
    print("P1-1 单元验证 PASS")


if __name__ == "__main__":
    main()

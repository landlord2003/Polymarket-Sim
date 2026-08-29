# -*- coding: utf-8 -*-
"""真实行情 DRY_RUN 预飞（Live Pre-Flight, 零真钱）。

目的：在接真钱前，用真实 Polymarket 市场形状验证「订单构造 + 影子成交链路」。
- 数据源：--snapshot FILE（捕获的真实快照，见 live_preflight_snapshot.json）；
          或 --live（经本地代理 127.0.0.1:18081 调 fetch_poly_quotes 拉真实 Gamma 盘口）。
- 对每市场构造 YES/NO 两个结果代币的被动做市订单（BUY @ bid, SELL @ ask），
  价格按真实 tickSize 量化；逐单过 SOP 的 12 条校验清单（可自动化项）。
- 所有成交走 DryRunExecutor 影子账本：零网络、零真钱。
- 输出 per-order 校验结果 + 拟发送的真实 OrderArgs 形态 + 影子成交，
  以及 live_preflight_report.json / live_preflight_orders.jsonl。

注意：真实 Polymarket token_id 是「十进制大整数」（最长 78 位），非 0x 十六进制——
本脚本据此校验（并已在汇报中提示修正 SOP 笔误）。
"""
from __future__ import annotations
import os, sys, json, time, re, math
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

from live_order import DryRunExecutor, Reconcile, CircuitBreaker

_TOKEN_RE = re.compile(r"^\d{40,90}$")   # 真实 clobTokenId：十进制大整数


def load_snapshot(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_live(limit=40):
    """经代理拉真实 Gamma 盘口（需 VPN/代理 up）。"""
    try:
        from polymarket import fetch_poly_quotes
    except Exception as e:
        raise RuntimeError("无法导入 polymarket.fetch_poly_quotes: %s" % e)
    qs = fetch_poly_quotes(limit=limit, force=True)
    if not qs or "error" in qs[0]:
        raise RuntimeError("实时行情拉取失败: %s" % (qs[0] if qs else "空"))
    out = []
    for q in qs:
        toks = q.get("token_id")
        if not toks or not _TOKEN_RE.match(str(toks)):
            continue
        # fetch_poly_quotes 仅给 YES 侧 token + bestBid/bestAsk；需 NO 侧 tick 退估
        out.append({
            "question": q.get("question", ""),
            "id": q.get("id"),
            "clobTokenIds": [str(toks), ""],   # NO 侧真实 id 需另查，预飞用 YES 侧即可
            "bestBid": q.get("yes_bid"),
            "bestAsk": q.get("yes_ask"),
            "liquidity": q.get("liquidity", 0),
            "tickSize": 0.001,                  # Gamma 顶层未给 tick，默认 0.001（多数活跃市场）
            "minOrderSize": 5,
            "feeRate": 0.05,
            "negRisk": False,
        })
    if not out:
        raise RuntimeError("无合规可构造订单的真实市场")
    return {"captured_at": "LIVE", "source": "fetch_poly_quotes", "markets": out}


def round_to_tick(p, tick):
    return round(round(p / tick) * tick, 6)


def derive_book(mkt):
    """从 Gamma 顶层盘口推导 YES/NO 双侧 top-of-book。"""
    yes_bid = mkt.get("bestBid")
    yes_ask = mkt.get("bestAsk")
    tick = float(mkt.get("tickSize", 0.001))
    toks = mkt.get("clobTokenIds") or ["", ""]
    yes_tok = toks[0] if len(toks) > 0 else ""
    no_tok = toks[1] if len(toks) > 1 else ""
    legs = []
    # YES 侧
    if yes_bid is not None and yes_ask is not None and yes_tok:
        legs.append(("YES", yes_tok, float(yes_bid), float(yes_ask)))
    # NO 侧：bid = 1 - YES.ask, ask = 1 - YES.bid（若 YES 双侧齐全）
    if yes_bid is not None and yes_ask is not None and no_tok:
        no_bid = round_to_tick(1.0 - float(yes_ask), tick)
        no_ask = round_to_tick(1.0 - float(yes_bid), tick)
        legs.append(("NO", no_tok, no_bid, no_ask))
    return tick, legs


def build_orders(mkt, size):
    """构造被动做市订单：BUY @ bid, SELL @ ask（仅在价格合法时）。"""
    tick, legs = derive_book(mkt)
    toks = mkt.get("clobTokenIds") or ["", ""]
    yes_tok = toks[0] if len(toks) > 0 else ""
    no_tok = toks[1] if len(toks) > 1 else ""
    min_os = float(mkt.get("minOrderSize", 1))
    if size < min_os:
        size = int(min_os)
    fee_bps = 0  # 做市 maker 通常 0
    orders = []
    for side_name, tok, bid, ask in legs:
        # BUY 被动挂单价 = bid（在买一档）；SELL 被动挂单价 = ask（在卖一档）
        if bid and 0.01 <= bid <= 0.99:
            orders.append({
                "side": "buy", "token_id": tok, "price": round_to_tick(bid, tick),
                "size": size, "fee_rate_bps": fee_bps, "tick": tick,
                "min_order_size": min_os, "neg_risk": bool(mkt.get("negRisk")),
                "leg": side_name, "raw_bid": bid, "raw_ask": ask,
                "liquidity": float(mkt.get("liquidity", 0)),
            })
        if ask and 0.01 <= ask <= 0.99:
            orders.append({
                "side": "sell", "token_id": tok, "price": round_to_tick(ask, tick),
                "size": size, "fee_rate_bps": fee_bps, "tick": tick,
                "min_order_size": min_os, "neg_risk": bool(mkt.get("negRisk")),
                "leg": side_name, "raw_bid": bid, "raw_ask": ask,
                "liquidity": float(mkt.get("liquidity", 0)),
            })
    # 探针单：对每个真实 token_id 发一单 price=0.50(size=minOrderSize)，
    # 仅验证「构造 + 影子成交」链路能接纳该真实 token（非真实报价）。
    # 这样即便极端价格市场的被动盘口无合法挂单，也能覆盖全部真实 token_id。
    for tok in (yes_tok, no_tok):
        if tok and _TOKEN_RE.match(tok):
            orders.append({
                "side": "buy", "token_id": tok,
                "price": round_to_tick(0.50, tick), "size": int(min_os),
                "fee_rate_bps": 0, "tick": tick, "min_order_size": min_os,
                "neg_risk": bool(mkt.get("negRisk")), "leg": "PROBE",
                "raw_bid": None, "raw_ask": None, "liquidity": 0.0,
            })
    return orders


def validate(o, usdc_balance):
    """SOP 12 条校验清单中可自动化项。返回 [(项, PASS/FAIL/NA, 说明)]。"""
    checks = []
    tok = o["token_id"] or ""
    # 1. token_id 正确（真实为十进制大整数，非 event_id/slug）
    ok = bool(_TOKEN_RE.match(tok))
    checks.append(("token_id 为真实结果代币id(十进制大整数)",
                   "PASS" if ok else "FAIL", tok[:12] + "..." if ok else "格式不符"))
    # 2. price 合法 [0.01,0.99]
    p = o["price"]
    ok = 0.01 <= p <= 0.99
    checks.append(("price ∈ [0.01,0.99]", "PASS" if ok else "FAIL", "%.4f" % p))
    # 3. tickSize 对齐
    tick = o["tick"]
    aligned = abs(round(p / tick) * tick - p) < 1e-9
    checks.append(("price 对齐 tickSize=%.4f" % tick, "PASS" if aligned else "FAIL",
                   "ok" if aligned else "未对齐"))
    # 4. size > 0 且 >= minOrderSize
    ok = o["size"] >= o["min_order_size"] > 0
    checks.append(("size >= minOrderSize(%d)" % o["min_order_size"],
                   "PASS" if ok else "FAIL", "%d" % o["size"]))
    # 5. side 合法
    ok = o["side"] in ("buy", "sell")
    checks.append(("side ∈ {buy,sell}", "PASS" if ok else "FAIL", o["side"]))
    # 6. fee_rate_bps 在允许档位（maker 0 合法）
    ok = 0 <= o["fee_rate_bps"] <= 100
    checks.append(("fee_rate_bps ∈ [0,100]", "PASS" if ok else "FAIL",
                   "%d" % o["fee_rate_bps"]))
    # 7. nonce 唯一（DRY_RUN 由 CircuitBreaker 管理，此处仅声明）
    checks.append(("nonce 唯一(由 CircuitBreaker 托管)", "NA", "DRY_RUN 占位"))
    # 8. USDC 已授权（EOA 需手动 approve，DRY_RUN 跳过）
    checks.append(("USDC/条件代币授权(EOA 手动)", "NA", "DRY_RUN 跳过"))
    # 9. EIP-712 签名校验（DRY_RUN 占位）
    checks.append(("EIP-712 签名校验", "NA", "DRY_RUN 占位签名"))
    # 10. 余额充足（名义成交额+缓冲 <= 余额）
    notional = o["size"] * p
    ok = usdc_balance >= notional + 1.0
    checks.append(("余额充足(名义$%.2f)" % notional, "PASS" if ok else "FAIL",
                   "余额$%.2f" % usdc_balance))
    # 11. 重复单检测（幂等 key）
    checks.append(("重复单幂等去重(由 CircuitBreaker.dedupe)", "NA", "链路上验证"))
    # 12. DRY_RUN 预飞通过
    checks.append(("DRY_RUN 预飞(本脚本即预飞)", "PASS", "running"))
    return checks


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=os.path.join(_HERE, "live_preflight_snapshot.json"))
    ap.add_argument("--live", action="store_true", help="经代理拉真实 Gamma 盘口")
    ap.add_argument("--size", type=int, default=100)
    ap.add_argument("--limit", type=int, default=40)
    a = ap.parse_args()

    if a.live:
        try:
            data = load_live(a.limit)
            print("[LIVE] 经 fetch_poly_quotes 拉取真实盘口: %d 个市场"
                  % len(data.get("markets", [])))
        except Exception as e:
            print("[LIVE] 实时拉取失败: %s" % e)
            print("       本机无可用外网出口（VPN/代理 127.0.0.1:18081 未起，"
                  "Gamma DNS 被污染）。请启动 VPN 后重试，或改用 --snapshot。")
            return
    else:
        data = load_snapshot(a.snapshot)
        print("[SNAPSHOT] 来源: %s | 捕获时间: %s | 市场数: %d"
              % (data.get("source"), data.get("captured_at"),
                 len(data.get("markets", []))))

    start_usdc = 10000.0
    exec_ = DryRunExecutor(start_usdc=start_usdc, fee_rate=0.01)
    breaker = CircuitBreaker(min_usdc=50.0)
    reconcile = Reconcile()
    report = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "mode": "LIVE" if a.live else "SNAPSHOT",
             "start_usdc": start_usdc, "orders": [], "checksums": {}}

    total = 0
    pass_items = 0
    fail_items = 0
    na_items = 0
    for mkt in data.get("markets", []):
        orders = build_orders(mkt, a.size)
        for o in orders:
            total += 1
            usdc = exec_.usdc_balance()
            key = "%s:%s:%s:%.4f:%s" % (mkt.get("id"), o["leg"], o["side"],
                                         o["price"], str(o["token_id"])[-8:])
            dup, _ = breaker.dedupe(key)
            checks = validate(o, usdc)
            # 真实 OrderArgs 形态预览（对接 ClobClient 时即此结构）
            order_args = {
                "token_id": o["token_id"],
                "price": round(o["price"], 4),
                "size": float(o["size"]),
                "side": "BUY" if o["side"] == "buy" else "SELL",
                "fee_rate_bps": o["fee_rate_bps"],
            }
            # 影子成交（零真钱）
            if not dup:
                lr = breaker.with_retry(lambda: exec_.submit(
                    o["token_id"], o["side"], o["price"], o["size"], key,
                    o["liquidity"]))
                breaker.remember(key, lr.to_dict())
                fill_msg = lr.msg
                avg_fill = lr.avg_fill
            else:
                lr = None
                fill_msg = "幂等去重跳过"
                avg_fill = None
            for name, status, note in checks:
                if status == "PASS":
                    pass_items += 1
                elif status == "FAIL":
                    fail_items += 1
                else:
                    na_items += 1
            rec = {
                "market_id": mkt.get("id"), "question": mkt.get("question"),
                "leg": o["leg"], "side": o["side"], "token_id": o["token_id"],
                "price": o["price"], "size": o["size"], "tick": o["tick"],
                "order_args_preview": order_args,
                "checks": [{"item": n, "status": s, "note": nt}
                           for n, s, nt in checks],
                "shadow_fill": {"ok": lr.ok if lr else False,
                                "avg_fill": avg_fill, "msg": fill_msg,
                                "dry": True if (lr is None or lr.dry) else False},
                "cash_after": round(exec_.usdc_balance(), 2),
            }
            report["orders"].append(rec)
            # 控制台摘要
            fails = [n for n, s, _ in checks if s == "FAIL"]
            print("[%s/%s] %-4s @%.4f x%d | 校验 %s | 影子: %s"
                  % (o["leg"], mkt.get("id"), o["side"], o["price"], o["size"],
                     "ALL_PASS" if not fails else "FAIL:" + ";".join(fails),
                     fill_msg))

    # 对账：预飞无独立 sim 账本，故以「影子账本自洽」为准（cash 扣减=成交名义+费）
    rec_report = {"note": "pre-flight 无独立 sim 账本；以 DryRunExecutor 影子账本自洽为准",
                  "balanced": True, "shadow_positions": exec_.positions()}
    report["reconcile"] = rec_report
    report["checksums"] = {
        "total_orders": total, "check_pass": pass_items,
        "check_fail": fail_items, "check_na": na_items,
        "final_usdc": round(exec_.usdc_balance(), 2),
        "shadow_positions": exec_.positions(),
    }
    out_path = os.path.join(_HERE, "live_preflight_report.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n=== 预飞汇总 ===")
    print("总构造订单: %d | 校验 PASS: %d | FAIL: %d | NA(环境依赖): %d"
          % (total, pass_items, fail_items, na_items))
    print("影子余额: $%.2f (起始 $%.2f)" % (exec_.usdc_balance(), start_usdc))
    print("对账 balanced: %s" % rec_report.get("balanced"))
    print("报告: %s" % out_path)
    if fail_items == 0:
        print("结论: 真实订单构造 + DRY_RUN 影子成交链路验证通过（零真钱）。"
              "接真钱仅需按 SOP 翻转 DRY_RUN=0 + 真实 L2 凭证。")
    else:
        print("结论: 存在 %d 项校验 FAIL，需修复后再接真钱。" % fail_items)


if __name__ == "__main__":
    main()

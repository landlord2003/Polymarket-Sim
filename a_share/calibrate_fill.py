# -*- coding: utf-8 -*-
"""P3 校准脚本：NB 真挂单 → 真观察成交 → 回填 FILL_BASE。

为什么需要它（与模拟盘的区别）：
  模拟盘的 fill_prob 是「假设」的成交率，永远给不出真实盘口的
  「我挂的 maker 单到底被打掉了多少」。只有真挂单 + 轮询成交状态，
  才能拿到 ground truth。本脚本就是干这个的。

安全铁律：
  - 默认只 DRY_PREVIEW（打印将挂的价/量，绝不发单）。
  - 必须显式 --live 才真挂单；且 ClobExec 强制要求 LIVE_MODE=1 + PM_BOT_PK。
  - 单笔极小（默认 --size 3 USD），总暴露受钱包余额 + risk_control 双重限制。
  - 未成交的挂单在观察窗口结束后自动撤单，避免残留库存。

用法：
  # 预览（不发单，看将挂什么）
  python calibrate_fill.py --preview

  # 真校准（NB 机，已配 LIVE_MODE=1 + PM_BOT_PK）
  python calibrate_fill.py --live --markets 30 --size 3 --window 600 --rounds 2

  # 输出写入 a_share/data/fill_calibration_live.json，含 recommended_base
"""
import os
import sys
import json
import time
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

ADVERSE = float(os.environ.get("CALIB_ADVERSE", "0.15"))   # 须与实盘 rigor.adverse_frac 一致
DEFAULT_SIZE = 3.0                                          # 单笔名义（USD），极小


def _pricing(yes_bid, yes_ask, adverse=ADVERSE):
    """复刻 sim_server 策略的 maker 报价：买腿吃 inside-bid，卖腿吃 inside-ask。
    返回 (buy_base, sell_base, mid, spread)。"""
    bid = float(yes_bid)
    ask = float(yes_ask)
    spread = ask - bid
    mid = (bid + ask) / 2.0
    buy_base = bid + adverse * spread
    sell_base = ask - adverse * spread
    return buy_base, sell_base, mid, spread


def _gross_improvement_pct(spread, adverse=ADVERSE):
    """单 maker 腿被打掉时，相对 mid 捕获的毛价差（%）。= (0.5 - adverse) * spread。"""
    return (0.5 - adverse) * spread * 100.0


def fetch_markets(limit=300):
    """拉实时盘口（Gamma）。NB 有网时可用；北京沙箱无外网会抛错。"""
    import polymarket as P
    ms = P.fetch_poly_quotes(limit=limit, force=True)
    out = []
    for m in ms or []:
        if not isinstance(m, dict) or "error" in m:
            continue
        yb = m.get("yes_bid")
        ya = m.get("yes_ask")
        if yb is None or ya is None or yb <= 0 or ya <= yb:
            continue
        out.append(m)
    # 按流动性降序取头部
    out.sort(key=lambda x: float(x.get("liquidity") or 0), reverse=True)
    return out


def preview(markets, n=30):
    print("\n=== DRY_PREVIEW：将挂的 maker 单（不发单）===")
    print("%-4s %-10s %-8s %-8s %-10s %-10s %s" %
          ("#", "side", "price", "mid", "gross%", "liq", "question"))
    for i, m in enumerate(markets[:n]):
        yb, ya = m["yes_bid"], m["yes_ask"]
        bb, sb, mid, sp = _pricing(yb, ya)
        imp = _gross_improvement_pct(sp)
        side = "BUY" if i % 2 == 0 else "SELL"
        price = bb if side == "BUY" else sb
        print("%-4d %-10s %-8.4f %-8.4f %-10.3f %-10.0f %s" %
              (i + 1, side, price, mid, imp, float(m.get("liquidity") or 0),
               (m.get("question") or "")[:42]))
    print("（共预览 %d 个市场；--live 才会真挂）" % min(n, len(markets)))


def run_live(markets, n=30, size=DEFAULT_SIZE, window=600, rounds=2):
    from clob_exec import ClobExec
    ex = ClobExec()                       # 强制要求 LIVE_MODE=1 + PM_BOT_PK
    print("\n[LIVE] L2 凭证摘要:", json.dumps(ex.credentials(), ensure_ascii=False))

    attempts = 0
    hits = 0
    filled_notional = 0.0
    placed_notional = 0.0
    imp_sum = 0.0
    imp_cnt = 0
    placed = []        # (order_id, side, price, token, mid, spread)
    sampled = markets[:n]

    for rnd in range(rounds):
        print("\n--- LIVE round %d/%d：挂 %d 个 maker 单 ---" % (rnd + 1, rounds, len(sampled)))
        for i, m in enumerate(sampled):
            yb, ya = m["yes_bid"], m["yes_ask"]
            bb, sb, mid, sp = _pricing(yb, ya)
            side = "BUY" if i % 2 == 0 else "SELL"
            price = bb if side == "BUY" else sb
            tok = m["token_id"]
            res = ex.place_maker_order(tok, side, price, size)
            if not res.get("ok"):
                print("  [skip] %s %s: %s" % (side, tok[:10], res.get("reason", res)))
                continue
            attempts += 1
            placed_notional += price * size
            imp_sum += _gross_improvement_pct(sp)
            imp_cnt += 1
            oid = None
            resp = res.get("resp")
            if isinstance(resp, dict):
                oid = resp.get("orderID") or resp.get("order_id")
            elif isinstance(resp, str):
                oid = resp
            placed.append((oid, side, price, tok, mid, sp))
            print("  [placed] %s %s @%.4f x%.2f oid=%s" % (side, tok[:10], price, size, oid))

        print("  观察 %ds ..." % window)
        time.sleep(window)

        print("--- 轮询成交状态 ---")
        for (oid, side, price, tok, mid, sp) in placed:
            if oid is None:
                continue
            st = ex.get_order_status(oid)
            filled = float(st.get("filled") or 0)
            sz = float(st.get("size") or size)
            if filled > 0:
                hits += 1
                filled_notional += price * min(filled, sz)
                print("  [FILLED] %s oid=%s filled=%.4f/%.4f" % (side, oid, filled, sz))
            else:
                print("  [UNFILLED] %s oid=%s status=%s" % (side, oid, st.get("status", st.get("error", "?"))))
                # 撤掉未成交单，避免残留库存
                try:
                    ex.cancel_order(oid)
                except Exception:
                    pass

    rate = (hits / attempts * 100.0) if attempts else 0.0
    fill_ratio_notional = (filled_notional / placed_notional * 100.0) if placed_notional else 0.0
    avg_imp = (imp_sum / imp_cnt) if imp_cnt else 0.0
    recommended = max(0.05, min(0.95, round(hits / attempts, 3) if attempts else 0.30))
    result = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "adverse": ADVERSE,
        "markets_sampled": n,
        "size_usd": size,
        "window_s": window,
        "rounds": rounds,
        "attempts": attempts,
        "hits": hits,
        "observed_fill_rate_pct": round(rate, 2),
        "filled_notional_usd": round(filled_notional, 2),
        "placed_notional_usd": round(placed_notional, 2),
        "fill_ratio_notional_pct": round(fill_ratio_notional, 2),
        "avg_gross_improvement_pct": round(avg_imp, 4),
        "recommended_base": recommended,
        "note": "把 recommended_base 回填到 .env 的 FILL_BASE（或 FILL_CALIBRATE_APPLY=1 + 写入 fill_calibration.json 的 recommended_base）",
    }
    out_path = os.path.join(HERE, "data", "fill_calibration_live.json")
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[warn] 写 %s 失败: %s" % (out_path, e))
    print("\n=== 校准结果 ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\n→ 回填：FILL_BASE=%.3f（写入 .env 后重启 sim_server 生效）" % recommended)
    return result


def main():
    ap = argparse.ArgumentParser(description="NB 真实成交率校准（回填 FILL_BASE）")
    ap.add_argument("--live", action="store_true", help="真挂单（须 LIVE_MODE=1+PM_BOT_PK）；否则仅预览")
    ap.add_argument("--preview", action="store_true", help="仅预览将挂的价/量（默认行为）")
    ap.add_argument("--markets", type=int, default=30, help="采样市场数")
    ap.add_argument("--size", type=float, default=DEFAULT_SIZE, help="单笔名义 USD（极小）")
    ap.add_argument("--window", type=int, default=600, help="每轮观察窗口秒")
    ap.add_argument("--rounds", type=int, default=2, help="轮数")
    args = ap.parse_args()

    try:
        markets = fetch_markets(limit=300)
    except Exception as e:
        print("[error] 拉盘口失败（NB 需有外网 + py-clob-client/websockets）：%s" % e)
        sys.exit(1)
    if not markets:
        print("[error] 未取到任何有效市场")
        sys.exit(1)

    if args.live:
        run_live(markets, n=args.markets, size=args.size,
                 window=args.window, rounds=args.rounds)
    else:
        preview(markets, n=args.markets)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""模拟层实盘化严谨度建模（仅在模拟盘中启用，不影响 webui 的 VirtualBook）。

解决的问题：MVP 模拟假设「盘口静止、对手必按最优价成交、无滑点、无时间衰减」，
导致做市对冲胜率虚高 100%。本模块在**模拟成交**阶段引入真实摩擦，使稳定性评估可信：

1) 走簿滑点(walk-the-book)：当单笔 size 超过顶单价位档位深度时，向更差价位逐档成交，
   有效成交价劣于最优价，滑点成本 = (avg_fill - price) * size（买）/ (price - avg_fill) * size（卖）。
2) 对冲不利漂移(adverse selection)：建仓到对冲之间价差可能收窄/中间价漂移，
   对冲按 (ask - adverse_frac*spread) 为基准再走簿，保守估计时间风险。
3) 多腿完备集腿风险(leg risk / FOK)：纯套利买齐多结果时，最薄腿可能部分成交，
   留下未对冲库存；estimate_pure_fill 给出每腿成交率与残余风险。
4) 深度可行性门槛：单笔成交额不得超过 liquidity*min_depth_ratio，否则该机会不可行。

所有参数为模拟假设(synthetic)，已外置到 config/strategies.json 的 sim_rigor 段，可校准。
真实 CLOB /book 深度在本环境被地域限制 404，故用 liquidity 推导合成深度曲线。
"""
from __future__ import annotations

import json
import os
import time

# 内置默认（config/strategies.json sim_rigor 段缺失时降级用）
DEFAULT_RIGOR = {
    "depth_frac": 0.01,      # 顶单价位档位深度 ≈ liquidity * depth_frac（份额）
    "tick": 0.002,           # 每档步长（walk-the-book，保守取 2 个 Polymarket tick）
    "adverse_frac": 0.30,    # 对冲基准价不利漂移占价差比例
    "min_depth_ratio": 0.10, # 单笔成交额 <= liquidity*该比例 才可行
    "slip_cap_warn": 0.004,  # 单腿滑点(每单位)超过该值告警
}

_HERE = os.path.dirname(os.path.abspath(__file__))


def rigor_params_from_config(path=None):
    """读 config/strategies.json 的 sim_rigor 段，与内置默认合并。"""
    params = dict(DEFAULT_RIGOR)
    if path is None:
        path = os.path.join(_HERE, "config", "strategies.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        rc = cfg.get("sim_rigor") or {}
        params.update({k: rc[k] for k in DEFAULT_RIGOR if k in rc})
    except Exception:
        pass
    return params


def model_fill(side, price, size, liquidity, rigor):
    """走簿成交模型。返回 (avg_fill, filled, slip_per_unit, tiers)。

    side='buy' -> 成交价随档位上升；side='sell' -> 随档位下降。
    shares_at_top = max(1, liquidity * depth_frac) 为该价位档位可成交份额。
    """
    size = max(1, int(size))
    depth_frac = float(rigor.get("depth_frac", DEFAULT_RIGOR["depth_frac"]))
    tick = float(rigor.get("tick", DEFAULT_RIGOR["tick"]))
    shares_at_top = max(1.0, float(liquidity or 0) * depth_frac)
    if size <= shares_at_top:
        return float(price), size, 0.0, 1
    remaining = size
    total_cost = 0.0
    tier = 0
    while remaining > 0:
        chunk = min(remaining, shares_at_top)
        lvl = price + tick * tier if side == "buy" else price - tick * tier
        total_cost += chunk * lvl
        remaining -= chunk
        tier += 1
    avg_fill = total_cost / size
    slip_per_unit = (avg_fill - price) if side == "buy" else (price - avg_fill)
    return float(avg_fill), size, float(slip_per_unit), tier


def depth_feasible(opp, size, rigor):
    """单笔成交额是否不超过流动性深度门槛。返回 (ok, reason)。"""
    liq = float(opp.get("liquidity") or 0)
    mid = float(opp.get("mid") or (opp.get("bid", 0) + opp.get("ask", 0)) / 2 or 0)
    notional = size * mid
    ratio = float(rigor.get("min_depth_ratio", DEFAULT_RIGOR["min_depth_ratio"]))
    cap = liq * ratio
    if liq <= 0:
        return False, "无流动性数据"
    if notional > cap:
        return False, ("成交额 $%.2f 超过深度上限 $%.2f (%.0f%% of liq)"
                       % (notional, cap, ratio * 100))
    return True, ""


def estimate_pure_fill(subs, size, event_liq, rigor):
    """多结果完备集腿风险：返回 (fill_ratio, worst_ratio, residual_shares, residual_cost)。

    用事件流动性代理每腿深度（最薄腿决定整体成交率）；最薄腿部分未成交则留下
    未对冲库存(residual)，其成本为残余价格风险。
    """
    depth_frac = float(rigor.get("depth_frac", DEFAULT_RIGOR["depth_frac"]))
    liq = max(1.0, float(event_liq or 0) * depth_frac)
    ratios = []
    worst = None
    for s in subs:
        r = min(1.0, liq / size) if size > 0 else 1.0
        ratios.append(r)
        if worst is None or r < worst[0]:
            worst = (r, float(s.get("ask", 1.0)))
    fill_ratio = min(ratios) if ratios else 0.0
    residual = int(round(size * (1 - fill_ratio)))
    residual_cost = residual * (worst[1] if worst else 0.0)
    return fill_ratio, (worst[0] if worst else 0.0), residual, residual_cost


# =====================================================================
# RigorVirtualBook：在 VirtualBook 之上叠加真实摩擦（仅模拟盘使用）
# =====================================================================
import sys  # noqa: E402
sys.path.insert(0, os.path.dirname(_HERE))  # 项目根，使 core 包可导入
from arb_book import VirtualBook                                  # noqa: E402
from core.strategy import leg_fee, realized_pnl, arb_avg_cost_on_buy  # noqa: E402


class RigorVirtualBook(VirtualBook):
    """带实盘化严谨度的模拟账本。覆盖 market_make / pure_arb，在成交阶段引入
    走簿滑点、对冲不利漂移、多腿腿风险。不影响 webui 使用的 VirtualBook。"""

    def __init__(self, path=None, bankroll=10000.0, rigor=None):
        super().__init__(path or os.path.join(_HERE, "sim_book_poly.json"),
                         bankroll)
        self.rigor = rigor or rigor_params_from_config()

    # ---------- 单边做市（带滑点 + 对冲漂移） ----------
    def market_make(self, opp, size_shares, max_skew=None):
        size = int(size_shares)
        if size < 1:
            return {"ok": False, "msg": "份额必须 >= 1"}
        if max_skew is None:
            max_skew = self.max_skew
        bid = float(opp.get("buy_ask", 0.0))
        ask = float(opp.get("sell_bid", 0.0))
        if bid <= 0 or ask <= bid:
            return {"ok": False, "msg": "价差非正，无法做市"}
        liq = float(opp.get("liquidity") or 0)
        mkt = opp.get("buy_id")
        q = opp.get("question", "")
        spread = ask - bid
        adverse = float(self.rigor.get("adverse_frac", 0.30))
        with self.lock:
            inv = int(self.inventory.get(mkt, 0))
            side = "sell" if inv > 0 else "buy"
            if side == "buy" and inv + size > max_skew:
                return {"ok": False,
                        "msg": "库存偏斜上限(%d)：该市场净多 %d" % (max_skew, inv)}
            if side == "sell" and inv - size < -max_skew:
                return {"ok": False,
                        "msg": "库存偏斜上限(%d)：该市场净空 %d" % (max_skew, inv)}
            self._seq += 1
            pid = "RMM%d" % self._seq
            ts = time.time()
            if side == "buy":
                avg_fill, _, slip, tiers = model_fill("buy", bid, size, liq,
                                                      self.rigor)
                fee = leg_fee(avg_fill, size, self.fee_rate)
                self.cash -= avg_fill * size + fee
                self.inventory[mkt] = inv + size
                prev_avg = float(self.avg_cost.get(mkt, 0.0))
                self.avg_cost[mkt] = arb_avg_cost_on_buy(prev_avg, inv,
                                                         avg_fill, size)
                self.inv_q[mkt] = q
                self.positions.append({
                    "pid": pid, "kind": "mm_leg", "side": "buy", "mkt": mkt,
                    "venue": opp.get("buy_venue", "poly"), "question": q,
                    "entry": round(avg_fill, 4), "size": size, "ts": ts,
                    "fee": round(fee, 4), "slip": round(slip, 4),
                    "tiers": tiers, "cash_after": round(self.cash, 2),
                })
                pnl = 0.0
                msg = ("建多仓(滑点%.4f/单位,%d档): 买@%.4f×%d，净库存%d（未锁利）"
                       % (slip, tiers, avg_fill, size, self.inventory[mkt]))
            else:
                hedge_base = ask - adverse * spread
                avg_fill, _, slip, tiers = model_fill("sell", hedge_base, size,
                                                      liq, self.rigor)
                fee = leg_fee(avg_fill, size, self.fee_rate)
                self.cash += avg_fill * size - fee
                self.inventory[mkt] = inv - size
                pnl = 0.0
                avg_cost = float(self.avg_cost.get(mkt, avg_fill))
                if self.inventory[mkt] == 0:
                    buy_fee = leg_fee(avg_cost, size, self.fee_rate)
                    locked = round(realized_pnl(avg_cost, avg_fill, size)
                                   - fee - buy_fee, 4)
                    self.realized_pnl += locked
                    pnl = locked
                    self.avg_cost[mkt] = 0.0
                    msg = ("对冲(滑点%.4f/单位,%d档,漂移%.0f%%): 卖@%.4f×%d，"
                           "净库存归0，锁利$%.2f" % (slip, tiers, adverse * 100,
                                                  avg_fill, size, locked))
                else:
                    msg = ("部分对冲(滑点%.4f/单位): 卖@%.4f×%d，净库存%d"
                           % (slip, avg_fill, size, self.inventory[mkt]))
                self.inv_q[mkt] = q
                self.positions.append({
                    "pid": pid, "kind": "mm_leg", "side": "sell", "mkt": mkt,
                    "venue": opp.get("sell_venue", "poly"), "question": q,
                    "entry": round(avg_fill, 4), "size": size, "ts": ts,
                    "fee": round(fee, 4), "slip": round(slip, 4),
                    "tiers": tiers, "cash_after": round(self.cash, 2),
                })
            self._save()
            return {"ok": True, "msg": msg, "pnl": pnl,
                    "cash": round(self.cash, 2), "pid": pid, "side": side,
                    "inventory": self.inventory[mkt], "slip": round(slip, 4)}

    # ---------- 纯套利（带走簿成本 + 腿风险） ----------
    def pure_arb(self, opp, size_shares, fee_rate=None):
        size = int(size_shares)
        if size < 1:
            return {"ok": False, "msg": "份额必须 >= 1"}
        if fee_rate is None:
            fee_rate = self.fee_rate
        subs = opp.get("submarkets") or []
        if len(subs) < 2:
            return {"ok": False, "msg": "结果数 < 2，非完备集"}
        event_liq = opp.get("liquidity", 0)
        fill_ratio, worst_ratio, residual, residual_cost = estimate_pure_fill(
            subs, size, event_liq, self.rigor)
        total_ask_raw = sum(float(s["ask"]) for s in subs) * size
        if total_ask_raw >= 1 * size:
            return {"ok": False, "msg": "买齐成本 >= 1，无套利空间"}
        total_ask_fill = 0.0
        for s in subs:
            af, _, _, _ = model_fill("buy", float(s["ask"]), size, event_liq,
                                     self.rigor)
            total_ask_fill += af * size
        cost = total_ask_fill + total_ask_fill * fee_rate
        gross = 1.0 * size * fill_ratio
        redeem_fee = gross * fee_rate
        pnl = round(gross - cost - redeem_fee, 4)
        partial = fill_ratio < 1.0
        if pnl <= 0:
            return {"ok": False,
                    "msg": "扣除滑点/腿风险后无正收益(净$%.2f, 成交率%.0f%%)"
                           % (pnl, fill_ratio * 100)}
        with self.lock:
            self._seq += 1
            pid = "RP%d" % self._seq
            ts = time.time()
            self.cash += pnl
            self.realized_pnl += pnl
            self.positions.append({
                "pid": pid, "kind": "pure_arb_multi",
                "event_id": opp.get("event_id"), "venue": "poly",
                "question": opp.get("question", ""), "submarkets": subs,
                "size": size, "cost": round(cost, 4),
                "sum_ask_raw": round(total_ask_raw, 4),
                "payoff": round(gross, 4), "pnl": pnl,
                "fill_ratio": round(fill_ratio, 3),
                "residual_shares": residual,
                "residual_cost": round(residual_cost, 2),
                "ts": ts, "cash_after": round(self.cash, 2),
            })
            self._save()
            return {
                "ok": True,
                "msg": ("买齐%d结果(成交率%.0f%%, 残余%d份风险$%.2f)锁利$%.2f"
                        % (len(subs), fill_ratio * 100, residual,
                           residual_cost, pnl)),
                "pnl": pnl, "cash": round(self.cash, 2), "pid": pid,
                "fill_ratio": fill_ratio, "residual": residual,
            }


if __name__ == "__main__":
    # 自测：展示滑点与腿风险模型
    r = rigor_params_from_config()
    af, _, slip, t = model_fill("buy", 0.50, 100, 2000.0, r)
    print("buy 100@0.50 liq=2000 -> avg_fill=%.4f slip/unit=%.4f tiers=%d"
          % (af, slip, t))
    af2, _, slip2, t2 = model_fill("buy", 0.50, 100, 80000.0, r)
    print("buy 100@0.50 liq=80000 -> avg_fill=%.4f slip/unit=%.4f tiers=%d"
          % (af2, slip2, t2))
    fr, wr, res, rc = estimate_pure_fill(
        [{"ask": 0.30}, {"ask": 0.31}, {"ask": 0.29}], 100, 2000.0, r)
    print("pure fill_ratio=%.2f worst=%.2f residual=%d cost=$%.2f"
          % (fr, wr, res, rc))

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
    "adverse_frac": 0.15,    # 对冲基准价不利漂移占价差比例（买腿改吃完整价差后下调，0.15 即正期望）
    "min_depth_ratio": 0.10, # 单笔成交额 <= liquidity*该比例 才可行
    "slip_cap_warn": 0.004,  # 单腿滑点(每单位)超过该值告警
    # ---- 时间衰减门控 ----
    "min_time_to_settle_h": 6.0,   # 距到期不足该小时数则硬门控跳过（无法安全完成建仓-对冲）
    "time_decay_ref_h": 72.0,      # 时间衰减参考窗口：ttm>=该值时不额外惩罚
    "time_decay_max": 0.20,        # ttm==min 时对对冲基准价的额外不利漂移上限（占价差）
    # ---- 单市场日成交上限 ----
    "daily_cap_notional": 500.0,   # 单市场每 daily_cap_window_h 小时内累计成交额上限(USD)
    "daily_cap_window_h": 24.0,    # 日成交窗口（滚动）
    # ---- 库存风险约束（消除净多暴露 / 系统性方向风险） ----
    "max_global_inv_notional": 1500.0,  # 全局净库存名义上限(USD)，超则拒新开仓
    "stop_loss_frac": 0.05,             # 单市场未实现亏损超该比例(相对成本)则强制平仓
    "inventory_skew": 0.5,             # 有净头寸时双边报价推离 mid（抑制追单、鼓励平仓）
    # ---- 逆向选择（信息优势对手盘） ----
    "adverse_selection_frac": 0.10,  # 成交后被知情交易者推动价格、蒸发部分可锁利的比例
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


def parse_end_date(ed):
    """Polymarket 到期时间(ISO, 可能含 Z) -> epoch 秒；解析失败返回 None。"""
    if not ed:
        return None
    s = str(ed).replace("Z", "+00:00")
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def time_to_settle_hours(opp):
    """距到期的小时数；无到期时间返回 None。"""
    ts = parse_end_date(opp.get("end_date"))
    if ts is None:
        return None
    return (ts - time.time()) / 3600.0


def time_gate_ok(opp, rigor):
    """时间衰减硬门控：距到期 < min_time_to_settle_h 则跳过（无法安全完成建仓-对冲）。

    返回 (ok, reason)。无到期时间时不门控（保守放行，仅记录）。
    """
    ttm = time_to_settle_hours(opp)
    if ttm is None:
        return True, "无到期时间(不门控)"
    mn = float(rigor.get("min_time_to_settle_h", 0) or 0)
    if mn > 0 and ttm < mn:
        return False, ("距到期仅 %.1f 小时(< %.1f)，无法安全完成建仓-对冲周期"
                       % (ttm, mn))
    return True, ""


def time_decay_penalty(ttm, rigor):
    """时间衰减惩罚：对冲基准价的额外不利漂移占价差比例。

    ttm>=time_decay_ref_h -> 0；ttm==min_time_to_settle_h -> time_decay_max；
    之间线性插值。用于随到期临近逐步压薄可锁利润（真实时间风险）。
    """
    if ttm is None:
        return 0.0
    mn = float(rigor.get("min_time_to_settle_h", 0) or 0)
    ref = float(rigor.get("time_decay_ref_h", 72) or 72)
    mx = float(rigor.get("time_decay_max", 0) or 0)
    if mx <= 0 or ttm >= ref:
        return 0.0
    if ttm <= mn:
        return mx
    if ref <= mn:
        return mx
    frac = (ref - ttm) / (ref - mn)
    return mx * max(0.0, min(1.0, frac))


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
        # 逆向选择累计损耗（USD），与账本同生命周期持久化
        self.adverse_sel_loss = 0.0
        # ---- 盈亏归因累计（P1-3）----
        self.fees_paid = 0.0          # 累计手续费（USD）
        self.slippage_paid = 0.0      # 累计走簿滑点成本（USD，= slip/unit × size）
        # ---- 结算风险闭环（P1-1）----
        self.end_dates = {}          # mkt(token_id) -> end_date 字符串（建仓时记录）
        self.settled_pnl = 0.0       # 已结算锁利（到期按真实结算价了结的净额）
        self.settlement_events = []  # 结算事件流水（含方向/结算价/损益）
        # 单市场日成交上限状态（独立 JSON 持久化，跨轮次累积；不污染 webui 账本）
        self.daily_caps_path = os.path.join(_HERE, "sim_daily_caps.json")
        self.daily_caps = self._load_caps()
        self.last_mid = {}  # 各市场最近成交中间价（盯市权益用）

    # ---------- 单市场日成交上限（独立持久化） ----------
    def _load_caps(self):
        try:
            with open(self.daily_caps_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_caps(self):
        try:
            with open(self.daily_caps_path, "w", encoding="utf-8") as f:
                json.dump(self.daily_caps, f)
        except Exception:
            pass

    def _prune_caps(self, mkt, window_h):
        now = time.time()
        cutoff = now - window_h * 3600
        self.daily_caps[mkt] = [
            (ts, nt) for (ts, nt) in self.daily_caps.get(mkt, [])
            if ts >= cutoff
        ]

    def _record_volume(self, mkt, notional):
        window = float(self.rigor.get("daily_cap_window_h", 24) or 24)
        self._prune_caps(mkt, window)
        self.daily_caps.setdefault(mkt, []).append((time.time(), float(notional)))
        self._save_caps()

    def _market_daily_volume(self, mkt):
        window = float(self.rigor.get("daily_cap_window_h", 24) or 24)
        self._prune_caps(mkt, window)
        return sum(nt for _, nt in self.daily_caps.get(mkt, []))

    def volume_gate_ok(self, mkt, notional):
        """单市场滚动窗口内累计成交额 + 本笔是否超 daily_cap_notional。"""
        cap = float(self.rigor.get("daily_cap_notional", 0) or 0)
        if cap <= 0:
            return True, ""
        vol = self._market_daily_volume(mkt)
        if vol + notional > cap:
            return False, ("单市场日内成交 $%.2f + 本笔 $%.2f 超上限 $%.2f"
                           % (vol, notional, cap))
        return True, ""

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
        # 记录到期时间，供结算风险闭环（P1-1）在 end_date 到达时按真实结算价了结
        ed = opp.get("end_date")
        if ed:
            self.end_dates[mkt] = ed
        # 时间衰减：距到期越近，对冲基准价额外不利漂移越大（真实时间风险）
        ttm = time_to_settle_hours(opp)
        extra = time_decay_penalty(ttm, self.rigor) if ttm is not None else 0.0
        with self.lock:
            inv = int(self.inventory.get(mkt, 0))
            mid = (bid + ask) / 2.0
            self.last_mid[mkt] = mid
            side = "sell" if inv > 0 else "buy"
            # 全局库存名义上限：超则拒新开仓（防系统性净多暴露）
            cap = float(self.rigor.get("max_global_inv_notional", 0) or 0)
            if cap > 0:
                glob_inv = sum(abs(v) * abs(self.avg_cost.get(mk, 0.0))
                               for mk, v in self.inventory.items())
                if side == "buy" and glob_inv + size * bid > cap:
                    return {"ok": False,
                            "msg": "全局库存名义超上限 $%.0f，拒开仓" % cap}
            # 止损：未实现亏损超阈值强制平仓（以 mid 平仓，忽略 adverse 折扣）
            sl_frac = float(self.rigor.get("stop_loss_frac", 0) or 0)
            stop_loss_hit = False
            if inv != 0 and sl_frac > 0:
                cost = float(self.avg_cost.get(mkt, mid))
                if cost > 0:
                    if inv > 0 and (mid - cost) / cost < -sl_frac:
                        stop_loss_hit = True
                    if inv < 0 and (cost - mid) / cost < -sl_frac:
                        stop_loss_hit = True
            if side == "buy" and inv + size > max_skew:
                return {"ok": False,
                        "msg": "库存偏斜上限(%d)：该市场净多 %d" % (max_skew, inv)}
            if side == "sell" and inv - size < -max_skew:
                return {"ok": False,
                        "msg": "库存偏斜上限(%d)：该市场净空 %d" % (max_skew, inv)}
            self._seq += 1
            pid = "RMM%d" % self._seq
            ts = time.time()
            # 库存偏置报价：买腿吃完整价差(bid+adverse)，卖腿吃完整价差(ask-adverse)；
            # 有净头寸时把双边推离 mid 以抑制追单、鼓励平仓（clamp 在 [bid,ask] 内）。
            skew = float(self.rigor.get("inventory_skew", 0.0))
            off = skew * spread
            buy_base = bid + (adverse + extra) * spread
            sell_base = ask - (adverse + extra) * spread
            if inv > 0:
                sell_base = min(ask, sell_base + off)
                buy_base = max(bid, buy_base - off)
            elif inv < 0:
                buy_base = max(bid, buy_base - off)
                sell_base = min(ask, sell_base + off)
            if side == "buy":
                build_base = mid if stop_loss_hit else buy_base
                avg_fill, _, slip, tiers = model_fill("buy", build_base, size, liq,
                                                      self.rigor)
                fee = leg_fee(avg_fill, size, self.fee_rate)
                self.cash -= avg_fill * size + fee
                self.fees_paid += fee
                self.slippage_paid += slip * size
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
                    "tiers": tiers, "pnl": 0.0,
                    "cash_after": round(self.cash, 2),
                })
                self._record_volume(mkt, avg_fill * size)
                pnl = 0.0
                msg = ("建多仓(滑点%.4f/单位,%d档): 买@%.4f×%d，净库存%d（未锁利）"
                       % (slip, tiers, avg_fill, size, self.inventory[mkt]))
            else:
                hedge_base = mid if stop_loss_hit else sell_base
                avg_fill, _, slip, tiers = model_fill("sell", hedge_base, size,
                                                      liq, self.rigor)
                fee = leg_fee(avg_fill, size, self.fee_rate)
                self.cash += avg_fill * size - fee
                self.fees_paid += fee
                self.slippage_paid += slip * size
                self.inventory[mkt] = inv - size
                pnl = 0.0
                avg_cost = float(self.avg_cost.get(mkt, avg_fill))
                if self.inventory[mkt] == 0:
                    buy_fee = leg_fee(avg_cost, size, self.fee_rate)
                    locked_raw = realized_pnl(avg_cost, avg_fill, size) - fee - buy_fee
                    asel = round(locked_raw * float(self.rigor.get(
                        "adverse_selection_frac", 0.0)), 4)
                    locked = round(locked_raw - asel, 4)
                    self.realized_pnl += locked
                    self.adverse_sel_loss += asel
                    pnl = locked
                    self.avg_cost[mkt] = 0.0
                    msg = ("对冲(滑点%.4f/单位,%d档,逆向选择损耗$%.2f): 卖@%.4f×%d，"
                           "净库存归0，净锁利$%.2f" % (slip, tiers, asel,
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
                    "tiers": tiers, "pnl": round(pnl, 4),
                    "cash_after": round(self.cash, 2),
                })
            self._record_volume(mkt, avg_fill * size)
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

    # ---------- 逆向选择损耗持久化（覆盖基类，附加字段） ----------
    def _load(self):
        super()._load()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.adverse_sel_loss = float(d.get("adverse_sel_loss", 0.0))
            self.fees_paid = float(d.get("fees_paid", 0.0))
            self.slippage_paid = float(d.get("slippage_paid", 0.0))
            self.end_dates = d.get("end_dates", {}) or {}
            self.settled_pnl = float(d.get("settled_pnl", 0.0))
            self.settlement_events = d.get("settlement_events", []) or []
        except Exception:
            self.adverse_sel_loss = getattr(self, "adverse_sel_loss", 0.0)
            self.end_dates = getattr(self, "end_dates", {})
            self.settled_pnl = getattr(self, "settled_pnl", 0.0)
            self.settlement_events = getattr(self, "settlement_events", [])

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({
                    "cash": self.cash, "bankroll": self.bankroll,
                    "realized_pnl": self.realized_pnl,
                    "positions": self.positions, "inventory": self.inventory,
                    "avg_cost": self.avg_cost, "inv_q": self.inv_q,
                    "seq": self._seq, "max_skew": self.max_skew,
                    "fee_rate": self.fee_rate,
                    "adverse_sel_loss": round(self.adverse_sel_loss, 2),
                    "fees_paid": round(self.fees_paid, 2),
                    "slippage_paid": round(self.slippage_paid, 2),
                    "end_dates": self.end_dates,
                    "settled_pnl": round(self.settled_pnl, 2),
                    "settlement_events": self.settlement_events[-200:],
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------- 库存盯市（成本基准权益） ----------
    def equity_at_cost(self):
        """真实权益 = 现金 + 未平仓库存按成本基准计价。
        纠正'账本现金下降=亏损'的账面假象：库存是净多持仓，非已实现的损失。"""
        with self.lock:
            inv_val = sum(v * abs(self.avg_cost.get(mk, 0.0))
                          for mk, v in self.inventory.items() if v != 0)
            return round(self.cash + inv_val, 2)

    def equity_marked(self):
        """盯市权益 = 现金 + 未平仓库存按最近中间价(last_mid)计价（含未实现盈亏）。"""
        with self.lock:
            inv_val = 0.0
            for mk, v in self.inventory.items():
                if v == 0:
                    continue
                px = self.last_mid.get(mk, abs(self.avg_cost.get(mk, 0.0)))
                inv_val += v * px
            return round(self.cash + inv_val, 2)

    def inventory_notional(self):
        with self.lock:
            return round(sum(abs(v) * abs(self.avg_cost.get(mk, 0.0))
                              for mk, v in self.inventory.items()), 2)

    # ---------- 结算风险闭环（P1-1） ----------
    def settlement_exposure(self):
        """当前仍持开放库存的市场，若全部按最不利方向结算（多头 yes 归 0 /
        空头 yes 归 1）的最大不利现金变动——即'仍暴露在结算风险下的金额'。"""
        with self.lock:
            exp = 0.0
            for mk, v in self.inventory.items():
                if v == 0:
                    continue
                c = float(self.avg_cost.get(mk, 0.0))
                s = abs(v)
                # 多头 yes 归 0 损失 s*c；空头 yes 归 1 损失 s*(1-c)
                exp += s * (c if v > 0 else (1.0 - c))
            return round(exp, 2)

    def settle_expired_markets(self, now=None, resolve_fn=None):
        """结算已到期且仍有库存的市场：把'逐笔锁利'升级为'已结算锁利'。

        - 对每个 end_date 已过、库存非 0 的市场，取真实结算价 resolve_fn(token_id)。
        - 结算价取不到（未结算 / API 失败）时用最近中间价 last_mid 暂估并标记
          pending，待下一轮复核，避免凭空确认收益（不伪造成功）。
        - 返回本次触发的结算事件列表。
        """
        now = now or time.time()
        if resolve_fn is None:
            resolve_fn = lambda tid: None
        events = []
        with self.lock:
            for mkt, inv in list(self.inventory.items()):
                if inv == 0:
                    continue
                ed = self.end_dates.get(mkt)
                ts = parse_end_date(ed) if ed else None
                if ts is None or ts > now:
                    continue
                rp = resolve_fn(mkt)
                cost = float(self.avg_cost.get(mkt, 0.0))
                s = abs(inv)
                pending = rp is None
                if rp is None:
                    rp = float(self.last_mid.get(mkt, 0.0))
                buy_fee = leg_fee(cost, s, self.fee_rate)
                redeem_fee = leg_fee(rp, s, self.fee_rate)
                if inv > 0:
                    self.cash += s * rp - redeem_fee
                    pnl = round(s * (rp - cost) - buy_fee - redeem_fee, 4)
                else:
                    self.cash -= s * rp + redeem_fee
                    pnl = round(s * (cost - rp) - buy_fee - redeem_fee, 4)
                self.realized_pnl += pnl
                self.settled_pnl += pnl
                ev = {
                    "ts": round(now, 3), "mkt": mkt,
                    "question": self.inv_q.get(mkt, ""),
                    "side": "long" if inv > 0 else "short",
                    "size": s, "cost": round(cost, 4),
                    "resolved_price": round(rp, 4), "pending": pending,
                    "pnl": pnl,
                }
                self.settlement_events.append(ev)
                events.append(ev)
                self.inventory[mkt] = 0
                self.avg_cost[mkt] = 0.0
                self.end_dates.pop(mkt, None)
            if events:
                self._save()
        return events

    def settlement_summary(self):
        """供看板 / 报告展示的结算风险摘要。"""
        with self.lock:
            n_pending = sum(1 for e in self.settlement_events if e.get("pending"))
            return {
                "settled_pnl": round(self.settled_pnl, 2),
                "n_settled": len(self.settlement_events),
                "n_pending": n_pending,
                "exposure": self.settlement_exposure(),
                "events": self.settlement_events[-20:],
            }

    def pnl_attribution(self, realized_total):
        """盈亏归因瀑布（P1-3）：把累计净锁利拆成
        毛价差捕获 − 走簿滑点 − 手续费 − 逆向选择 + 结算净损益。

        realized_total 为 STATE['realized']（已含结算损益）；book 自身累计器提供各成本分量。
        恒等式：gross = realized − settled + fees + slip + asel。各分量均来自真实成交记录，
        不编造。"""
        fees = self.fees_paid
        slip = self.slippage_paid
        asel = self.adverse_sel_loss
        settled = self.settled_pnl
        gross = float(realized_total) - settled + fees + slip + asel
        return {
            "gross_spread": round(gross, 2),
            "walk_the_book": round(-slip, 2),
            "fees": round(-fees, 2),
            "adverse_selection": round(-asel, 2),
            "settlement": round(settled, 2),
            "net": round(float(realized_total), 2),
        }


# ============ 敏感性分析（P1-2）：参数对一轮做市对冲期望锁利的边际影响 ============
def mm_expected_lock(rigor, mid=0.5, spread=0.02, liq=50000.0, fee_rate=0.01):
    """代表性市场（mid=0.5、价差=0.02、流动性=50000、单笔 size 取 rigor.size）
    一轮「建仓→对冲」的期望锁利（USD），用于敏感性分析的基准量。

    与 market_make 真实成交模型一致：买腿吃 bid+adverse*spread、卖腿吃 ask-adverse*spread，
    再扣双边手续费、扣逆向选择蒸发比例。size 超过档位深度时叠加走簿滑点。
    """
    a = float(rigor.get("adverse_frac", 0.15))
    asel = float(rigor.get("adverse_selection_frac", 0.10))
    f = float(rigor.get("fee_rate", fee_rate))
    size = int(float(rigor.get("size", 100) or 100))
    bid = mid - spread / 2.0
    ask = mid + spread / 2.0
    buy_fill, _, _, _ = model_fill("buy", bid + a * spread, size, liq, rigor)
    sell_fill, _, _, _ = model_fill("sell", ask - a * spread, size, liq, rigor)
    gross = (sell_fill - buy_fill) * size
    fees = leg_fee(buy_fill, size, f) + leg_fee(sell_fill, size, f)
    locked_raw = gross - fees
    asel_loss = locked_raw * asel
    return round(locked_raw - asel_loss, 4)


def mm_param_sensitivity(rigor, fee_rate=0.01):
    """对每个可调参数做 ±step 扰动，估计对一轮期望锁利的弹性（敏感性排序）。

    返回 {base_pnl, rep, params:[{param, base, step, pnl_up, pnl_dn,
    delta_pnl, elasticity}]}（按 |elasticity| 降序）。elasticity = (pnl_up-pnl_dn)/2 / base。

    诚实边界：基于代表性市场解析估计，非全量重模拟；用于看板快速定位「哪个旋钮最影响盈亏」。
    """
    rep = {"mid": 0.5, "spread": 0.02, "liq": 50000.0, "size": int(float(rigor.get("size", 100) or 100))}
    base = mm_expected_lock(rigor, fee_rate=fee_rate)
    specs = [
        ("adverse_frac", float(rigor.get("adverse_frac", 0.15) or 0.15), 0.05),
        ("adverse_selection_frac", float(rigor.get("adverse_selection_frac", 0.10) or 0.10), 0.05),
        ("fee_rate", float(fee_rate), 0.005),
        ("size", float(rep["size"]), 20.0),
        ("tick", float(rigor.get("tick", 0.002) or 0.002), 0.001),
        ("depth_frac", float(rigor.get("depth_frac", 0.01) or 0.01), 0.005),
        ("inventory_skew", float(rigor.get("inventory_skew", 0.5) or 0.5), 0.2),
        ("stop_loss_frac", float(rigor.get("stop_loss_frac", 0.05) or 0.05), 0.02),
    ]
    out = []
    for name, val, step in specs:
        if val <= 0:
            continue
        r_up = dict(rigor); r_up[name] = val + step
        r_dn = dict(rigor); r_dn[name] = max(0.0, val - step)
        up = mm_expected_lock(r_up)
        dn = mm_expected_lock(r_dn)
        delta = (up - dn) / 2.0
        elasticity = (delta / base) if base else 0.0
        out.append({"param": name, "base": round(val, 4), "step": step,
                    "pnl_up": up, "pnl_dn": dn,
                    "delta_pnl": round(delta, 4),
                    "elasticity": round(elasticity, 3)})
    out.sort(key=lambda x: -abs(x["elasticity"]))
    return {"base_pnl": base, "rep": rep, "params": out}


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

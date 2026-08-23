# -*- coding: utf-8 -*-
"""跨平台套利模拟账本（不碰任何真实资金/下单接口）。

VirtualBook 维护：
  - cash：虚拟本金（默认 $10,000）
  - positions：做市成交腿(mm_leg) 与跨平台套利持仓(long/short)
  - realized_pnl：已实现盈亏
  - inventory：每个市场(market_id)的净库存(份额, +多 -空) —— 做市偏斜控制核心
  - avg_cost：每个市场的建仓均价

做市模型（单边成交 + 自动反向对冲）：
  market_make 模拟做市商在同一市场双边挂单，但按真实场景**逐腿成交**：
    · 库存为 0 时首笔在 bid 买入建多仓（不锁利润，仅记库存）；
    · 库存 > 0（已净多）时自动在 ask 卖出对冲，库存归 0 时锁定 spread 利润；
    · 库存 < 0（已净空）时自动在 bid 买入对冲。
  这样单市场连续做市天然形成「建仓→对冲→建仓→对冲」循环，库存始终被
  偏斜上限(max_skew)约束，实现「单边成交后自动反向对冲」。
  rebalance(price_map)：把仍偏斜的市场以实时价强制对冲平仓，库存归 0。

持久化到独立文件 arb_book.json（与 A股模拟盘 sim_book.json 完全分离）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from core.strategy import (leg_fee, realized_pnl, unrealized_pnl,
                           arb_avg_cost_on_buy)

DEFAULT_BANKROLL = 10000.0
DEFAULT_MAX_SKEW = 300        # 单市场最大净库存(份额)，防过度集中于单一市场
DEFAULT_FEE = 0.01             # 单边撮合手续费(默认 1%，可配置)，每次腿成交按成交额计
_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "arb_book.json")


class VirtualBook:
    def __init__(self, path=_DEFAULT_PATH, bankroll=DEFAULT_BANKROLL):
        self.path = path
        self.lock = threading.Lock()
        self.cash = bankroll
        self.bankroll = bankroll
        self.positions = []
        self.realized_pnl = 0.0
        self.inventory = {}      # market_id -> 净库存(份额, +多 -空)
        self.avg_cost = {}       # market_id -> 建仓均价
        self.inv_q = {}          # market_id -> 问题文案(展示用)
        self._seq = 0
        self.max_skew = DEFAULT_MAX_SKEW   # 单市场最大净库存(份额)，面板可调、持久化
        self.fee_rate = DEFAULT_FEE        # 单边撮合手续费率，面板可调、持久化
        self._load()

    # ---------- 持久化 ----------
    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.cash = float(d.get("cash", self.bankroll))
            self.bankroll = float(d.get("bankroll", self.bankroll))
            self.realized_pnl = float(d.get("realized_pnl", 0.0))
            self.positions = d.get("positions", [])
            self.inventory = d.get("inventory", {}) or {}
            self.avg_cost = d.get("avg_cost", {}) or {}
            self.inv_q = d.get("inv_q", {}) or {}
            self._seq = int(d.get("seq", 0))
            self.max_skew = int(d.get("max_skew", DEFAULT_MAX_SKEW))
            if self.max_skew < 10 or self.max_skew > 5000:
                self.max_skew = DEFAULT_MAX_SKEW
            fr = float(d.get("fee_rate", DEFAULT_FEE))
            self.fee_rate = fr if 0.0 <= fr <= 0.1 else DEFAULT_FEE
        except Exception:
            pass

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({
                    "cash": self.cash,
                    "bankroll": self.bankroll,
                    "realized_pnl": self.realized_pnl,
                    "positions": self.positions,
                    "inventory": self.inventory,
                    "avg_cost": self.avg_cost,
                    "inv_q": self.inv_q,
                    "seq": self._seq,
                    "max_skew": self.max_skew,
                    "fee_rate": self.fee_rate,
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------- 操作 ----------
    def execute_arb(self, opp, size_shares):
        """执行一笔跨平台套利（模拟，仅演示对 demo=True 用）。"""
        size = int(size_shares)
        if size < 1:
            return {"ok": False, "msg": "份额必须 >= 1"}
        edge = float(opp.get("edge", 0.0))
        if edge <= 0:
            return {"ok": False, "msg": "该机会无正收益，不执行"}
        cost = float(opp.get("buy_ask", 0.0)) * size
        if cost > self.cash:
            return {"ok": False, "msg": "虚拟本金不足（需 $%.2f，余 $%.2f）"
                    % (cost, self.cash)}
        with self.lock:
            self._seq += 1
            base = self._seq
            ts = time.time()
            self.positions.append({
                "pid": "L%d" % base, "kind": "long", "venue": opp["buy_venue"],
                "market_id": opp["buy_id"], "question": opp.get("question", ""),
                "outcome": opp.get("outcome_label", "primary"),
                "entry": float(opp["buy_ask"]), "size": size, "ts": ts,
                "arb": "A%d" % base,
            })
            self.positions.append({
                "pid": "S%d" % base, "kind": "short", "venue": opp["sell_venue"],
                "market_id": opp["sell_id"], "question": opp.get("question", ""),
                "outcome": opp.get("outcome_label", "primary"),
                "entry": float(opp["sell_bid"]), "size": size, "ts": ts,
                "arb": "A%d" % base,
            })
            pnl = edge * size
            self.cash += pnl
            self.realized_pnl += pnl
            self._save()
            return {
                "ok": True,
                "msg": "已模拟成交：买 %s @ %.4f / 卖 %s @ %.4f ×%d，锁定收益 $%.2f"
                       % (opp["buy_venue"], opp["buy_ask"], opp["sell_venue"],
                          opp["sell_bid"], size, pnl),
                "pnl": pnl, "cash": self.cash,
                "positions": ["L%d" % base, "S%d" % base],
            }

    def market_make(self, opp, size_shares, max_skew=None):
        """模拟做市（单边成交 + 自动反向对冲，库存偏斜受控）。

        方向自动决定：
          · 当前净库存 > 0（已多）→ 本次在 ask 卖出对冲；
          · 当前净库存 < 0（已空）→ 本次在 bid 买入对冲；
          · 当前为 0（首笔）    → 本次在 bid 买入建多仓。
        偏斜上限：若本次将使 |净库存| 超过 max_skew，则拒绝（提示先再平衡）。
        利润：仅在对冲使库存归 0 的成交中实现 = (ask - 建仓均价) * 份额。
        opp 来自 arbitrage.scan_poly_marketmaking（buy_ask=bid, sell_bid=ask, 同标的）。
        """
        size = int(size_shares)
        if size < 1:
            return {"ok": False, "msg": "份额必须 >= 1"}
        if max_skew is None:
            max_skew = self.max_skew
        bid = float(opp.get("buy_ask", 0.0))    # 买价(bid)
        ask = float(opp.get("sell_bid", 0.0))   # 卖价(ask)
        if bid <= 0 or ask <= bid:
            return {"ok": False, "msg": "价差非正，无法做市"}
        mkt = opp.get("buy_id")
        q = opp.get("question", "")
        with self.lock:
            inv = int(self.inventory.get(mkt, 0))
            side = "sell" if inv > 0 else "buy"
            if side == "buy" and inv + size > max_skew:
                return {"ok": False,
                        "msg": "库存偏斜上限(%d)：该市场净多 %d，先点「再平衡」"
                               % (max_skew, inv)}
            if side == "sell" and inv - size < -max_skew:
                return {"ok": False,
                        "msg": "库存偏斜上限(%d)：该市场净空 %d，先点「再平衡」"
                               % (max_skew, inv)}
            self._seq += 1
            pid = "MM%d" % self._seq
            ts = time.time()
            fee = 0.0
            if side == "buy":
                fee = leg_fee(bid, size, self.fee_rate)
                self.cash -= bid * size + fee
                self.inventory[mkt] = inv + size
                prev_avg = float(self.avg_cost.get(mkt, 0.0))
                self.avg_cost[mkt] = arb_avg_cost_on_buy(prev_avg, inv, bid, size)
                self.inv_q[mkt] = q
                self.positions.append({
                    "pid": pid, "kind": "mm_leg", "side": "buy", "mkt": mkt,
                    "venue": opp.get("buy_venue", "poly"), "question": q,
                    "entry": round(bid, 4), "size": size, "ts": ts,
                    "fee": round(fee, 4),
                    "cash_after": round(self.cash, 2),
                })
                msg = "建多仓：买 @%.4f ×%d，当前净库存 %d（未锁利润，待对冲）" \
                      % (bid, size, self.inventory[mkt])
                pnl = 0.0
            else:
                fee = leg_fee(ask, size, self.fee_rate)
                self.cash += ask * size - fee
                self.inventory[mkt] = inv - size
                pnl = 0.0
                if self.inventory[mkt] == 0:
                    buy_fee = leg_fee(float(self.avg_cost.get(mkt, bid)), size,
                                     self.fee_rate)
                    locked = round(realized_pnl(float(self.avg_cost.get(mkt, ask)),
                                               ask, size) - fee - buy_fee, 4)
                    self.realized_pnl += locked
                    pnl = locked
                    self.avg_cost[mkt] = 0.0
                    msg = "对冲平仓：卖 @%.4f ×%d，净库存归 0，锁定价差净收益 $%.2f（含费）" \
                          % (ask, size, locked)
                else:
                    msg = "部分对冲：卖 @%.4f ×%d，当前净库存 %d" \
                          % (ask, size, self.inventory[mkt])
                self.inv_q[mkt] = q
                self.positions.append({
                    "pid": pid, "kind": "mm_leg", "side": "sell", "mkt": mkt,
                    "venue": opp.get("sell_venue", "poly"), "question": q,
                    "entry": round(ask, 4), "size": size, "ts": ts,
                    "fee": round(fee, 4),
                    "cash_after": round(self.cash, 2),
                })
            self._save()
            return {"ok": True, "msg": msg, "pnl": pnl,
                    "cash": round(self.cash, 2), "pid": pid, "side": side,
                    "inventory": self.inventory[mkt]}

    def set_max_skew(self, value):
        """面板可调：设置单市场最大净库存上限（10~5000，持久化到 arb_book.json）。"""
        try:
            v = int(value)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "偏斜上限必须是 10~5000 的整数"}
        if v < 10:
            return {"ok": False, "msg": "偏斜上限过小（建议 >= 10，避免库存失控）"}
        if v > 5000:
            return {"ok": False, "msg": "偏斜上限过大（建议 <= 5000）"}
        with self.lock:
            self.max_skew = v
            self._save()
        return {"ok": True, "msg": "偏斜上限已设为 %d（已保存）" % v,
                "max_skew": v}

    def set_fee(self, value):
        """面板可调：设置单边撮合手续费率（0~0.1 即 0%~10%，默认 1%，持久化）。"""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "费率必须是 0~0.1 的小数（如 0.01=1%）"}
        if v < 0:
            return {"ok": False, "msg": "费率不能为负"}
        if v > 0.1:
            return {"ok": False, "msg": "费率过大（建议 <= 0.1 即 10%）"}
        with self.lock:
            self.fee_rate = v
            self._save()
        return {"ok": True, "msg": "手续费率已设为 %.2f%%（已保存）"
                % (v * 100), "fee_rate": v}

    def rebalance(self, price_map=None, max_skew=None):
        """把仍偏斜的市场以实时价强制对冲平仓，库存归 0，锁定对应价差利润。

        price_map: {market_id: {"bid":..,"ask":..}} 来自实时行情；
        缺失时用建仓均价近似（利润≈0，仅清库存）。
        """
        with self.lock:
            done = 0
            pnl_total = 0.0
            for mkt, raw_inv in list(self.inventory.items()):
                inv = int(raw_inv)
                if inv == 0:
                    continue
                pm = (price_map or {}).get(mkt) or {}
                if inv > 0:
                    ask = float(pm.get("ask") or self.avg_cost.get(mkt, 0.0))
                    if ask <= 0:
                        continue
                    fee = leg_fee(ask, inv, self.fee_rate)
                    self.cash += ask * inv - fee
                    pnl = round(realized_pnl(float(self.avg_cost.get(mkt, ask)),
                                            ask, inv) - fee, 4)
                    self.realized_pnl += pnl
                    pnl_total += pnl
                    self._seq += 1
                    self.positions.append({
                        "pid": "MM%d" % self._seq, "kind": "mm_leg",
                        "side": "sell", "mkt": mkt, "venue": "poly",
                        "question": "再平衡对冲", "entry": round(ask, 4),
                        "size": inv, "ts": time.time(), "fee": round(fee, 4),
                        "cash_after": round(self.cash, 2),
                    })
                else:
                    bid = float(pm.get("bid") or self.avg_cost.get(mkt, 0.0))
                    if bid <= 0:
                        continue
                    fee = leg_fee(bid, -inv, self.fee_rate)
                    self.cash -= bid * (-inv) + fee
                    pnl = round(unrealized_pnl(float(self.avg_cost.get(mkt, bid)),
                                              bid, inv) - fee, 4)
                    self.realized_pnl += pnl
                    pnl_total += pnl
                    self._seq += 1
                    self.positions.append({
                        "pid": "MM%d" % self._seq, "kind": "mm_leg",
                        "side": "buy", "mkt": mkt, "venue": "poly",
                        "question": "再平衡对冲", "entry": round(bid, 4),
                        "size": -inv, "ts": time.time(), "fee": round(fee, 4),
                        "cash_after": round(self.cash, 2),
                    })
                self.inventory[mkt] = 0
                self.avg_cost[mkt] = 0.0
                done += 1
            self._save()
            return {"ok": True,
                    "msg": "已再平衡 %d 个市场，锁定/调整利润 $%.2f"
                           % (done, pnl_total),
                    "rebalanced": done, "pnl": pnl_total}

    def settle(self, pid):
        """结算单个跨平台套利持仓（演示用）。做市腿请用 rebalance 管理。"""
        with self.lock:
            before = len(self.positions)
            self.positions = [p for p in self.positions if p["pid"] != pid]
            if len(self.positions) == before:
                return {"ok": False, "msg": "未找到持仓 %s" % pid}
            self._save()
            return {"ok": True, "msg": "已结算 %s" % pid}

    def view(self, price_map=None):
        """price_map: {market_id: {"bid":..,"ask":..,"mid":..}} 实时行情，
        用于按 mid 重估单边库存的未实现盈亏（方向性风险敞口）。"""
        with self.lock:
            inv_view = []
            unreal = 0.0
            for mkt, raw_inv in self.inventory.items():
                net = int(raw_inv)
                if net == 0:
                    continue
                avg = float(self.avg_cost.get(mkt, 0.0))
                pm = (price_map or {}).get(mkt) or {}
                mid = float(pm.get("mid") or pm.get("ask") or pm.get("bid")
                            or avg)
                u = unrealized_pnl(avg, mid, net)
                unreal += u
                inv_view.append({
                    "mkt": mkt, "net": net,
                    "avg_cost": round(avg, 4),
                    "mid": round(mid, 4),
                    "unrealized": round(u, 2),
                    "skew": round(net / self.max_skew, 3),
                    "question": self.inv_q.get(mkt, ""),
                })
            inv_view.sort(key=lambda x: abs(x["net"]), reverse=True)
            return {
                "cash": round(self.cash, 2),
                "bankroll": round(self.bankroll, 2),
                "realized_pnl": round(self.realized_pnl, 2),
                "unrealized_pnl": round(unreal, 2),
                "equity": round(self.cash + unreal, 2),
                "open_positions": len(self.positions),
                "max_skew": self.max_skew,
                "fee_rate": self.fee_rate,
                "inventory": inv_view,
                "positions": [
                    {k: p.get(k) for k in ("pid", "kind", "side", "venue",
                                          "question", "entry", "size", "mkt",
                                          "fee", "ts", "cash_after")}
                    for p in self.positions
                ],
            }

    def reset(self):
        with self.lock:
            self.cash = self.bankroll
            self.realized_pnl = 0.0
            self.positions = []
            self.inventory = {}
            self.avg_cost = {}
            self.inv_q = {}
            self._save()
            return {"ok": True, "msg": "虚拟账本已重置（保留偏斜上限设置）"}


_book = None


def get_book():
    global _book
    if _book is None:
        _book = VirtualBook()
    return _book


if __name__ == "__main__":
    import json as _j
    print(_j.dumps(get_book().view(), ensure_ascii=False, indent=2))

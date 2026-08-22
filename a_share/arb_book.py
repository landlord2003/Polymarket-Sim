# -*- coding: utf-8 -*-
"""跨平台套利模拟账本（不碰任何真实资金/下单接口）。

VirtualBook 维护：
  - cash：虚拟本金（默认 $10,000）
  - positions：跨平台套利持仓（两腿对冲，锁定无风险收益）
  - realized_pnl：已实现盈亏
执行一笔跨平台套利 = 在便宜平台买入 primary、在贵平台卖出 primary，
两腿对冲后无论事件结果如何净敞口为 0，收益在成交时即锁定 = (卖价 - 买价) * 份额。
结算(settle)仅用于走完生命周期、核销两腿（结果恒为 0 对冲）。
持久化到独立文件 arb_book.json（与 A股模拟盘 sim_book.json 完全分离）。
"""
from __future__ import annotations

import json
import os
import threading
import time

DEFAULT_BANKROLL = 10000.0
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
        self._seq = 0
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
            self._seq = int(d.get("seq", 0))
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
                    "seq": self._seq,
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------- 操作 ----------
    def execute_arb(self, opp, size_shares):
        """执行一笔跨平台套利（模拟）。

        opp 来自 arbitrage.scan 的单个机会，含：
          buy_venue/buy_id/buy_ask, sell_venue/sell_id/sell_bid,
          question, edge(每份额外收益), outcome_label
        size_shares：买入/卖出的份额（整数，>=1）
        返回 {ok, msg, pnl, positions}
        """
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
            pnl = edge * size  # 成交即锁定的无风险收益
            self.cash += pnl
            self.realized_pnl += pnl
            self._save()
            return {
                "ok": True,
                "msg": "已模拟成交：买 %s @ %.4f / 卖 %s @ %.4f ×%d，锁定收益 $%.2f"
                       % (opp["buy_venue"], opp["buy_ask"], opp["sell_venue"],
                          opp["sell_bid"], size, pnl),
                "pnl": pnl,
                "cash": self.cash,
                "positions": ["L%d" % base, "S%d" % base],
            }

    def settle(self, pid):
        """结算单个持仓（演示用：走完生命周期，对冲腿互抵为 0）。"""
        with self.lock:
            before = len(self.positions)
            self.positions = [p for p in self.positions if p["pid"] != pid]
            if len(self.positions) == before:
                return {"ok": False, "msg": "未找到持仓 %s" % pid}
            self._save()
            return {"ok": True, "msg": "已结算 %s" % pid}

    def view(self):
        with self.lock:
            return {
                "cash": round(self.cash, 2),
                "bankroll": round(self.bankroll, 2),
                "realized_pnl": round(self.realized_pnl, 2),
                "open_positions": len(self.positions),
                "positions": [
                    {k: p[k] for k in ("pid", "kind", "venue", "question",
                                      "outcome", "entry", "size", "arb")}
                    for p in self.positions
                ],
            }

    def reset(self):
        with self.lock:
            self.cash = self.bankroll
            self.realized_pnl = 0.0
            self.positions = []
            self._save()


_book = None


def get_book():
    global _book
    if _book is None:
        _book = VirtualBook()
    return _book


if __name__ == "__main__":
    import json as _j
    print(_j.dumps(get_book().view(), ensure_ascii=False, indent=2))

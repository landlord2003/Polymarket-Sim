# -*- coding: utf-8 -*-
"""真实下单层（生产骨架，DRY_RUN 默认不接真钱）。

设计目标：把 B 设计文档落成可运行代码。策略层(sim_trader / sim_rigor)完全
无感切换——通过环境变量 LIVE / DRY_RUN 控制，默认 DRY_RUN=True（任何
真实下单/签名/链上读取都走 stub，绝不动真钱）。

关键安全约束：
- DRY_RUN=True 时，ClobExecutor 内部全部 delegate 到 DryRunExecutor（内存影子账本
  + 订单日志），不 import 任何网络/链上依赖，不发起任何 HTTP / 链上调用。
- 只有 DRY_RUN=False 且依赖(py_clob_client / eth_account)可用时，才会真正构造
  签名订单并 POST。缺失依赖时显式抛 RuntimeError，绝不静默绕过。

真实部署（国外）时：pip install py-clob-client eth-account，设置环境变量
POLY_PK(钱包私钥) / CLOB_API_KEY / CLOB_API_SECRET / CLOB_PASSPHRASE，LIVE=1 DRY_RUN=0。
"""
from __future__ import annotations

import os
import sys
import time
import json
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

# 复用模拟盘的走簿成交模型，做 DRY_RUN 影子成交
try:
    from sim_rigor import model_fill
except Exception:  # pragma: no cover
    def model_fill(side, price, size, liquidity, rigor):
        return float(price), int(size), 0.0, 1


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class OrderResult:
    def __init__(self, ok, order_id=None, filled=0, avg_fill=None,
                 msg="", raw=None, dry=False):
        self.ok = ok
        self.order_id = order_id
        self.filled = filled
        self.avg_fill = avg_fill
        self.msg = msg
        self.raw = raw or {}
        self.dry = dry

    def to_dict(self):
        return {"ok": self.ok, "order_id": self.order_id,
                "filled": self.filled, "avg_fill": self.avg_fill,
                "msg": self.msg, "dry": self.dry}


# =====================================================================
# 钱包：EIP-712 订单签名（DRY_RUN 返回占位签名）
# =====================================================================
class Wallet:
    """Polymarket CTC 订单签名。私钥仅从环境变量 POLY_PK 读取，不落盘。

    DRY_RUN=True: 返回确定性占位签名（不加载 eth_account，不接触密钥）。
    DRY_RUN=False: 用 eth_account 对 EIP-712 结构签名（懒加载依赖）。
    """

    def __init__(self, dry_run=True):
        self.dry_run = dry_run
        self._pk = os.environ.get("POLY_PK", "")
        self._acct = None

    def address(self):
        if self.dry_run or not self._pk:
            return "0xDRYRUN" + "0" * 36
        self._ensure_acct()
        return self._acct.address

    def _ensure_acct(self):
        if self._acct is not None:
            return
        try:
            from eth_account import Account
        except ImportError as e:
            raise RuntimeError(
                "真实签名需要 eth_account（pip install eth-account），"
                "或设 DRY_RUN=1 使用占位签名") from e
        self._acct = Account.from_key(self._pk)

    def sign_order(self, order):
        """order: dict(EIP-712 字段)。返回 signature 字符串。"""
        if self.dry_run:
            return "0xdryrun_" + uuid.uuid4().hex
        self._ensure_acct()
        try:
            from py_clob_client.signer import ClobAuth
            raise RuntimeError("请通过 ClobExecutor 经 clob.sign_order 完成 EIP-712 签名")
        except ImportError as e:
            raise RuntimeError(
                "真实签名需要 py_clob_client（pip install py-clob-client），"
                "或设 DRY_RUN=1 使用占位签名") from e


# =====================================================================
# 执行器抽象 + DRY_RUN 实现 + 真实 Clob 实现
# =====================================================================
class OrderExecutor(ABC):
    @abstractmethod
    def submit(self, market_id, side, price, size, idempotency_key=None,
               liquidity=0.0):
        ...

    @abstractmethod
    def cancel(self, order_id):
        ...

    @abstractmethod
    def positions(self):
        ...

    @abstractmethod
    def usdc_balance(self):
        ...

    @abstractmethod
    def is_dry_run(self):
        ...


class DryRunExecutor(OrderExecutor):
    """内存影子账本：模拟真实下单的成交、持仓、余额。纯本地，零网络。"""

    def __init__(self, log_path=None, start_usdc=10000.0, fee_rate=0.01):
        self.cash = float(start_usdc)
        self.fee_rate = fee_rate
        self.inv = {}
        self.avg_cost = {}
        self.orders = {}
        self.log_path = log_path or os.path.join(_HERE, "live_dryrun_orders.jsonl")
        self._seq = 0

    def is_dry_run(self):
        return True

    def submit(self, market_id, side, price, size, idempotency_key=None,
               liquidity=0.0):
        size = int(size)
        if size < 1:
            return OrderResult(False, msg="size<1")
        self._seq += 1
        oid = "DRY%d" % self._seq
        rigor = {"depth_frac": 0.01, "tick": 0.002}
        af, _, slip, _ = model_fill(side, price, size, liquidity or 1e9, rigor)
        fee = af * size * self.fee_rate
        if side == "buy":
            self.cash -= af * size + fee
            inv = int(self.inv.get(market_id, 0))
            prev = float(self.avg_cost.get(market_id, 0.0))
            self.avg_cost[market_id] = (prev * inv + af * size) / (inv + size) if (inv + size) else af
            self.inv[market_id] = inv + size
        else:
            self.cash += af * size - fee
            inv = int(self.inv.get(market_id, 0))
            if inv + (-size) == 0:
                self.avg_cost[market_id] = 0.0
            self.inv[market_id] = inv - size
        rec = {"oid": oid, "market_id": market_id, "side": side,
               "price": round(price, 4), "size": size,
               "avg_fill": round(af, 4), "slip": round(slip, 4),
               "fee": round(fee, 4), "ts": _now_iso()}
        self.orders[oid] = rec
        self._log(rec)
        return OrderResult(True, order_id=oid, filled=size, avg_fill=af,
                           msg="DRY_RUN 影子成交 @%.4f" % af, dry=True)

    def cancel(self, order_id):
        return OrderResult(True, order_id=order_id, msg="DRY_RUN 无挂单可撤", dry=True)

    def positions(self):
        return {m: {"net": s, "avg_cost": self.avg_cost.get(m, 0.0)}
                for m, s in self.inv.items() if s != 0}

    def usdc_balance(self):
        return self.cash

    def _log(self, rec):
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass


class ClobExecutor(OrderExecutor):
    """真实 Polymarket CTC 链下订单执行。DRY_RUN=True 时内部 delegate 到
    DryRunExecutor；DRY_RUN=False 时真正构造签名订单 POST。"""

    def __init__(self, dry_run=True, host="https://clob.polymarket.com",
                 start_usdc=10000.0, fee_rate=0.01, log_path=None):
        self.dry_run = dry_run
        self.host = host
        self.fee_rate = fee_rate
        self.wallet = Wallet(dry_run=dry_run)
        self._clob = None
        if dry_run:
            self._dry = DryRunExecutor(start_usdc=start_usdc, fee_rate=fee_rate,
                                        log_path=log_path)
        else:
            self._init_real()

    def _init_real(self):
        try:
            from py_clob_client.client import ClobClient
        except ImportError as e:
            raise RuntimeError(
                "真实下单需要 py_clob_client（pip install py-clob-client），"
                "或设 DRY_RUN=1 使用影子账本") from e
        key = os.environ.get("CLOB_API_KEY", "")
        if not key:
            raise RuntimeError("真实模式需要 CLOB_API_KEY 环境变量")
        self._clob = ClobClient(
            host=self.host,
            key=self.wallet._pk,
            chain_id=137,
            creds={"apiKey": key,
                   "apiSecret": os.environ.get("CLOB_API_SECRET", ""),
                   "passphrase": os.environ.get("CLOB_PASSPHRASE", "")},
        )

    def is_dry_run(self):
        return self.dry_run

    def submit(self, market_id, side, price, size, idempotency_key=None,
               liquidity=0.0):
        if self.dry_run:
            return self._dry.submit(market_id, side, price, size,
                                    idempotency_key, liquidity)
        order = self._clob.create_order(
            maker=maker_side(side),
            fee_rate_bps=int(self.fee_rate * 10000),
            price=round(price, 4),
            size=size,
            token_id=market_id,
        )
        signed = self._clob.sign_order(order)
        resp = self._clob.post_order(signed)
        return OrderResult(True, order_id=resp.get("orderID"),
                           filled=size, avg_fill=price,
                           msg="CLOB POST ok", raw=resp)

    def cancel(self, order_id):
        if self.dry_run:
            return self._dry.cancel(order_id)
        self._clob.cancel_order(order_id)
        return OrderResult(True, order_id=order_id, msg="CLOB cancel ok")

    def positions(self):
        if self.dry_run:
            return self._dry.positions()
        return self._clob.get_positions()

    def usdc_balance(self):
        if self.dry_run:
            return self._dry.usdc_balance()
        return float(self._clob.get_balance_allowance()["balance"])


def maker_side(side):
    """sim 的 buy/sell 对应 CTC 的 BUY/SELL（以 token 多空计）。"""
    return "BUY" if side == "buy" else "SELL"


# =====================================================================
# 对账：虚拟账本 vs 链上持仓（每日）
# =====================================================================
class Reconcile:
    def __init__(self, log_path=None):
        self.log_path = log_path or os.path.join(_HERE, "live_reconcile.jsonl")

    def daily(self, sim_inventory, live_exec):
        """sim_inventory: {mkt: net_shares}; live_exec: OrderExecutor。
        返回差异报告 dict。"""
        live_pos = live_exec.positions()
        diffs = []
        all_mkts = set(sim_inventory) | set(live_pos)
        for m in all_mkts:
            sim_n = int(sim_inventory.get(m, 0))
            liv_n = int((live_pos.get(m) or {}).get("net", 0))
            if sim_n != liv_n:
                diffs.append({"market": m, "sim": sim_n, "live": liv_n,
                              "delta": liv_n - sim_n})
        report = {
            "ts": _now_iso(),
            "dry_run": live_exec.is_dry_run(),
            "sim_markets": len(sim_inventory),
            "live_markets": len(live_pos),
            "mismatches": diffs,
            "balanced": len(diffs) == 0,
        }
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(report, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return report


# =====================================================================
# 熔断器：幂等去重 + 网络重试 + 资金阈值 + nonce + 拥堵
# =====================================================================
class CircuitBreaker:
    def __init__(self, min_usdc=50.0, max_retry=3, persist_path=None):
        self.min_usdc = float(min_usdc)
        self.max_retry = max_retry
        self._seen = {}
        self._nonce = 0
        self._persist = persist_path or os.path.join(_HERE, "live_nonce.json")
        self._load_nonce()

    def _load_nonce(self):
        try:
            with open(self._persist, encoding="utf-8") as f:
                self._nonce = json.load(f).get("nonce", 0)
        except Exception:
            self._nonce = 0

    def _save_nonce(self):
        try:
            with open(self._persist, "w", encoding="utf-8") as f:
                json.dump({"nonce": self._nonce}, f)
        except Exception:
            pass

    def dedupe(self, idempotency_key):
        if idempotency_key and idempotency_key in self._seen:
            return True, self._seen[idempotency_key]
        return False, None

    def remember(self, idempotency_key, result_dict):
        if idempotency_key:
            self._seen[idempotency_key] = result_dict

    def nonce_next(self):
        self._nonce += 1
        self._save_nonce()
        return self._nonce

    def funds_ok(self, usdc_balance):
        if usdc_balance < self.min_usdc:
            return False, ("USDC 余额 $%.2f 低于安全阈值 $%.2f，熔断暂停下单"
                           % (usdc_balance, self.min_usdc))
        return True, ""

    def congestion_ok(self):
        return True

    def with_retry(self, fn):
        last = None
        for i in range(self.max_retry):
            try:
                return fn()
            except Exception as e:
                last = e
                time.sleep(min(2 ** i, 8))
        raise RuntimeError("下单重试 %d 次仍失败: %s" % (self.max_retry, last))


# =====================================================================
# 工厂：按 LIVE/DRY_RUN 组装执行栈
# =====================================================================
def build_executor(live=False, dry_run=True, start_usdc=10000.0, fee_rate=0.01):
    if not live or dry_run:
        return DryRunExecutor(start_usdc=start_usdc, fee_rate=fee_rate)
    return ClobExecutor(dry_run=False, start_usdc=start_usdc, fee_rate=fee_rate)


if __name__ == "__main__":
    ex = build_executor(live=True, dry_run=True)
    r1 = ex.submit("MKT1", "buy", 0.50, 100, liquidity=50000)
    r2 = ex.submit("MKT1", "sell", 0.52, 100, liquidity=50000)
    print("DRY_RUN submit buy:", r1.to_dict())
    print("DRY_RUN submit sell:", r2.to_dict())
    print("positions:", ex.positions(), "usdc:", round(ex.usdc_balance(), 2))
    cb = CircuitBreaker()
    print("dedupe first:", cb.dedupe("k1"), "second:", cb.dedupe("k1"))
    print("nonce:", cb.nonce_next(), cb.nonce_next())
    print("OK")

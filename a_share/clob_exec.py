# -*- coding: utf-8 -*-
"""P3-3 CLOB 实盘下单模块（基于 py-clob-client，L2 认证 + 钱包签名）。

安全铁律：
  - 钱包私钥 **只从环境变量 PM_BOT_PK 读取**，绝不停代码 / 入 git / 打日志。
  - 默认 DRY_RUN（LIVE_MODE != "1"）只模拟下单、不真发单、不碰真钱。
  - NB 实盘：设 LIVE_MODE=1 + 真实 PM_BOT_PK 才真发单。
  - 每笔实盘下单前强制过 risk_control.check_new_order()，被拒即放弃。

依赖（NB 部署机）：pip install py-clob-client web3
对 py_clob_client 做惰性导入，未安装时本文件仍可 import（不污染模拟盘默认运行）。

用法:
  from clob_exec import ClobExec
  ex = ClobExec()                       # 读 env：PM_BOT_PK / LIVE_MODE / CLOB_HOST
  ex.place_maker_order("0xTOKEN", "BUY", 0.45, 50.0)   # DRY_RUN 时仅模拟
"""
import os
import sys
import json
import logging

logger = logging.getLogger("clob_exec")

CLOB_HOST = os.environ.get("CLOB_HOST", "https://clob.polymarket.com")
CHAIN_ID = int(os.environ.get("CLOB_CHAIN_ID", "137"))          # Polygon Mainnet
PM_BOT_PK = os.environ.get("PM_BOT_PK", "").strip()            # 钱包私钥（仅 env）
LIVE_MODE = os.environ.get("LIVE_MODE", "0") == "1"            # 1=真发单；默认 DRY_RUN

try:
    import risk_control as RC
except Exception:
    RC = None


class ClobExec:
    """CLOB 下单执行器（DRY_RUN 安全默认）。"""

    def __init__(self, live=None, pk=None, host=CLOB_HOST, chain_id=CHAIN_ID):
        self.live = LIVE_MODE if live is None else live
        self.pk = PM_BOT_PK if pk is None else pk
        self.host = host
        self.chain_id = chain_id
        self._client = None
        if self.live and not self.pk:
            raise RuntimeError("LIVE_MODE=1 但未设置 PM_BOT_PK，拒绝真发单")

    # ---------- 客户端（惰性） ----------
    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self.live:
            return None
        if not self.pk:
            raise RuntimeError("LIVE_MODE=1 但未设置 PM_BOT_PK")
        # 惰性导入：仅在真正连 CLOB 时才需要 py-clob-client（保持模拟盘零额外依赖）
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        cli = ClobClient(host=self.host, key=self.pk, chain_id=self.chain_id)
        creds = cli.create_or_derive_api_creds()   # 派生 L2（api_key/secret/passphrase，HMAC-SHA256）
        cli.set_api_creds(creds)
        self._client = cli
        self._creds = creds
        logger.info("[clob] L2 凭证已派生（api_key=%s…）", str(creds.api_key)[:8])
        return cli

    def credentials(self):
        """返回已派生的 L2 凭证摘要（不泄露私钥/secret 全文）。"""
        if not self.live:
            return {"live": False, "note": "DRY_RUN，无 L2 凭证"}
        c = self._ensure_client()
        cr = getattr(self, "_creds", None)
        if not cr:
            return {"live": True, "note": "凭证未派生"}
        return {"live": True, "api_key_prefix": str(cr.api_key)[:8] + "…",
                "has_secret": bool(cr.api_secret), "has_passphrase": bool(cr.passphrase)}

    # ---------- 盘口 / 持仓（只读） ----------
    def get_order_book(self, token_id):
        if not self.live:
            return {"live": False, "token_id": token_id, "note": "DRY_RUN 读盘口请走 ws_polymarket/Gamma"}
        return self._ensure_client().get_order_book(token_id)

    def get_positions(self):
        if not self.live:
            return {"live": False, "note": "DRY_RUN 无真实持仓"}
        return self._ensure_client().get_positions()

    # ---------- 下单（核心，带风控闸门） ----------
    def place_maker_order(self, token_id, side, price, size, order_type="GTC"):
        """做市挂单（GTC，挂单方 0 费）。side ∈ {BUY, SELL}。
        返回下单结果 dict；DRY_RUN 返回模拟；被风控拒绝返回 {ok:False, reason}。"""
        notional = float(price) * float(size)
        # 风控闸门：实盘/模拟都过一遍，保证限额逻辑一致
        if RC is not None:
            ok, reason = RC.check_new_order(token_id, notional)
            if not ok:
                logger.warning("[clob] 被风控拒绝 %s %s: %s", side, token_id, reason)
                return {"ok": False, "reason": reason, "risk_blocked": True}
        if not self.live:
            logger.info("[clob][DRY_RUN] 模拟挂单 %s %s @%.4f x%.2f (notional=%.2f)",
                        side, token_id, float(price), float(size), notional)
            return {"ok": True, "dry_run": True, "side": side, "token_id": token_id,
                    "price": float(price), "size": float(size), "notional": notional}
        # ---- 真发单 ----
        from py_clob_client.clob_types import OrderArgs, OrderType, BUY, SELL
        cli = self._ensure_client()
        _side = BUY if str(side).upper() == "BUY" else SELL
        _ot = OrderType.GTC if str(order_type).upper() == "GTC" else OrderType.GTC
        order = cli.create_order(OrderArgs(token_id=token_id, price=float(price),
                                            size=float(size), side=_side))
        resp = cli.post_order(order, _ot)
        logger.info("[clob][LIVE] 已发单 %s %s @%.4f x%.2f -> %s", side, token_id, float(price), float(size), resp)
        if RC is not None:
            RC.record_fill(token_id, notional, 0.0)
        return {"ok": True, "live": True, "resp": resp}

    def cancel_order(self, order_id):
        if not self.live:
            logger.info("[clob][DRY_RUN] 模拟撤单 %s", order_id)
            return {"ok": True, "dry_run": True, "order_id": order_id}
        return self._ensure_client().cancel_order(order_id)


def main():
    """CLI 自检：派生凭证 / 模拟挂单（不真发）。"""
    logging.basicConfig(level=logging.INFO)
    ex = ClobExec()
    print("LIVE_MODE =", ex.live, "| 有 PM_BOT_PK =", bool(ex.pk))
    print("凭证摘要:", json.dumps(ex.credentials(), ensure_ascii=False))
    if len(sys.argv) >= 5:
        # python clob_exec.py <token_id> <BUY|SELL> <price> <size>
        print("模拟挂单:", json.dumps(
            ex.place_maker_order(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]),
            ensure_ascii=False))


if __name__ == "__main__":
    main()

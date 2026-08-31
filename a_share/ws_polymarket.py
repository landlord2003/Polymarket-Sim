# -*- coding: utf-8 -*-
"""P3-2 CLOB WebSocket 实时盘口订阅（实盘做市/套利必需，毫秒级）。

依赖（NB 部署机需安装）：pip install websockets
模块对 websockets 做惰性导入，因此未安装时本文件仍可 import（不污染模拟盘默认运行）。

Polymarket CLOB WebSocket:
  wss://ws-subscriptions-clob.polymarket.com/ws/market
订阅指定 token_id（asset_id）后，推送 book / trade / market / price_change 等消息。

用法:
  from ws_polymarket import ClobWsClient
  def on_book(b): print("BOOK", b["token_id"], "bids", b["bids"][:1])
  cli = ClobWsClient(token_ids=["0x...", "0x..."], on_book=on_book)
  cli.run_forever()   # 阻塞；内部指数退避自动重连
"""
import os
import sys
import time
import json
import asyncio
import logging

logger = logging.getLogger("ws_polymarket")

WS_URI = os.environ.get("CLOB_WS_URI",
                        "wss://ws-subscriptions-clob.polymarket.com/ws/market")
RECONNECT_BASE = float(os.environ.get("WS_RECONNECT_BASE", "2"))   # 退避基数(s)
RECONNECT_MAX = float(os.environ.get("WS_RECONNECT_MAX", "60"))    # 退避上限(s)
PING_TIMEOUT = float(os.environ.get("WS_PING_TIMEOUT", "20"))      # 心跳超时(s)


def _normalize_book(msg):
    """把 WS book 消息归一化为 {token_id, bids:[(price,size)], asks:[(price,size]]}。"""
    asset = msg.get("asset_id") or msg.get("token_id")
    out = {"token_id": asset, "bids": [], "asks": []}
    for side in ("bids", "asks"):
        for lvl in (msg.get(side) or []):
            try:
                p = float(lvl.get("price"))
                s = float(lvl.get("size"))
            except (TypeError, ValueError):
                continue
            out[side].append((p, s))
    out["bids"].sort(key=lambda x: x[0], reverse=True)
    out["asks"].sort(key=lambda x: x[0])
    return out


class ClobWsClient:
    """CLOB WebSocket 订阅客户端（自带指数退避重连）。"""

    def __init__(self, token_ids, on_book=None, on_trade=None, on_market=None,
                 on_price_change=None, uri=WS_URI):
        self.token_ids = list(token_ids)
        self.handlers = {"book": on_book, "trade": on_trade,
                         "market": on_market, "price_change": on_price_change}
        self.uri = uri
        self._stop = False

    def stop(self):
        self._stop = True

    def _dispatch(self, msg):
        t = msg.get("type")
        if t == "book" and self.handlers.get("book"):
            self.handlers["book"](_normalize_book(msg))
        elif t == "trade" and self.handlers.get("trade"):
            self.handlers["trade"](msg)
        elif t == "market" and self.handlers.get("market"):
            self.handlers["market"](msg)
        elif t == "price_change" and self.handlers.get("price_change"):
            self.handlers["price_change"](msg)

    async def _connect_once(self):
        # 惰性导入：仅在真正连接时才需要 websockets（保持模拟盘默认零额外依赖）
        import websockets  # noqa: WPS433 (lazy)
        async with websockets.connect(self.uri, ping_timeout=PING_TIMEOUT,
                                       max_queue=4096) as ws:
            await ws.send(json.dumps({"type": "subscribe", "market": True,
                                       "assets_ids": self.token_ids}))
            logger.info("[ws] 已订阅 %d 个 token", len(self.token_ids))
            async for raw in ws:
                if self._stop:
                    break
                try:
                    self._dispatch(json.loads(raw))
                except Exception as e:  # 单条消息解析失败不影响整体
                    logger.warning("[ws] 消息解析异常: %s", e)

    async def _run(self):
        backoff = RECONNECT_BASE
        while not self._stop:
            try:
                await self._connect_once()
                backoff = RECONNECT_BASE  # 正常断开后重置退避
            except Exception as e:
                if self._stop:
                    break
                logger.warning("[ws] 连接异常 %s，%ss 后重连", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)

    def run_forever(self):
        """阻塞运行（内部指数退避自动重连）。可在独立线程里调用。"""
        try:
            asyncio.run(self._run())
        except KeyboardInterrupt:
            self._stop = True


def run_client(token_ids, on_book=None, on_trade=None, on_market=None,
               on_price_change=None, uri=WS_URI):
    """便捷入口：订阅并阻塞运行。"""
    ClobWsClient(token_ids, on_book=on_book, on_trade=on_trade,
                 on_market=on_market, on_price_change=on_price_change,
                 uri=uri).run_forever()


if __name__ == "__main__":
    # 演示：python ws_polymarket.py 0xTOKEN1 0xTOKEN2
    ids = sys.argv[1:]
    if not ids:
        print("usage: python ws_polymarket.py <token_id1> [token_id2 ...]")
        sys.exit(1)
    logging.basicConfig(level=logging.INFO)
    run_client(ids, on_book=lambda b: print("BOOK", b["token_id"],
                                            "best_bid", b["bids"][:1],
                                            "best_ask", b["asks"][:1]))

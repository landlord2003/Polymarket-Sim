"""加密模拟盘全自动链路（CCXT + 风控闸门，零资金验证技术栈）

闭环：取数(OHLCV) → RSI/EMA 信号 → RiskController 闸门 → testnet/dry-run 下单

两种零资金模式：
  1) dry_run=True（默认）：永不真实下单，只模拟"会下什么单"，验证整条链路。
  2) dry_run=False + testnet=True + 配置 API Key：向 Binance Testnet 下真实测试单
     （用的是测试资金，仍为零真实风险）。

环境变量（读 .env，密钥不入库）：
  CRYPTO_SYMBOL / CRYPTO_TIMEFRAME / CRYPTO_TESTNET / CRYPTO_DRYRUN
  CRYPTO_API_KEY / CRYPTO_API_SECRET（仅 testnet 真下单时需要）
  CRYPTO_PUSH（true/false，是否推送钉钉）

用法：
  python crypto/bot_dryrun.py --once            # 跑一轮（默认离线合成，验证逻辑）
  python crypto/bot_dryrun.py --once --live     # 连 Binance/Testnet 实跑一轮
  python crypto/bot_dryrun.py --loop --interval 60   # 每60秒一轮（Ctrl+C 退出）

注意：境内访问交易所需合规自担；本脚本仅做技术栈验证，不鼓励实盘。
"""

from __future__ import annotations

import os
import sys
import time
import argparse
from datetime import datetime

import numpy as np
import pandas as pd

try:
    import ccxt
except Exception:  # pragma: no cover
    ccxt = None

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from risk.risk_control import RiskController, RiskConfig

# 复用 A股 的钉钉推送（可选）
ASHERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "a_share")
sys.path.insert(0, ASHERE)


def load_config() -> dict:
    return {
        "symbol": os.getenv("CRYPTO_SYMBOL", "BTC/USDT"),
        "timeframe": os.getenv("CRYPTO_TIMEFRAME", "1m"),
        "testnet": os.getenv("CRYPTO_TESTNET", "true").lower() == "true",
        "dry_run": os.getenv("CRYPTO_DRYRUN", "true").lower() == "true",
        "api_key": os.getenv("CRYPTO_API_KEY", ""),
        "api_secret": os.getenv("CRYPTO_API_SECRET", ""),
        "push": os.getenv("CRYPTO_PUSH", "false").lower() == "true",
        "equity": float(os.getenv("CRYPTO_EQUITY", "10000")),
    }


def get_exchange(cfg: dict):
    if ccxt is None:
        return None
    ex = ccxt.binance({
        "enableRateLimit": True,
        "apiKey": cfg["api_key"],
        "secret": cfg["api_secret"],
    })
    if cfg["testnet"]:
        ex.set_sandbox_mode(True)
    return ex


def synth_ohlcv(n: int = 200) -> list:
    """离线合成 K 线：先跌后拉，制造一个买点，用于验证整条链路。"""
    rng = np.random.default_rng(7)
    ts = int(datetime.now().timestamp()) - n * 60_000
    close = np.concatenate([np.linspace(60000, 58000, 140), np.linspace(58000, 62000, 60)])
    close = close + rng.normal(0, 80, n)
    rows = []
    for i in range(n):
        c = close[i]
        o = c + rng.normal(0, 50)
        h = max(o, c) + abs(rng.normal(0, 40))
        l = min(o, c) - abs(rng.normal(0, 40))
        v = rng.integers(1, 5) * 10
        rows.append([ts + i * 60_000, o, h, l, c, v])
    return rows


def fetch_ohlcv(ex, cfg: dict, live: bool, limit: int = 200):
    if not live or ex is None:
        return synth_ohlcv(limit), True
    try:
        return ex.fetch_ohlcv(cfg["symbol"], cfg["timeframe"], limit=limit), False
    except Exception as e:
        print(f"[warn] 取数失败，转离线合成：{type(e).__name__} {str(e)[:80]}")
        return synth_ohlcv(limit), True


def calc_indicators(ohlcvs: list) -> dict:
    df = pd.DataFrame(ohlcvs, columns=["ts", "open", "high", "low", "close", "volume"])
    close = df["close"]
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / (loss + 1e-9)
    rsi = float((100 - 100 / (1 + rs)).iloc[-1])
    ema_fast = float(close.ewm(span=12).mean().iloc[-1])
    ema_slow = float(close.ewm(span=26).mean().iloc[-1])
    last = float(close.iloc[-1])
    return {"rsi": rsi, "ema_fast": ema_fast, "ema_slow": ema_slow, "last": last}


def decide(ind: dict) -> tuple:
    rsi, ef, es = ind["rsi"], ind["ema_fast"], ind["ema_slow"]
    if ef > es and rsi < 70:
        return "buy", f"EMA金叉(快{ef:.0f}>慢{es:.0f}) 且 RSI{rsi:.0f} 未超买"
    if ef < es and rsi > 30:
        return "sell", f"EMA死叉(快{ef:.0f}<慢{es:.0f}) 且 RSI{rsi:.0f} 未超卖"
    return "hold", f"RSI{rsi:.0f} 中性，无交叉"


def execute(cfg: dict, ex, signal: str, ind: dict, rc: RiskController) -> str:
    price = ind["last"]
    ok, reason = rc.gate(signal)
    if not ok:
        return f"🔴 风控拦截：{reason}，跳过下单"
    if signal == "hold":
        return "⚪ 持有，无操作"
    qty = rc.position_size(price, rc.equity or cfg["equity"])
    if signal == "sell":
        qty = -qty
    if cfg["dry_run"]:
        return (f"🟢 [DRY-RUN] 模拟信号 {signal} @ {price:.2f} "
                f"数量 {abs(qty):.6f}（未真实下单）")
    # testnet 真实测试单
    try:
        side = "buy" if qty > 0 else "sell"
        order = ex.create_order(cfg["symbol"], "market", side, abs(qty))
        return (f"✅ [TESTNET] 已下单 {side} {abs(qty):.6f} @ {price:.2f} "
                f"id={order.get('id')}")
    except Exception as e:
        return f"⚠️ 下单失败：{type(e).__name__} {str(e)[:80]}"


def run_once(cfg: dict, live: bool) -> str:
    ex = get_exchange(cfg) if live else None
    ohlcvs, off = fetch_ohlcv(ex, cfg, live)
    ind = calc_indicators(ohlcvs)
    signal, reason = decide(ind)
    rc = RiskController(RiskConfig(max_position_pct=0.20, stop_loss_pct=0.05,
                                   max_drawdown_pct=0.15))
    rc.init_equity(cfg["equity"])
    action = execute(cfg, ex, signal, ind, rc)
    ts = datetime.now().strftime("%H:%M:%S")
    line = (f"[{ts}] {cfg['symbol']} 价={ind['last']:.2f} RSI={ind['rsi']:.1f} "
            f"信号={signal}({reason}) | {action}" + (" [离线合成]" if off else ""))
    return line


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="跑一轮即退出")
    ap.add_argument("--live", action="store_true", help="连 Binance/Testnet 实跑（需网络/密钥）")
    ap.add_argument("--loop", action="store_true", help="持续轮询")
    ap.add_argument("--interval", type=int, default=60, help="轮询间隔秒")
    args = ap.parse_args()

    cfg = load_config()
    print(f"=== 加密模拟盘 dry_run={cfg['dry_run']} testnet={cfg['testnet']} "
          f"symbol={cfg['symbol']} live={args.live} ===")

    def tick():
        line = run_once(cfg, live=args.live)
        print(line)
        if cfg["push"]:
            try:
                from notify import send_markdown
                send_markdown("加密信号", line)
            except Exception as e:
                print(f"[warn] 推送失败：{e}")

    if args.once or not args.loop:
        tick()
        return
    print(f"[loop] 每 {args.interval}s 一轮，Ctrl+C 退出")
    try:
        while True:
            tick()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[stop] 已停止。")


if __name__ == "__main__":
    main()

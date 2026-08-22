"""加密线骨架：CCXT 取数 + RSI/EMA 信号 + testnet 模拟下单（dry-run）

零资金原则：
  - 默认仅拉取公开市场数据 + 计算信号，不落任何真实订单。
  - 接 Binance testnet 模拟盘时，需在环境变量填入 *单独* 的 testnet API key：
        export BINANCE_API_KEY=xxx
        export BINANCE_SECRET=yyy
    （testnet 注册：https://testnet.binance.vision/）

后续路线（P3）：
  - 用 Freqtrade 接管策略/回测/资金管理与 Telegram 控制；或
  - 在本文件基础上接 exchange.create_market_buy_order(...) 的 testnet 版本做全自动。
"""

import os
import sys
import pandas as pd
import ccxt

# 复用 A股 行情模块的加密 K 线（data-api.binance.vision 直连，绕过 ccxt fapi 超时）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "a_share"))
try:
    from datasource import fetch_crypto_kline
except Exception:  # pragma: no cover
    fetch_crypto_kline = None


# 技术指标手写，避免 pandas_ta / numba 的重依赖与 numpy 版本冲突

SYMBOL = "BTC/USDT"
TIMEFRAME = "1m"
LIMIT = 200


def make_exchange(sandbox: bool = True) -> ccxt.binance:
    """sandbox=True 走 Binance testnet 模拟环境（需单独 testnet key）。"""
    return ccxt.binance({
        "apiKey": os.getenv("BINANCE_API_KEY", ""),
        "secret": os.getenv("BINANCE_SECRET", ""),
        "sandbox": sandbox,
        "enableRateLimit": True,
    })


def fetch_ohlcv(exchange: ccxt.binance, symbol: str = SYMBOL,
                timeframe: str = TIMEFRAME, limit: int = LIMIT) -> pd.DataFrame:
    if fetch_crypto_kline is None:
        return pd.DataFrame()
    df = fetch_crypto_kline(symbol, timeframe, limit)
    if df.empty:
        print("[warn] 行情获取失败，返回空 DataFrame")
        return pd.DataFrame()
    return df


def add_signals(df: pd.DataFrame) -> pd.DataFrame:
    # RSI（Wilder 平滑）
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    df["rsi"] = 100 - 100 / (1 + rs)
    # EMA
    df["ema_fast"] = df["close"].ewm(span=12, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=26, adjust=False).mean()
    return df


def get_signal(df: pd.DataFrame) -> str:
    rsi = df["rsi"].iloc[-1]
    if rsi < 30:
        return "buy"
    if rsi > 70:
        return "sell"
    return "hold"


def main() -> None:
    ex = make_exchange(sandbox=True)
    df = fetch_ohlcv(ex)
    df = add_signals(df)
    sig = get_signal(df)
    print(f"[{SYMBOL} {TIMEFRAME}] RSI={df['rsi'].iloc[-1]:.2f} -> signal={sig}")
    print("dry-run: 未下单（零资金）")


if __name__ == "__main__":
    main()

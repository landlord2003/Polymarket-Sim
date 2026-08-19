"""A股四维度信号引擎：行情 / 资金 / 板块 / 消息 → 综合评分 → 信号 → 风控闸门

原则：只出信号，不自动下单。所有评分经 risk_control.RiskController.gate() 闸门。
离线兜底：AkShare 取数失败时该维度返回中性 0 并在备注标注 (离线)，绝不伪造真实信号。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

try:
    import akshare as ak
except Exception:  # pragma: no cover
    ak = None

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# 维度权重（可在调用处覆盖）
DEFAULT_WEIGHTS = {"market": 0.35, "money": 0.30, "sector": 0.20, "news": 0.15}

POS_WORDS = ["利好", "增持", "中标", "获批", "回购", "签约", "增长", "突破", "订单", "扩产", "合作"]
NEG_WORDS = ["利空", "减持", "处罚", "诉讼", "亏损", "下调", "警示", "退市", "问询", "违规", "停产"]


@dataclass
class StockResult:
    symbol: str
    name: str
    offline: bool = False
    market_score: float = 0.0
    money_score: float = 0.0
    sector_score: float = 0.0
    news_score: float = 0.0
    composite: float = 0.0
    signal: str = "观望"
    signal_emoji: str = "🟡"
    notes: list = field(default_factory=list)
    risk_pass: bool = True
    risk_reason: str = "ok"
    last_price: Optional[float] = None


def _market_of(symbol: str) -> str:
    if symbol.startswith("6"):
        return "sh"
    if symbol.startswith(("0", "3")):
        return "sz"
    if symbol.startswith(("8", "4")):
        return "bj"
    return "sh"


def load_price(symbol: str, start: str = "20240101",
               end: Optional[str] = None) -> tuple[pd.DataFrame, bool]:
    """返回 (df, offline)。offline=True 表示使用合成数据，不应据此产生真实信号。"""
    end = end or datetime.today().strftime("%Y%m%d")
    if ak is not None:
        try:
            df = ak.stock_zh_a_hist(symbol=symbol, period="daily",
                                    start_date=start, end_date=end, adjust="qfq")
            df = df.rename(columns={"日期": "date", "开盘": "open", "收盘": "close",
                                    "最高": "high", "最低": "low", "成交量": "volume"})
            df["date"] = pd.to_datetime(df["date"])
            df = df[["date", "open", "high", "low", "close", "volume"]]
            df.set_index("date", inplace=True)
            return df, False
        except Exception:
            pass
    # 离线兜底：随机游走（仅用于验证引擎接线，非真实行情）
    rng = np.random.default_rng(abs(hash(symbol)) % (2**32))
    n = 250
    dates = pd.date_range(end=pd.Timestamp.today().normalize(), periods=n, freq="D")
    close = 25.0 + np.cumsum(rng.normal(0, 0.3, n))
    close = np.maximum(close, 1.0)
    df = pd.DataFrame({
        "open": close + rng.normal(0, 0.1, n),
        "high": close + np.abs(rng.normal(0, 0.1, n)),
        "low": close - np.abs(rng.normal(0, 0.1, n)),
        "close": close,
        "volume": rng.integers(1_000_000, 5_000_000, n),
    }, index=dates)
    return df, True


def dim_market(df: pd.DataFrame) -> tuple[float, list]:
    try:
        close = df["close"]
        if len(close) < 30:
            return 0.0, ["行情数据不足"]
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / (loss + 1e-9)
        rsi = float((100 - 100 / (1 + rs)).iloc[-1])
        ma20 = close.rolling(20).mean()
        mid = close.rolling(20).mean()
        std = close.rolling(20).std()
        lower = mid - 2 * std
        score, notes = 0.0, []
        if rsi < 30:
            score += 0.4; notes.append(f"RSI超卖{rsi:.0f}")
        elif rsi < 45:
            score += 0.2; notes.append(f"RSI偏低{rsi:.0f}")
        elif rsi > 70:
            score -= 0.4; notes.append(f"RSI超买{rsi:.0f}")
        if ma20.iloc[-1] > ma20.iloc[-2] and close.iloc[-1] > ma20.iloc[-1]:
            score += 0.3; notes.append("站上MA20且上行")
        if close.iloc[-1] <= lower.iloc[-1] * 1.02:
            score += 0.3; notes.append("触及布林下轨")
        return max(-1.0, min(1.0, score)), notes
    except Exception as e:
        return 0.0, [f"行情数据缺失:{e}"]


def dim_money(symbol: str) -> tuple[float, list]:
    if ak is None:
        return 0.0, ["资金模块未加载(离线)"]
    try:
        df = ak.stock_individual_fund_flow(stock=symbol, market=_market_of(symbol))
        col = "主力净流入-净额"
        if col not in df.columns:
            return 0.0, ["资金字段缺失"]
        recent = df[col].head(5)
        net = float(recent.sum())
        consec = 0
        for v in recent.tolist():
            if v > 0:
                consec += 1
            else:
                break
        if net > 0 and consec >= 3:
            return 0.6, [f"主力连续{consec}日净流入"]
        if net > 0:
            return 0.3, ["主力净流入"]
        return -0.5, ["主力净流出"]
    except Exception as e:
        return 0.0, [f"资金数据缺失:{e}"]


def dim_sector(stock_df: pd.DataFrame) -> tuple[float, list]:
    if ak is None:
        return 0.0, ["板块模块未加载(离线)"]
    try:
        close = stock_df["close"]
        ret_stock = float(close.iloc[-1] / close.iloc[-20] - 1)
        idx = ak.stock_zh_index_daily(symbol="sh000300")
        ret_mkt = float(idx["close"].iloc[-1] / idx["close"].iloc[-20] - 1)
        diff = ret_stock - ret_mkt
        return max(-1.0, min(1.0, diff * 3)), [
            f"个股20日{ret_stock:.1%} vs 沪深300 {ret_mkt:.1%}"
        ]
    except Exception as e:
        return 0.0, [f"板块数据缺失:{e}"]


def dim_news(symbol: str) -> tuple[float, list]:
    if ak is None:
        return 0.0, ["消息模块未加载(离线)"]
    try:
        news = ak.stock_news_em(symbol=symbol)
        col = "新闻标题" if "新闻标题" in news.columns else news.columns[0]
        titles = news[col].head(10).tolist()
        pos = sum(any(w in str(t) for w in POS_WORDS) for t in titles)
        neg = sum(any(w in str(t) for w in NEG_WORDS) for t in titles)
        score = max(-1.0, min(1.0, (pos - neg) / max(1, len(titles)) * 2))
        return score, [f"近{len(titles)}条新闻 利好{pos}/利空{neg}"]
    except Exception as e:
        return 0.0, [f"消息数据缺失:{e}"]


def _map_signal(composite: float) -> tuple[str, str]:
    if composite >= 0.5:
        return "买入", "🟢"
    if composite >= 0.15:
        return "偏多", "🟢"
    if composite > -0.15:
        return "观望", "🟡"
    if composite > -0.5:
        return "减仓", "🔴"
    return "卖出", "🔴"


def analyze_stock(symbol: str, name: str = "", df: Optional[pd.DataFrame] = None,
                  weights: Optional[dict] = None, risk_gate=None) -> StockResult:
    """对单只股票跑四维度评分。risk_gate 为可选函数 signal->(bool, reason)。"""
    weights = weights or DEFAULT_WEIGHTS
    if df is None:
        df, offline = load_price(symbol)
    else:
        offline = False

    if offline:
        return StockResult(symbol=symbol, name=name, offline=True,
                           notes=["离线合成数据，仅验证引擎，未产生真实信号"])

    sm, n_m = dim_market(df)
    sz, n_z = dim_money(symbol)
    ss, n_s = dim_sector(df)
    sn, n_n = dim_news(symbol)

    composite = (weights["market"] * sm + weights["money"] * sz +
                 weights["sector"] * ss + weights["news"] * sn)
    composite = max(-1.0, min(1.0, composite))
    signal, emoji = _map_signal(composite)

    risk_pass, risk_reason = True, "ok"
    if risk_gate is not None:
        risk_pass, risk_reason = risk_gate(signal)

    res = StockResult(
        symbol=symbol, name=name, offline=False,
        market_score=sm, money_score=sz, sector_score=ss, news_score=sn,
        composite=composite, signal=signal, signal_emoji=emoji,
        notes=n_m + n_z + n_s + n_n,
        risk_pass=risk_pass, risk_reason=risk_reason,
        last_price=float(df["close"].iloc[-1]),
    )
    if not risk_pass:
        res.signal = "暂停"; res.signal_emoji = "🔴"
    return res


if __name__ == "__main__":
    # 离线自测：合成数据验证逻辑接线
    r = analyze_stock("300034", "钢研高纳")
    print(r)

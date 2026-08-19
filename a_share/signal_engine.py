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
               end: Optional[str] = None,
               force_offline: bool = False) -> tuple[pd.DataFrame, bool]:
    """返回 (df, offline)。offline=True 表示使用合成数据，不应据此产生真实信号。
    force_offline=True 时直接走合成兜底、不触网（用于沙箱 / 离线验证）。"""
    end = end or datetime.today().strftime("%Y%m%d")
    if ak is not None and not force_offline:
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


def live_boll_lower(df: pd.DataFrame, window: int = 20, k: float = 2.0) -> Optional[float]:
    """实时布林下轨（与 dim_market 同口径：20日均值 - k*std）。

    用于「动态布林下轨买点」：每次按最新数据计算，避免把历史快照（如钢研早前的18.45）
    当静态阈值，导致价格跌穿时被误判为买点。
    """
    try:
        close = df["close"]
        if len(close) < window:
            return None
        mid = close.rolling(window).mean()
        std = close.rolling(window).std()
        lb = float(mid.iloc[-1] - k * std.iloc[-1])
        return lb if np.isfinite(lb) else None
    except Exception:
        return None


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


def _apply_personal_rules(rules: dict, last_price: float, base_comp: float,
                          base_signal: str, base_notes: list,
                          holding: bool = False,
                          df: Optional[pd.DataFrame] = None) -> tuple:
    """把个股个性化阈值（建仓区间/止损/阻力/动态布林买点）叠加进信号。

    空仓语义（holding=False，当前老吴全空仓）：
      - 止损线 = 趋势破位参考线：跌破则偏弱、不接飞刀、不报「卖出」（没有持仓可卖）。
      - 建仓区间 / 布林下轨 = 回补参考：价格进入才亮「买入」。
    持仓语义（holding=True）：止损线跌破报「卖出」，阻力位触及建议减仓。

    动态布林下轨：use_dynamic_boll=true 时按实时 20日-2σ 计算，避免静态快照（如钢研18.45）
    过时导致跌穿误判为买点。返回 (composite, signal, emoji, notes)。
    """
    notes = list(base_notes)
    comp = base_comp
    forced = None  # (signal, emoji)
    block_buy = False

    # —— 止损 / 趋势破位参考线 ——
    stop_loss = rules.get("stop_loss")
    if stop_loss is not None and last_price <= stop_loss:
        if holding:
            forced = ("卖出", "🔴")
            notes.append(f"触发止损线 {stop_loss}")
        else:
            notes.append(f"跌破趋势参考线 {stop_loss}，暂不强（空仓）")
            comp = min(comp, -0.2)
            block_buy = True
    elif stop_loss is not None:
        notes.append(f"价格高于趋势参考线 {stop_loss}")

    # —— 阻力位（减仓/止盈参考）——
    resistance = rules.get("resistance")
    if resistance is not None and last_price >= resistance:
        notes.append(f"触及阻力位 {resistance}，建议减仓/止盈")
        if holding:
            comp = min(comp, -0.4)

    # —— 建仓区间（回补/建仓参考）——
    buy_range = rules.get("buy_range")
    if isinstance(buy_range, (list, tuple)) and len(buy_range) == 2:
        lo, hi = buy_range
        if lo <= last_price <= hi:
            comp = max(comp, 0.7)
            notes.append(f"进入建仓区间 [{lo}, {hi}]")
            forced = forced or ("买入", "🟢")

    # —— 布林下轨买点（动态优先，静态 boll_lower_buy 兼容兜底）——
    use_dyn = rules.get("use_dynamic_boll", False)
    boll_floor = rules.get("boll_lower_buy")
    lb = live_boll_lower(df) if (use_dyn and df is not None) else None
    if use_dyn and lb is not None:
        tol = rules.get("boll_tol", 0.03)
        near = (last_price >= lb * (1 - tol)) and (last_price <= lb * (1 + tol))
        stable = last_price >= float(df["close"].iloc[-5:].min())
        if near and stable:
            if block_buy:
                notes.append(f"贴近动态下轨 {lb:.2f} 但已跌破破位线，暂缓")
            else:
                comp = max(comp, 0.6)
                notes.append(f"触及动态布林下轨 {lb:.2f} 且企稳")
                forced = forced or ("买入", "🟢")
        elif near and not stable:
            notes.append(f"贴近下轨 {lb:.2f} 但仍在创新低，暂观望")
    elif boll_floor is not None:
        if last_price <= boll_floor * 1.03:
            if block_buy:
                notes.append(f"贴近下轨参考 {boll_floor} 但已跌破破位线，暂缓")
            else:
                comp = max(comp, 0.6)
                notes.append(f"触及布林下轨参考 {boll_floor}")
                forced = forced or ("买入", "🟢")

    cost_avg = rules.get("cost_avg")
    if cost_avg is not None:
        cmp = "低于" if last_price < cost_avg else "高于"
        notes.append(f"现价 {cmp} 参考成本 {cost_avg}")

    comp = max(-1.0, min(1.0, comp))
    if forced:
        return comp, forced[0], forced[1], notes
    sig, emo = _map_signal(comp)
    return comp, sig, emo, notes


def analyze_stock(symbol: str, name: str = "", df: Optional[pd.DataFrame] = None,
                  weights: Optional[dict] = None, risk_gate=None,
                  rules: Optional[dict] = None,
                  holding: bool = False,
                  force_offline: bool = False) -> StockResult:
    """对单只股票跑四维度评分。risk_gate 为可选函数 signal->(bool, reason)。

    force_offline=True 或联网取数失败时，用合成数据「完整跑通全流程」并返回
    带分数/价位/信号的结果（offline=True 仅用于标注与抑制推送），
    不再返回空壳结果——否则看板会一片空白，看着像"没有结果"。
    """
    weights = weights or DEFAULT_WEIGHTS
    if df is None:
        df, offline = load_price(symbol, force_offline=force_offline)
    else:
        offline = False

    sm, n_m = dim_market(df)
    ss, n_s = dim_sector(df)
    if offline:
        # 资金/消息维度需要联网，离线一律跳过并置 0，避免卡在网络等待
        sz, n_z = 0.0, ["资金维度离线跳过"]
        sn, n_n = 0.0, ["消息维度离线跳过"]
    else:
        sz, n_z = dim_money(symbol)
        sn, n_n = dim_news(symbol)

    composite = (weights["market"] * sm + weights["money"] * sz +
                 weights["sector"] * ss + weights["news"] * sn)
    composite = max(-1.0, min(1.0, composite))
    signal, emoji = _map_signal(composite)

    last_price = float(df["close"].iloc[-1])

    # —— 个性化规则叠加（动态布林下轨、元力建仓区间等）——
    if rules:
        composite, signal, emoji, pnotes = _apply_personal_rules(
            rules, last_price, composite, signal, n_m + n_z + n_s + n_n,
            holding=holding, df=df)
    else:
        pnotes = n_m + n_z + n_s + n_n

    risk_pass, risk_reason = True, "ok"
    if risk_gate is not None:
        risk_pass, risk_reason = risk_gate(signal)

    if offline:
        pnotes = ["⚠️ 合成数据（未联网），价位与信号不可据此下单"] + pnotes

    res = StockResult(
        symbol=symbol, name=name, offline=offline,
        market_score=sm, money_score=sz, sector_score=ss, news_score=sn,
        composite=composite, signal=signal, signal_emoji=emoji,
        notes=pnotes,
        risk_pass=risk_pass, risk_reason=risk_reason,
        last_price=last_price,
    )
    if not risk_pass:
        res.signal = "暂停"; res.signal_emoji = "🔴"
    return res


if __name__ == "__main__":
    # 离线自测：合成数据验证逻辑接线
    r = analyze_stock("300034", "钢研高纳")
    print(r)

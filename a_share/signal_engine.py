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

# 多源直连数据层（绕过 akshare 的限流/兼容问题，见 datasource.py 顶部说明）
try:
    from . import datasource as ds  # type: ignore
except ImportError:  # 直接以脚本方式运行时
    import datasource as ds  # type: ignore

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
    source: str = ""            # 真实数据来源，如「腾讯财经(前复权)」/「合成随机游走」
    data_date: str = ""         # 最新K线日期，用于判断数据是否新鲜
    pct_change: Optional[float] = None   # 当日涨跌幅 %


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

    force_offline=True 时直接走合成兜底、不触网（用于「离线验证」跑通链路）。

    取数链路（2026-08-19 重构）：datasource.fetch_kline 多源兜底
    腾讯前复权 → 东财前复权 → 新浪不复权，各源带 3 次重试。
    真实来源写入 df.attrs['source']；若全部失败，失败原因写入
    df.attrs['fallback_reason'] 并回退合成，**绝不静默伪装成真实行情**。
    """
    if not force_offline:
        try:
            df = ds.fetch_kline(symbol)
            if start:
                try:
                    df = df[df.index >= pd.to_datetime(start)]
                except Exception:  # noqa: BLE001
                    pass
            if end:
                try:
                    df = df[df.index <= pd.to_datetime(end)]
                except Exception:  # noqa: BLE001
                    pass
            return df, False
        except Exception as e:  # noqa: BLE001
            fallback_reason = str(e)
    else:
        fallback_reason = "force_offline（离线验证模式，主动不联网）"

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
    df.attrs["source"] = "合成随机游走"
    df.attrs["synthetic"] = True
    df.attrs["fallback_reason"] = fallback_reason
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


def money_proxy(df: pd.DataFrame, window: int = 5) -> tuple[float, list]:
    """资金强度代理指标（纯本地计算，无需联网，永不缺失）。

    东财资金流接口对同一 IP 有间歇性限流，取不到时用量价关系代理：
    近 window 日「上涨日成交额」与「下跌日成交额」的净差 / 总成交额，
    直观表达「钱在往里进还是往外出」，再叠加 MFI(14) 超买超卖修正。
    """
    try:
        if len(df) < 20:
            return 0.0, ["资金代理：数据不足"]
        close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
        amount = ((high + low + close) / 3) * vol       # 近似成交额
        chg = close.diff()
        recent_amt = amount.iloc[-window:]
        recent_chg = chg.iloc[-window:]
        up_amt = float(recent_amt[recent_chg > 0].sum())
        dn_amt = float(recent_amt[recent_chg < 0].sum())
        total = up_amt + dn_amt
        net_ratio = (up_amt - dn_amt) / total if total > 0 else 0.0

        # MFI(14)
        tp = (high + low + close) / 3
        mf = tp * vol
        tp_diff = tp.diff()
        pos_mf = mf.where(tp_diff > 0, 0.0).rolling(14).sum()
        neg_mf = mf.where(tp_diff < 0, 0.0).rolling(14).sum()
        mfi = float((100 - 100 / (1 + pos_mf / (neg_mf + 1e-9))).iloc[-1])

        score = net_ratio * 0.8
        notes = [f"资金代理：近{window}日量价净比 {net_ratio:+.0%}，MFI {mfi:.0f}"]
        if mfi < 20:
            score += 0.3; notes.append("MFI超卖(抛压衰竭)")
        elif mfi > 80:
            score -= 0.3; notes.append("MFI超买(追高风险)")
        return max(-1.0, min(1.0, score)), notes
    except Exception as e:  # noqa: BLE001
        return 0.0, [f"资金代理计算失败:{type(e).__name__}"]


def dim_money(symbol: str, df: Optional[pd.DataFrame] = None) -> tuple[float, list]:
    """资金维度：优先东财真实主力净流入，失败降级为本地量价代理指标。

    降级后仍给出有效评分，并在备注明确标注「代理」，避免出现
    「资金数据缺失」这种既没分数也没解释的黑洞。
    """
    try:
        flows = ds.fetch_money_flow(symbol, limit=10)
        recent = list(reversed(flows[-5:]))          # 最新在前
        net = sum(recent)
        consec = 0
        for v in recent:
            if v > 0:
                consec += 1
            else:
                break
        wan = net / 1e4
        if net > 0 and consec >= 3:
            return 0.6, [f"主力连续{consec}日净流入(近5日{wan:+.0f}万)"]
        if net > 0:
            return 0.3, [f"主力净流入(近5日{wan:+.0f}万)"]
        return -0.5, [f"主力净流出(近5日{wan:+.0f}万)"]
    except Exception:  # noqa: BLE001
        if df is not None:
            score, notes = money_proxy(df)
            return score, notes + ["(东财资金接口限流，已用本地代理)"]
        return 0.0, ["资金数据不可用(接口限流且无K线可代理)"]


def dim_sector(stock_df: pd.DataFrame, offline: bool = False) -> tuple[float, list]:
    """板块/相对强弱维度：个股20日涨幅 vs 沪深300（腾讯指数源）。

    离线模式下不联网，降级为「个股自身20日动量」，并明确标注。
    """
    try:
        close = stock_df["close"]
        if len(close) < 21:
            return 0.0, ["板块：K线不足20日"]
        ret_stock = float(close.iloc[-1] / close.iloc[-20] - 1)
        if offline:
            return max(-1.0, min(1.0, ret_stock * 3)), [
                f"个股20日动量{ret_stock:+.1%}（离线，无大盘对比）"]
        idx = ds.fetch_index_kline("sh000300", days=60)
        ret_mkt = float(idx["close"].iloc[-1] / idx["close"].iloc[-20] - 1)
        diff = ret_stock - ret_mkt
        tag = "跑赢" if diff > 0 else "跑输"
        return max(-1.0, min(1.0, diff * 3)), [
            f"20日{ret_stock:+.1%} vs 沪深300 {ret_mkt:+.1%}（{tag}{abs(diff):.1%}）"
        ]
    except Exception as e:  # noqa: BLE001
        try:
            close = stock_df["close"]
            ret_stock = float(close.iloc[-1] / close.iloc[-20] - 1)
            return max(-1.0, min(1.0, ret_stock * 3)), [
                f"个股20日动量{ret_stock:+.1%}（指数源失败:{type(e).__name__}）"]
        except Exception:  # noqa: BLE001
            return 0.0, [f"板块数据不可用:{type(e).__name__}"]


def dim_news(symbol: str, offline: bool = False) -> tuple[float, list]:
    """消息维度：东财新闻标题关键词情绪。离线或接口失败时置中性并说明原因。"""
    if offline:
        return 0.0, ["消息面：离线模式跳过（中性0分）"]
    try:
        titles = ds.fetch_news_titles(symbol, limit=10)
        pos = sum(any(w in str(t) for w in POS_WORDS) for t in titles)
        neg = sum(any(w in str(t) for w in NEG_WORDS) for t in titles)
        score = max(-1.0, min(1.0, (pos - neg) / max(1, len(titles)) * 2))
        return score, [f"近{len(titles)}条新闻 利好{pos}/利空{neg}"]
    except Exception as e:  # noqa: BLE001
        return 0.0, [f"消息面不可用({type(e).__name__})，按中性0分计"]


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
    ss, n_s = dim_sector(df, offline=offline)
    if offline:
        # 离线不触网：资金维度改用本地量价代理（仍有分数），消息维度置中性
        sz, n_z = money_proxy(df)
        n_z = [n + "（离线代理）" for n in n_z]
        sn, n_n = dim_news(symbol, offline=True)
    else:
        sz, n_z = dim_money(symbol, df=df)
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

    source = str(df.attrs.get("source", "未知来源"))
    data_date = ""
    try:
        data_date = pd.Timestamp(df.index[-1]).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        pass
    pct_change = None
    try:
        if len(df) >= 2:
            prev = float(df["close"].iloc[-2])
            if prev > 0:
                pct_change = (last_price / prev - 1) * 100
    except Exception:  # noqa: BLE001
        pass

    if offline:
        reason = df.attrs.get("fallback_reason", "")
        head = "⚠️ 合成数据（未联网），价位与信号不可据此下单"
        if reason and "force_offline" not in str(reason):
            head += f"｜回退原因：{reason}"
        pnotes = [head] + pnotes
    else:
        pnotes = [f"✅ 真实行情｜来源 {source}｜数据日 {data_date}"] + pnotes

    res = StockResult(
        symbol=symbol, name=name, offline=offline,
        market_score=sm, money_score=sz, sector_score=ss, news_score=sn,
        composite=composite, signal=signal, signal_emoji=emoji,
        notes=pnotes,
        risk_pass=risk_pass, risk_reason=risk_reason,
        last_price=last_price,
        source=source, data_date=data_date, pct_change=pct_change,
    )
    if not risk_pass:
        res.signal = "暂停"; res.signal_emoji = "🔴"
    return res


if __name__ == "__main__":
    # 离线自测：合成数据验证逻辑接线
    r = analyze_stock("300034", "钢研高纳")
    print(r)

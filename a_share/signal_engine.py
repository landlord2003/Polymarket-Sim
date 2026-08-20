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

# 阶段1 免费多源因子（AkShare + 本地/指数计算，见 akshare_factors.py）
try:
    from . import akshare_factors as af  # type: ignore
except ImportError:  # 直接以脚本方式运行时
    import akshare_factors as af  # type: ignore

import os as _os
import json as _json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

HERE = _os.path.dirname(_os.path.abspath(__file__))
MONEYFLOW_CACHE_DIR = _os.path.join(HERE, "data", "moneyflow")

# 维度权重（阶段1：五因子 + 新闻；regime 作为整体乘数单独施加）
# trend=多周期动量+RS+RSI/MA/Boll；money=真实主力净流入；
# rotation=板块/风格轮动；valuation=估值分位；news=新闻情绪。
DEFAULT_WEIGHTS = {
    "trend": 0.28,
    "money": 0.24,
    "rotation": 0.18,
    "valuation": 0.15,
    "news": 0.15,
}

POS_WORDS = ["利好", "增持", "中标", "获批", "回购", "签约", "增长", "突破", "订单", "扩产", "合作"]
NEG_WORDS = ["利空", "减持", "处罚", "诉讼", "亏损", "下调", "警示", "退市", "问询", "违规", "停产"]


@dataclass
class StockResult:
    symbol: str
    name: str
    offline: bool = False
    market_score: float = 0.0       # 趋势：多周期动量+RS+RSI/MA/Boll
    money_score: float = 0.0        # 真实主力净流入
    sector_score: float = 0.0       # 板块/风格轮动
    valuation_score: float = 0.0    # 估值分位（价格代理）
    news_score: float = 0.0         # 新闻情绪
    regime_score: float = 0.0       # 大盘状态（趋势+宽度），用作整体乘数
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
    as_of: str = ""                       # 信号计算时间（每日持久化，盘中不重算买卖）
    intraday_alert: str = ""              # 盘中实时价相对决策带的提示（仅信息，不改信号）


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
               force_offline: bool = False,
               fast: bool = False) -> tuple[pd.DataFrame, bool]:
    """返回 (df, offline)。offline=True 表示使用合成数据，不应据此产生真实信号。

    force_offline=True 时直接走合成兜底、不触网（用于「离线验证」跑通链路）。
    fast=True 时交给 datasource.fetch_kline(fast=True)：缩短超时与重试，
    用于板块初筛这类「宁可丢几只也要快」的场景；日常盯盘保持 fast=False。

    取数链路（2026-08-19 重构）：datasource.fetch_kline 多源兜底
    腾讯前复权 → 东财前复权 → 新浪不复权，各源带 3 次重试（fast 模式 1 次）。
    真实来源写入 df.attrs['source']；若全部失败，失败原因写入
    df.attrs['fallback_reason'] 并回退合成，**绝不静默伪装成真实行情**。
    """
    if not force_offline:
        try:
            df = ds.fetch_kline(symbol, fast=fast)
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


_IDX300_CACHE = None  # 沪深300指数K线缓存（回测中只抓一次，避免逐行联网）


def _get_idx300(days: int = 60):
    """一次性抓沪深300指数K线并缓存；失败只试一次（置 False 哨兵），杜绝逐行重复联网。"""
    global _IDX300_CACHE
    if _IDX300_CACHE is not None:
        return _IDX300_CACHE if _IDX300_CACHE is not False else None
    try:
        _IDX300_CACHE = ds.fetch_index_kline("sh000300", days=days)
    except Exception:  # noqa: BLE001
        _IDX300_CACHE = False
    return _IDX300_CACHE if _IDX300_CACHE is not False else None


def dim_trend(df: pd.DataFrame, idx_df: Optional[pd.DataFrame] = None) -> tuple[float, list]:
    """趋势维度（阶段1 增强）：RSI/MA20/布林 + 多周期动量(5/20/60) + RS(对沪深300)。

    多周期动量捕捉不同持仓周期的趋势强度，RS 表达相对大盘强弱，
    二者是此前缺失的「非价量派生」信息源（RSI/MA/布林仍同源价量，但动量/RS补了维度）。
    idx_df 为预取的沪深300历史（回测只取一次，避免每日重复联网）。
    """
    try:
        close = df["close"]
        if len(close) < 60:
            return 0.0, ["趋势：K线不足60日"]
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

        # —— RSI/MA/布林（原行情逻辑）——
        if rsi < 30:
            score += 0.25; notes.append(f"RSI超卖{rsi:.0f}")
        elif rsi < 45:
            score += 0.12; notes.append(f"RSI偏低{rsi:.0f}")
        elif rsi > 70:
            score -= 0.25; notes.append(f"RSI超买{rsi:.0f}")
        if ma20.iloc[-1] > ma20.iloc[-2] and close.iloc[-1] > ma20.iloc[-1]:
            score += 0.2; notes.append("站上MA20且上行")
        if close.iloc[-1] <= lower.iloc[-1] * 1.02:
            score += 0.2; notes.append("触及布林下轨")

        # —— 多周期动量（5/20/60日）——
        mom5 = float(close.iloc[-1] / close.iloc[-6] - 1)
        mom20 = float(close.iloc[-1] / close.iloc[-21] - 1)
        mom60 = float(close.iloc[-1] / close.iloc[-61] - 1)
        # 短期动量给更高权重（更灵敏），长期动量确认趋势
        mom_score = (np.clip(mom5 / 0.08, -1, 1) * 0.5
                     + np.clip(mom20 / 0.15, -1, 1) * 0.35
                     + np.clip(mom60 / 0.30, -1, 1) * 0.15)
        score += 0.35 * mom_score
        notes.append(f"动量 5/20/60日 {mom5:+.1%}/{mom20:+.1%}/{mom60:+.1%}")

        # —— RS 相对强度（对沪深300）——
        try:
            idx = idx_df if idx_df is not None else _get_idx300(60)
            if idx is None or len(idx) < 20:
                notes.append("RS：沪深300源不可用")
            else:
                ret_mkt = float(idx["close"].iloc[-1] / idx["close"].iloc[-20] - 1)
                rs_score = np.clip((mom20 - ret_mkt) / 0.10, -1, 1)
                score += 0.15 * rs_score
                notes.append(f"RS(对沪深300) {mom20 - ret_mkt:+.1%}")
        except Exception:  # noqa: BLE001
            notes.append("RS：沪深300源失败")

        return max(-1.0, min(1.0, score)), notes
    except Exception as e:  # noqa: BLE001
        return 0.0, [f"趋势数据缺失:{type(e).__name__}"]


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


def money_score_from_inflow(series) -> float:
    """把「日度主力净流入(元)序列」映射成连续资金强度分 [-1,1]。

    用 5 日累计净额相对其自身 60 日分布的 z 分数（尺度无关，不同市值股可比较）。
    仅供回测/特征使用；series 必须已切片到 target_date 当日及之前（无未来函数）。
    """
    s = pd.Series(series).dropna()
    if len(s) < 6:
        return 0.0
    daily = s.iloc[-60:] if len(s) >= 60 else s
    mean_d = float(daily.mean())
    std_d = float(daily.std())
    if std_d < 1e-9:
        recent = float(s.iloc[-5:].sum())
        return max(-0.3, min(0.3, (recent / (abs(recent) + 1e-9)) * 0.3))
    recent5 = float(s.iloc[-5:].sum())
    base5 = 5.0 * mean_d
    z = (recent5 - base5) / (std_d * (5.0 ** 0.5) + 1e-9)
    return max(-1.0, min(1.0, z / 3.0))


def proxy_inflow_series(df: pd.DataFrame) -> pd.Series:
    """从 K 线构造「日度伪主力净流入(元)」序列（与 money_proxy 同源逻辑）：

    上涨日净额≈当日成交额(正)，下跌日≈负，平盘≈0。用于回测 proxy 模式，
    与真实资金流走同一套 money_score_from_inflow 打分，保证 A/B 口径一致。
    """
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
    tp = (high + low + close) / 3.0
    amount = tp * vol
    chg = close.diff().fillna(0.0)
    net = np.where(chg > 0, amount.values,
                   np.where(chg < 0, -amount.values, 0.0))
    return pd.Series(net, index=df.index)


def load_moneyflow_cache(symbol: str) -> Optional[pd.Series]:
    """读取 Tushare 抓取的资金流缓存 CSV（data/moneyflow/<symbol>.csv）。

    返回 日度主力净流入(元) 的 Series（index=datetime，已排序）。无 tushare 依赖。
    文件不存在返回 None。
    """
    p = _os.path.join(MONEYFLOW_CACHE_DIR, f"{symbol}.csv")
    if not _os.path.exists(p):
        return None
    try:
        d = pd.read_csv(p)
        d["trade_date"] = d["trade_date"].astype(str)
        idx = pd.to_datetime(d["trade_date"], format="%Y%m%d", errors="coerce")
        mask = ~idx.isna()
        vals = d["main_net_in"].astype(float).values[mask]
        out_idx = idx[mask]
        s = pd.Series(vals, index=out_idx).sort_index()
        return s
    except Exception:  # noqa: BLE001
        return None


# ----------------------------------------------------------- 精细资金流特征（真实订单档位）
def load_moneyflow_full(symbol: str) -> Optional[pd.DataFrame]:
    """读取 Tushare 抓取的资金流全字段缓存 CSV → DataFrame（index=datetime，已排序）。

    含 trade_date / main_net_in / 各档位买额卖额(元) / 各档位买量卖量(手)。
    向后兼容：若缓存只有 main_net_in（旧版），仍返回该 df。无文件返回 None。
    """
    p = _os.path.join(MONEYFLOW_CACHE_DIR, f"{symbol}.csv")
    if not _os.path.exists(p):
        return None
    try:
        d = pd.read_csv(p)
        d["trade_date"] = d["trade_date"].astype(str)
        idx = pd.to_datetime(d["trade_date"], format="%Y%m%d", errors="coerce")
        na = idx.isna().values            # numpy 布尔数组，规避 pandas3.0 Series 索引对齐坑
        keep = ~na
        d = d.loc[keep].copy()
        d.index = idx[keep]
        return d.sort_index()
    except Exception:  # noqa: BLE001
        return None


def _ols_slope_norm(s) -> float:
    """对序列 s 做 OLS 斜率并归一化到 ~[-1,1]（跨标的可比）。点-时间安全。"""
    s = np.asarray(pd.Series(s).dropna(), dtype=float)
    n = len(s)
    if n < 6:
        return 0.0
    x = np.arange(n, dtype=float)
    xm, ym = x.mean(), s.mean()
    denom = float(((x - xm) ** 2).sum())
    if denom < 1e-12:
        return 0.0
    slope = float(((x - xm) * (s - ym)).sum()) / denom
    sy = float(s.std())
    if sy < 1e-9:
        return 0.0
    norm = slope * n / (sy + 1e-9)
    return float(max(-1.0, min(1.0, norm)))


def _sum_z(s, win: int = 5, hist: int = 60) -> float:
    """序列 s 最近 win 日累计相对其自身 hist 日分布的 z 分数（尺度无关）。"""
    s = np.asarray(pd.Series(s).dropna(), dtype=float)
    if len(s) < win + 1:
        return 0.0
    trail = s[-hist:] if len(s) >= hist else s
    mean, std = float(trail.mean()), float(trail.std())
    if std < 1e-9:
        return 0.0
    return float(max(-1.0, min(1.0,
                   (s[-win:].sum() - win * mean) / (std * (win ** 0.5) + 1e-9))))


def _mfi_series(close, high, low, vol, win: int = 14) -> pd.Series:
    """Money Flow Index（量价加权 RSI），返回与输入等长序列。滚动窗口因果、点-时间安全。"""
    close = pd.Series(close, dtype=float)
    high = pd.Series(high, dtype=float)
    low = pd.Series(low, dtype=float)
    vol = pd.Series(vol, dtype=float)
    tp = (high + low + close) / 3.0
    raw = tp * vol
    td = tp.diff().fillna(0.0)
    pos = raw.where(td > 0, 0.0).rolling(win).sum()
    neg = raw.where(td < 0, 0.0).rolling(win).sum()
    mfi = 100.0 - 100.0 / (1.0 + pos / (neg + 1e-9))
    return mfi


def _adi_series(close, high, low, vol) -> pd.Series:
    """Chaikin Accumulation/Distribution 线（累计），返回等长序列。"""
    close = np.asarray(close, dtype=float)
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    vol = np.asarray(vol, dtype=float)
    denom = (high - low)
    safe = np.where(denom > 0, denom, 1.0)
    frac = np.where(denom > 0, ((close - low) - (high - close)) / safe, 0.0)
    mf = frac.astype(float) * vol.astype(float)
    return pd.Series(mf).cumsum()


def refined_money_block(pre: dict, i: int, has_real: bool) -> list:
    """返回 10 维精细资金流特征（在 point i，即 sub=data[:i+1] 处）。

    pre 为在 build_rows 中预计算的全长序列字典（已对齐 df.index）：
      'mfi','adi'：由 K 线算（两种模式都有）；
      'elg_net','lg_net','md_net','sm_net','main_net'：真实订单净额（元），proxy 模式为 None；
      'price_div'：价格-资金背离编码（全长）。
    has_real=False（proxy）时，订单档位相关项置 0，仅保留 K 线可算的 MFI/ADI，
      从而干净隔离「真实订单信息」的增量贡献。
    """
    out = [0.0] * 10
    # MFI / ADI：始终由 K 线计算（两种模式一致，不引入混杂）
    try:
        mfi_v = pre["mfi"].iloc[i]
        if np.isfinite(mfi_v):
            out[7] = float(max(-1.0, min(1.0, (mfi_v - 50.0) / 50.0)))
    except Exception:
        pass
    try:
        out[8] = _ols_slope_norm(pre["adi"].iloc[: i + 1])
    except Exception:
        pass
    if not has_real:
        return out

    try:
        elg = pre["elg_net"].iloc[: i + 1]
        lg = pre["lg_net"].iloc[: i + 1]
        md = pre["md_net"].iloc[: i + 1]
        sm = pre["sm_net"].iloc[: i + 1]
        main = pre["main_net"].iloc[: i + 1]
        out[0] = _sum_z(main, 5, 60)                 # 主力净流入 z（近5日累计 vs 60日分布）
        out[1] = _ols_slope_norm(elg.tail(10))       # 特大单净流入趋势（机构意图）
        out[2] = _ols_slope_norm(lg.tail(10))        # 大单净流入趋势
        out[3] = _sum_z(elg - lg, 5, 60)             # 特大单−大单 背离（大资金是否一致）
        out[4] = _sum_z(main - sm, 5, 60)            # 机构−散户 分化（主力净 − 小单净）
        # 主力强度：近5日 |主力净| / 近5日 四档位总成交额
        tot = (pre["elg_net"].abs().iloc[: i + 1] + pre["lg_net"].abs().iloc[: i + 1]
               + pre["md_net"].abs().iloc[: i + 1] + pre["sm_net"].abs().iloc[: i + 1])
        denom_t = float(tot.iloc[-5:].sum())
        if denom_t > 0:
            out[5] = float(min(1.0, main.iloc[-5:].abs().sum() / denom_t))
        # 连续净流入天数（截止 i），封顶 10
        c = 0
        for v in reversed(main.values):
            if v > 0:
                c += 1
            else:
                break
        out[6] = float(min(c, 10) / 10.0)
        # 价格-资金背离：价跌钱进=吸筹(+0.5)，价涨钱出=派发(-0.5)，同向放大±1
        try:
            out[9] = float(pre["price_div"].iloc[i])
        except Exception:
            out[9] = 0.0
    except Exception:
        pass
    return out


def dim_money(symbol: str, df: Optional[pd.DataFrame] = None,
              inflow_series: Optional[list] = None) -> tuple[float, list]:
    """资金维度（阶段1）：真实主力净流入优先，失败回退本地量价代理。

    inflow_series 由调用方预取（回测只取一次避免重复触网）；为 None 时
    实时尝试 af.fetch_main_inflow（AkShare→东财），再失败用 money_proxy。
    """
    # 路径0：Tushare 抓取缓存（真实主力净流入，无需 tushare 运行时，无未来函数）
    cache = load_moneyflow_cache(symbol)
    if cache is not None and len(cache) >= 6:
        return money_score_from_inflow(cache), ["主力净流入(真实·Tushare缓存)"]
    flows = inflow_series
    src = "预取序列"
    if flows is None:
        flows, src = af.fetch_main_inflow(symbol, limit=10)
    if flows:
        recent = list(reversed(flows[-5:]))          # 最新在前
        net = sum(recent)
        consec = 0
        for v in recent:
            if v > 0:
                consec += 1
            else:
                break
        wan = net / 1e4
        tag = f"({src})"
        if net > 0 and consec >= 3:
            return 0.6, [f"主力连续{consec}日净流入{tag}(近5日{wan:+.0f}万)"]
        if net > 0:
            return 0.3, [f"主力净流入{tag}(近5日{wan:+.0f}万)"]
        return -0.5, [f"主力净流出{tag}(近5日{wan:+.0f}万)"]
    # 真实源全失败 → 本地代理
    if df is not None:
        score, notes = money_proxy(df)
        return score, notes + ["(真实资金接口限流，已用本地量价代理)"]
    return 0.0, ["资金数据不可用(接口限流且无K线可代理)"]


def dim_valuation(symbol: str, df: pd.DataFrame,
                  valuation_pct: Optional[float] = None) -> tuple[float, list]:
    """估值维度（阶段1）：估值分位越低越偏多。

    valuation_pct 由调用方预取（回测只算一次）；为 None 时实时计算。
    返回 (score∈[-1,1], notes)。
    """
    pct, src = (valuation_pct, "预取")
    if pct is None:
        pct, src = af.fetch_valuation_percentile(symbol, df)
    if pct is None:
        return 0.0, [f"估值不可用({src})"]
    # 分位越低越便宜→越偏多：0分位 +0.8，1分位 -0.8
    score = float(np.clip(0.5 - pct, -1.0, 1.0) * 1.6)
    notes = [f"估值分位 {pct:.0%} {src}（低=便宜→偏多）"]
    return score, notes


def dim_sector_rotation(symbol: str, df: pd.DataFrame,
                        bench_hist: Optional[dict] = None,
                        target_date=None) -> tuple[float, list]:
    """板块/风格轮动维度（阶段1）：个股20日动量 − 多基准20日动量均值。

    bench_hist 为预取的指数历史字典；target_date 为该日日期（walk-forward 截止日）。
    返回 (score∈[-1,1], notes)。
    """
    if bench_hist is None or target_date is None:
        try:
            bench_hist = af.fetch_benchmark_histories()
            target_date = df.index[-1]
        except Exception:  # noqa: BLE001
            return 0.0, ["轮动：基准获取失败"]
    if not bench_hist:
        return 0.0, ["轮动：无可用基准"]
    return af.sector_rotation_score(df, bench_hist, target_date)


def dim_regime(bench_hist: Optional[dict] = None,
               target_date=None,
               breadth: Optional[tuple] = None) -> tuple[float, list]:
    """大盘状态维度（阶段1）：沪深300 60日趋势 (+可选市场宽度) 作 regime 乘数。

    返回 (regime_score∈[-1,1], notes)。
    """
    if bench_hist is None or target_date is None:
        try:
            bench_hist = af.fetch_benchmark_histories()
            target_date = pd.Timestamp.today().normalize()
        except Exception:  # noqa: BLE001
            return 0.0, ["regime：基准获取失败"]
    if not bench_hist:
        return 0.0, ["regime：无可用基准"]
    return af.market_regime(bench_hist, target_date, breadth)


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


def _map_signal_hyst(comp: float, prev: Optional[str] = None) -> tuple[str, str]:
    """带迟滞死区的信号映射，避免综合分在边界附近抖动导致买卖秒翻。

    - 买入/偏多 要跌破 0.30 才降级（普通映射阈值 0.15）；
    - 观望 要冲上 0.55 才升级为买入（普通 0.50）；
    - 减仓/卖出 要回升到 -0.30 以上才脱离。
    prev=None 时退化为普通映射。
    """
    base = _map_signal(comp)
    if prev is None:
        return base
    if prev in ("买入", "偏多"):
        if comp >= 0.5:
            return "买入", "🟢"
        if comp >= 0.30:
            return "偏多", "🟢"
        return base
    if prev in ("减仓", "卖出", "暂停"):
        if comp <= -0.5:
            return "卖出", "🔴"
        if comp <= -0.30:
            return "减仓", "🔴"
        return base
    # prev == 观望
    if comp >= 0.55:
        return ("买入", "🟢") if comp >= 0.5 else ("偏多", "🟢")
    if comp <= -0.55:
        return ("减仓", "🔴") if comp > -0.5 else ("卖出", "🔴")
    return "观望", "🟡"



def _apply_personal_rules(rules: dict, decision_price: float, base_comp: float,
                          base_signal: str, base_notes: list,
                          holding: bool = False,
                          df: Optional[pd.DataFrame] = None,
                          live_price: Optional[float] = None,
                          prev_signal: Optional[str] = None) -> tuple:
    """把个股个性化阈值（建仓区间/止损/阻力/动态布林买点）叠加进信号。

    ⚠️ 关键修复：决策一律用 **decision_price（昨收/最近完成交易日收盘）**，
    不再用盘中实时价。盘中 tick 穿越 buy_range 窄带 / 触碰布林下轨，
    只产生 `intraday_alert` 提示，**不改变信号**——根治「上午买入、下午观望」的秒翻。

    空仓语义（holding=False，当前老吴全空仓）：
      - 止损线 = 趋势破位参考线：跌破则偏弱、不接飞刀、不报「卖出」（没有持仓可卖）。
      - 建仓区间 / 布林下轨 = 回补参考：决策价进入才亮「买入」。
    持仓语义（holding=True）：止损线跌破报「卖出」，阻力位触及建议减仓。

    动态布林下轨：use_dynamic_boll=true 时按「已完成交易日」序列的 20日-2σ 计算
    （df.iloc[:-1]），决策价用昨收，避免盘中价穿越窄带误判。
    返回 (composite, signal, emoji, notes)。
    """
    notes = list(base_notes)
    comp = base_comp
    forced = None  # (signal, emoji)
    block_buy = False

    # —— 止损 / 趋势破位参考线（决策价=昨收）——
    stop_loss = rules.get("stop_loss")
    if stop_loss is not None and decision_price <= stop_loss:
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
    if resistance is not None and decision_price >= resistance:
        notes.append(f"触及阻力位 {resistance}，建议减仓/止盈")
        if holding:
            comp = min(comp, -0.4)

    # —— 建仓区间（回补/建仓参考）——
    buy_range = rules.get("buy_range")
    if isinstance(buy_range, (list, tuple)) and len(buy_range) == 2:
        lo, hi = buy_range
        if lo <= decision_price <= hi:
            comp = max(comp, 0.7)
            notes.append(f"进入建仓区间 [{lo}, {hi}]")
            forced = forced or ("买入", "🟢")

    # —— 布林下轨买点（动态优先，静态 boll_lower_buy 兼容兜底）——
    use_dyn = rules.get("use_dynamic_boll", False)
    boll_floor = rules.get("boll_lower_buy")
    # 用「已完成交易日」序列算下轨，决策价用昨收，避免盘中价穿越窄带秒翻
    boll_df = df.iloc[:-1] if (df is not None and len(df) >= 2) else df
    lb = live_boll_lower(boll_df) if (use_dyn and boll_df is not None) else None
    if use_dyn and lb is not None:
        tol = rules.get("boll_tol", 0.03)
        near = (decision_price >= lb * (1 - tol)) and (decision_price <= lb * (1 + tol))
        stable = decision_price >= float(boll_df["close"].iloc[-5:].min())
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
        if decision_price <= boll_floor * 1.03:
            if block_buy:
                notes.append(f"贴近下轨参考 {boll_floor} 但已跌破破位线，暂缓")
            else:
                comp = max(comp, 0.6)
                notes.append(f"触及布林下轨参考 {boll_floor}")
                forced = forced or ("买入", "🟢")

    cost_avg = rules.get("cost_avg")
    if cost_avg is not None:
        cmp = "低于" if decision_price < cost_avg else "高于"
        notes.append(f"现价 {cmp} 参考成本 {cost_avg}")

    # 盘中实时价提示（仅信息，不改变信号）
    if live_price is not None and abs(live_price - decision_price) > 1e-6:
        diff = live_price - decision_price
        notes.append(f"盘中现价 {live_price:.2f}（决策价=昨收 {decision_price:.2f}，差 {diff:+.2f}）")

    comp = max(-1.0, min(1.0, comp))
    if forced:
        return comp, forced[0], forced[1], notes
    sig, emo = _map_signal_hyst(comp, prev_signal)
    return comp, sig, emo, notes



def analyze_stock(symbol: str, name: str = "", df: Optional[pd.DataFrame] = None,
                  weights: Optional[dict] = None, risk_gate=None,
                  rules: Optional[dict] = None,
                  holding: bool = False,
                  force_offline: bool = False,
                  prev_signal: Optional[str] = None,
                  breadth: Optional[tuple] = None) -> StockResult:
    """对单只股票跑四维度评分。risk_gate 为可选函数 signal->(bool, reason)。

    force_offline=True 或联网取数失败时，用合成数据「完整跑通全流程」并返回
    带分数/价位/信号的结果（offline=True 仅用于标注与抑制推送），
    不再返回空壳结果——否则看板会一片空白，看着像"没有结果"。
    """
    # 合并默认权重与调用方传入权重，缺失的维度键保留默认，避免 KeyError
    merged = dict(DEFAULT_WEIGHTS)
    if isinstance(weights, dict):
        merged.update({k: v for k, v in weights.items() if k in DEFAULT_WEIGHTS})
    weights = merged
    if df is None:
        df, offline = load_price(symbol, force_offline=force_offline)
    else:
        offline = False

    target_date = df.index[-1]
    # 趋势（本地量价，离线安全）
    sm, n_m = dim_trend(df)
    if offline:
        # 离线不触网：资金/估值/轮动/regime 全部中性，仅趋势+消息(中性)有分
        sz, n_z = money_proxy(df)
        n_z = [n + "（离线代理）" for n in n_z]
        sv, n_v = (0.0, ["估值：离线跳过(中性0)"])
        ss, n_s = (0.0, ["轮动：离线跳过(中性0)"])
        sreg, n_reg = (0.0, ["regime：离线跳过(中性0)"])
        sn, n_n = dim_news(symbol, offline=True)
    else:
        sz, n_z = dim_money(symbol, df=df)
        sv, n_v = dim_valuation(symbol, df)
        ss, n_s = dim_sector_rotation(symbol, df, target_date=target_date)
        # breadth 由调用方每轮只取一次传入（全A快照慢）；未传入则只用趋势
        sreg, n_reg = dim_regime(target_date=target_date, breadth=breadth)
        sn, n_n = dim_news(symbol)

    # 五因子方向性合成（regime 单独作乘数）
    comp_dir = (weights["trend"] * sm + weights["money"] * sz +
                weights["rotation"] * ss + weights["valuation"] * sv +
                weights["news"] * sn)
    comp_dir = max(-1.0, min(1.0, comp_dir))
    # regime 乘数：熊市(reg=-1)压到 0.55，牛市(reg=+1)不压制
    regime_factor = 0.55 + 0.45 * ((sreg + 1.0) / 2.0)
    composite = max(-1.0, min(1.0, comp_dir * regime_factor))
    signal, emoji = _map_signal_hyst(composite, prev_signal)

    last_price = float(df["close"].iloc[-1])
    # 决策价 = 昨收（最近完成交易日收盘），盘中实时价只用于显示与提示
    decision_price = float(df["close"].iloc[-2]) if len(df) >= 2 else last_price

    all_notes = n_m + n_z + n_v + n_s + n_reg + n_n
    # —— 个性化规则叠加（动态布林下轨、元力建仓区间等）——
    if rules:
        composite, signal, emoji, pnotes = _apply_personal_rules(
            rules, decision_price, composite, signal, all_notes,
            holding=holding, df=df, live_price=last_price,
            prev_signal=prev_signal)
    else:
        pnotes = all_notes

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
        market_score=sm, money_score=sz, sector_score=ss,
        valuation_score=sv, news_score=sn, regime_score=sreg,
        composite=composite, signal=signal, signal_emoji=emoji,
        notes=pnotes,
        risk_pass=risk_pass, risk_reason=risk_reason,
        last_price=last_price,
        source=source, data_date=data_date, pct_change=pct_change,
        as_of=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    if not risk_pass:
        res.signal = "暂停"; res.signal_emoji = "🔴"
    return res


if __name__ == "__main__":
    # 离线自测：合成数据验证逻辑接线
    r = analyze_stock("300034", "钢研高纳")
    print(r)


# ------------------------------------------------------- 信号持久化（每日一次）
# 看板主表信号不再每次刷新实时重算，而是每日算一次存入 signal_state.json，
# 盘中只更新「现价」与「盘中提示」，彻底杜绝买卖信号随 tick 秒翻。
STATE_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                            "signal_state.json")


def save_signal_state(results: list, as_of: Optional[str] = None) -> str:
    """把当日信号快照写入 signal_state.json，返回 as_of 时间戳。"""
    as_of = as_of or datetime.now().strftime("%Y-%m-%d %H:%M")
    data = {"as_of": as_of, "symbols": {}}
    for r in results:
        data["symbols"][r.symbol] = {
            "name": r.name, "signal": r.signal, "emoji": r.signal_emoji,
            "composite": r.composite,
            "market_score": r.market_score, "money_score": r.money_score,
            "sector_score": r.sector_score, "valuation_score": r.valuation_score,
            "news_score": r.news_score, "regime_score": r.regime_score,
            "last_price": r.last_price, "source": r.source,
            "data_date": r.data_date, "offline": r.offline,
            "notes": r.notes, "as_of": as_of,
        }
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass
    return as_of


def load_signal_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def state_fresh(state: dict, max_age_hours: int = 24) -> bool:
    """信号状态是否在有效期内（默认 24h 内算当日有效）。"""
    if not state or not state.get("as_of"):
        return False
    try:
        dt = datetime.strptime(state["as_of"], "%Y-%m-%d %H:%M")
        return (datetime.now() - dt) <= timedelta(hours=max_age_hours)
    except Exception:  # noqa: BLE001
        return False


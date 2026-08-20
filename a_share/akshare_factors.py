"""阶段1 免费多源因子（AkShare + 本地/指数计算）。

设计原则（与 datasource 一致）：
  1. 所有函数「失败即降级」，返回 (中性值, 原因)，绝不抛错阻断信号计算。
  2. 真实数据优先（AkShare 个股资金流 / 市场宽度），取不到自动回退到
     本地可计算的代理（量价 / 价格分位 / 指数动量），保证回测可回放、生产不死。
  3. 不引入任何付费源（Tushare 为可选升级，不在此文件）。

五个因子对应看板的：真实主力净流入 / 多周期动量+RS / 估值分位 /
板块-风格轮动 / 大盘趋势+宽度(regime)。
"""

from __future__ import annotations

import os as _os
# 抑制 AkShare 内部 tqdm 进度条（全A快照会刷屏且拖慢）
_os.environ.setdefault("TQDM_DISABLE", "1")

import numpy as np
import pandas as pd
from typing import Optional, Tuple

try:
    import akshare as ak  # 免费，无需 token
except Exception:  # pragma: no cover
    ak = None

try:
    from . import datasource as ds  # type: ignore
except ImportError:  # 直接以脚本方式运行时
    import datasource as ds  # type: ignore


def _market_prefix(symbol: str) -> str:
    """腾讯/东财/大智慧代码前缀：沪 sh / 深 sz / 北 bj。"""
    if symbol.startswith(("6", "5", "9")):
        return "sh"
    if symbol.startswith(("4", "8")):
        return "bj"
    return "sz"


# ---------------------------------------------------------- 1. 真实主力净流入
def fetch_main_inflow(symbol: str, limit: int = 10) -> Tuple[Optional[list], str]:
    """真实主力净流入(元)，最新一日在最后。

    链路：AkShare 个股资金流 → 东财 fetch_money_flow → 全失败返回 (None, 原因)。
    调用方应再回退到本地量价代理(money_proxy)，保证有分数。
    """
    # 1) AkShare（免费，但底层走东财，单 IP 可能被限流）
    if ak is not None:
        try:
            mkt = _market_prefix(symbol)
            df = ak.stock_individual_fund_flow(stock=symbol, market=mkt)
            if df is not None and len(df) > 0:
                col = None
                for c in df.columns:
                    if "主力" in c and ("净额" in c or "净流" in c):
                        col = c
                        break
                if col is None:
                    for c in df.columns:
                        if "主力" in c:
                            col = c
                            break
                if col is not None:
                    vals = pd.to_numeric(df[col], errors="coerce").dropna().tolist()
                    if vals:
                        return list(reversed(vals[-limit:])), "AkShare个股资金流"
        except Exception:  # noqa: BLE001
            pass
    # 2) 东财直连
    try:
        vals = ds.fetch_money_flow(symbol, limit=limit)
        if vals:
            return list(vals), "东财个股资金流"
    except Exception:  # noqa: BLE001
        pass
    return None, "资金流全部源失败(限流)"


# ---------------------------------------------------------- 3. 估值分位(代理)
def fetch_valuation_percentile(symbol: str, df: pd.DataFrame) -> Tuple[Optional[float], str]:
    """估值分位：近 250 日价格分位，作为 PE≈价/EPS 的免费、可回放代理。

    - 返回 (pct 0~1, 来源说明)；分位越低越便宜→越偏多。
    - 历史不足返回 (None, 原因)。
    说明：AkShare 的估值历史函数(stock_a_indicator*)在 1.18.x 被移除，
    故用价格历史分位近似——EPS 在 1 年内相对稳定，价格分位≈PE 分位。
    """
    try:
        close = df["close"]
        if len(close) < 60:
            return None, "估值:历史不足60日"
        win = close.iloc[-250:] if len(close) >= 250 else close
        lo, hi = float(win.min()), float(win.max())
        if hi <= lo:
            return 0.5, "估值:区间无波动"
        pct = (float(close.iloc[-1]) - lo) / (hi - lo)
        return float(np.clip(pct, 0.0, 1.0)), "价格分位(PE≈价/EPS代理,250日)"
    except Exception as e:  # noqa: BLE001
        return None, f"估值计算失败:{type(e).__name__}"


# ---------------------------------------------------------- 2/5. 指数动量工具
def _idx_ret(index_df: pd.DataFrame, target_date, win: int = 20) -> Optional[float]:
    """指数在 target_date 当日的近 win 日收益率（用于 RS / 轮动 / regime）。"""
    try:
        sub = index_df[index_df.index <= target_date]
        c = sub["close"]
        if len(c) < win + 1:
            return None
        return float(c.iloc[-1] / c.iloc[-win] - 1)
    except Exception:  # noqa: BLE001
        return None


_BENCHMARKS = {
    "沪深300": "sh000300",
    "中证500": "sh000905",
    "创业板指": "sz399006",
    "中证1000": "sh000852",
}


def fetch_benchmark_histories(days: int = 400) -> dict:
    """预取多基准指数历史（回测外层只取一次）。返回 {名称: df}。"""
    out = {}
    for name, code in _BENCHMARKS.items():
        try:
            df = ds.fetch_index_kline(code, days=days)
            if df is not None and len(df) >= 30:
                out[name] = df
        except Exception:  # noqa: BLE001
            continue
    return out


def sector_rotation_score(stock_sub: pd.DataFrame,
                          bench_hist: dict,
                          target_date) -> Tuple[float, list]:
    """板块/风格轮动：个股20日动量 − 多基准20日动量 的平均（相对强度）。

    高于 0 = 个股跑赢多数基准（处于领涨风格/板块）；低于 0 = 跑输。
    全部基准缺失时返回 (0.0, [原因])，由上层按中性计。
    """
    try:
        c = stock_sub["close"]
        if len(c) < 21:
            return 0.0, ["轮动:个股K线不足21日"]
        stock_ret = float(c.iloc[-1] / c.iloc[-20] - 1)
    except Exception:  # noqa: BLE001
        return 0.0, ["轮动:个股动量计算失败"]

    diffs = []
    notes = []
    for name, bdf in bench_hist.items():
        br = _idx_ret(bdf, target_date, win=20)
        if br is None:
            continue
        diffs.append(stock_ret - br)
        notes.append(f"{name}{stock_ret:+.1%}/{(stock_ret-br):+.1%}")
    if not diffs:
        return 0.0, ["轮动:基准数据缺失"]
    avg_diff = float(np.mean(diffs))
    # 映射到 [-1,1]：±15% 差异封顶
    score = float(np.clip(avg_diff / 0.15, -1.0, 1.0))
    return score, [f"板块/风格轮动(对{len(diffs)}基准均值){avg_diff:+.1%}"] + notes


# ---------------------------------------------------------- 4. 大盘状态(regime)
def market_regime(bench_hist: dict, target_date,
                  breadth_tuple: Optional[Tuple[float, str]] = None
                  ) -> Tuple[float, list]:
    """大盘状态(regime)：沪深300 60日趋势 + 可选的市场宽度(涨跌家数)。

    返回 (regime_score ∈ [-1,1], notes)。
    - 趋势：沪深300 现价比 60 日均线；>1.05 偏牛，<0.95 偏熊。
    - 宽度：上涨家数占比 − 0.5（仅实时可用；回测传 None → 只用趋势）。
    regime 用作信号合成的整体乘数（熊市压低买入倾向）。
    """
    notes = []
    trend_score = 0.0
    try:
        hs = bench_hist.get("沪深300")
        if hs is not None:
            sub = hs[hs.index <= target_date]
            c = sub["close"]
            if len(c) >= 61:
                ma60 = float(c.iloc[-60:].mean())
                ratio = float(c.iloc[-1]) / ma60
                # ratio 1.0→0; 1.08→+1; 0.92→-1
                trend_score = float(np.clip((ratio - 1.0) / 0.08, -1.0, 1.0))
                notes.append(f"沪深300 现/MA60={ratio:.3f}({'牛' if ratio>1 else '熊'})")
    except Exception:  # noqa: BLE001
        pass

    breadth_score = 0.0
    if breadth_tuple is not None and breadth_tuple[0] is not None:
        bval, bsrc = breadth_tuple
        breadth_score = float(np.clip((bval - 0.5) * 2.0, -1.0, 1.0))
        notes.append(bsrc)

    # 趋势为主(0.7)，宽度为辅(0.3)；回测无宽度时等于趋势
    if breadth_tuple is not None:
        score = 0.7 * trend_score + 0.3 * breadth_score
    else:
        score = trend_score
    return score, notes


def fetch_market_breadth() -> Tuple[Optional[float], str]:
    """市场宽度：全A上涨家数占比（AkShare 实时快照统计）。

    返回 (占比 0~1, 来源说明)；取数失败返回 (None, 原因)。
    仅实时有效，回测不调用（用趋势代替）。
    """
    if ak is None:
        return None, "AkShare未安装"
    try:
        sp = ak.stock_zh_a_spot_em()
        up = int((sp["涨跌幅"] > 0).sum())
        down = int((sp["涨跌幅"] < 0).sum())
        total = up + down
        if total == 0:
            return None, "宽度:涨跌家数为0"
        ratio = up / total
        return ratio, f"市场宽度 涨{up}/跌{down} 占比{ratio:.0%}"
    except Exception as e:  # noqa: BLE001
        return None, f"宽度取数失败:{type(e).__name__}"

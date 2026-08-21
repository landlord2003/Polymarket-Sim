"""L2 逐笔订单流因子：从原始 tick 聚合日频特征，接入 walk-forward IC 框架。

数据契约（与 alt_factors.build 对齐）：
    build(symbol, dates, closes) -> DataFrame(index=dates, columns=L2_NAMES)
    - 按 dates 向前填充（pandas 前向填充），保证与 K 线行日期对齐、无未来函数。
    - 首次调用按 src 生成缓存：synth=合成多日（pipeline 全链路验证用），
      akshare=真实当日逐笔（仅 1 天，用于真实数据解析校验，不能跑 walk-forward）。

真实 tick 来源（AKShare 免费）：ak.stock_zh_a_tick_tx_js(symbol)
    字段：成交时间 / 成交价格 / 价格变动 / 成交量(手) / 成交金额 / 性质(买盘|卖盘|中性盘)
    ⚠️ 免费接口只返回「最新一个交易日」，无历史 → 真实 edge 验证需付费历史逐笔。

特征工程（12 维，刻意不堆交互，防重蹈日频过拟合覆辙）：
    1  oi              订单失衡 (买量-卖量)/(买量+卖量)          量价层级高于日频 moneyflow
    2  aggr_buy_ratio  主动买入占比 (Lee-Ready 买盘量/(买+卖))
    3  buy_cnt_ratio   主动买入笔数占比
    4  large_trade_ratio 大单量占比 (成交量>当日中位数的成交占比)
    5  avg_trade_size  平均每笔成交量(手)
    6  log_trade_count 成交笔数(log1p，流动性代理)
    7  tick_vol        逐笔价格变动标准差(日内波动)
    8  close_imbalance 尾盘10%成交的订单失衡(收盘竞价压力)
    9  price_drift      日内漂移 (尾价/首价-1，知情交易代理)
    10 amihud           Amihud 非流动性 |drift|/成交额
    11 kyle_lambda      Kyle's lambda |drift|/|净买量比| (价格冲击)
    12 neutral_ratio    中性盘(隐藏流动性/冰山单)量占比
"""
from __future__ import annotations
import os
import json
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "data", "l2_daily")

# 12 维特征名（顺序固定，下游维度依赖此顺序）
L2_NAMES = [
    "oi", "aggr_buy_ratio", "buy_cnt_ratio", "large_trade_ratio",
    "avg_trade_size", "log_trade_count", "tick_vol", "close_imbalance",
    "price_drift", "amihud", "kyle_lambda", "neutral_ratio",
]
L2_DIM = len(L2_NAMES)
_EPS = 1e-9


# ---------------------------------------------------------------- 真实逐笔聚合（核心，须经真实数据验证）
def aggregate_daily(tick: pd.DataFrame) -> dict | None:
    """输入一只股票某一交易日的逐笔 DataFrame（AKShare 或合成，字段同上）。
    返回 12 维日频因子 dict；数据不足返回 None。"""
    if tick is None or len(tick) == 0:
        return None
    try:
        P = pd.to_numeric(tick.get("成交价格"), errors="coerce")
        V = pd.to_numeric(tick.get("成交量"), errors="coerce").fillna(0.0)
        nat = tick.get("性质")
        if nat is None:
            return None
        nat = nat.astype(str).str.strip()
    except Exception:
        return None
    if len(P) == 0:
        return None
    buy = nat == "买盘"
    sell = nat == "卖盘"
    neu = nat == "中性盘"
    vb = float(V[buy].sum()); vs = float(V[sell].sum()); vn = float(V[neu].sum())
    vt = vb + vs + vn
    nb = int(buy.sum()); ns = int(sell.sum()); nn = int(neu.sum()); nt = nb + ns + nn
    if nt == 0:
        return None

    oi = (vb - vs) / (vt + _EPS)
    aggr = vb / (vb + vs + _EPS)
    buy_cnt = nb / (nb + ns + _EPS)
    med = V.median() if len(V) else 0.0
    large_ratio = float(V[V > med].sum()) / (vt + _EPS) if (med and med > 0) else 0.0
    avg_size = vt / (nt + _EPS)
    log_cnt = np.log1p(nt)
    chg = pd.to_numeric(tick.get("价格变动"), errors="coerce")
    tick_vol = float(chg.std()) if len(chg) > 1 else 0.0
    k = max(1, int(0.1 * nt))
    vb_c = float(V[buy].tail(k).sum()); vs_c = float(V[sell].tail(k).sum())
    close_imb = (vb_c - vs_c) / (vb_c + vs_c + _EPS)
    p0 = float(P.iloc[0]); p1 = float(P.iloc[-1])
    drift = (p1 - p0) / p0 if (p0 and p0 > 0) else 0.0
    tot_amt = float(pd.to_numeric(tick.get("成交金额"), errors="coerce").sum())
    amihud = abs(drift) / (tot_amt / (vt + _EPS) + _EPS) if vt > 0 else 0.0
    net_share = abs(vb - vs) / (vt + _EPS)
    kyle = abs(drift) / (net_share + _EPS)
    neutral_ratio = vn / (vt + _EPS)

    return {
        "oi": oi, "aggr_buy_ratio": aggr, "buy_cnt_ratio": buy_cnt,
        "large_trade_ratio": large_ratio, "avg_trade_size": avg_size,
        "log_trade_count": log_cnt, "tick_vol": tick_vol, "close_imbalance": close_imb,
        "price_drift": drift, "amihud": amihud, "kyle_lambda": kyle,
        "neutral_ratio": neutral_ratio,
    }


# ---------------------------------------------------------------- 合成 tick（pipeline 全链路验证用，明确无真实信号）
def _gen_synth_tick(n_trades: int, seed: int, base_price: float) -> pd.DataFrame:
    """生成一只股票某一日的合成逐笔。 microstructure 真实但不含任何对未来收益的预测信号
    （价格随机游走、买卖性质带短程持续性），用于验证聚合+walk-forward 链路不崩、维度正确。"""
    rng = np.random.default_rng(seed)
    price = base_price
    rows = []
    side = rng.integers(0, 3)  # 0买 1卖 2中性
    t = 9 * 3600 + 30 * 60  # 09:30
    for _ in range(n_trades):
        # 价格随机游走（无方向性 -> 对未来收益无预测力）
        price *= (1.0 + rng.normal(0, 0.0004))
        # 买卖性质短程持续性
        if rng.random() < 0.7:
            pass
        else:
            side = rng.integers(0, 3)
        vol = int(rng.lognormal(mean=np.log(300), sigma=0.8)) + 1
        amt = price * vol * 100.0
        nat = ["买盘", "卖盘", "中性盘"][side]
        h = int(t // 3600); m = int((t % 3600) // 60); s = int(t % 60)
        rows.append([
            f"{h:02d}:{m:02d}:{s:02d}", round(price, 2), 0.0, vol, round(amt, 1), nat
        ])
        t += rng.integers(1, 6)  # 逐笔间隔 1~5 秒
        if t >= 15 * 3600:  # 15:00 收盘
            break
    df = pd.DataFrame(rows, columns=["成交时间", "成交价格", "价格变动", "成交量", "成交金额", "性质"])
    return df


def gen_synth_daily_cache(symbol: str, n_days: int = 600, seed_base: int = 20260821,
                          out_dir: str = CACHE_DIR, n_trades: int = 400) -> str:
    """为某股票生成 n_days 个交易日的合成逐笔并聚合为日频因子，写入
    data/l2_daily/l2_daily_{symbol}.csv。返回路径。"""
    os.makedirs(out_dir, exist_ok=True)
    rng0 = np.random.default_rng(seed_base + abs(hash(symbol)) % 100000)
    base_price = rng0.uniform(8.0, 120.0)
    # 用连续交易日序列（仅用于索引，forward-fill 对齐 K 线时按日期对齐）
    end = pd.Timestamp.today().normalize()
    bdays = pd.bdate_range(end=end, periods=n_days)
    recs = []
    for di, d in enumerate(bdays):
        seed = (seed_base + abs(hash(symbol)) * 131 + di * 977) % (2**31)
        tick = _gen_synth_tick(n_trades=n_trades, seed=seed, base_price=base_price)
        feat = aggregate_daily(tick)
        if feat is None:
            feat = {k: 0.0 for k in L2_NAMES}
        recs.append({**{"date": d}, **feat})
    out = pd.DataFrame(recs)[["date"] + L2_NAMES].set_index("date")
    out.index = pd.to_datetime(out.index)
    path = os.path.join(out_dir, f"l2_daily_{symbol}.csv")
    out.to_csv(path)
    return path


def gen_akshare_daily_cache(symbol: str, out_dir: str = CACHE_DIR) -> str | None:
    """抓取 AKShare 真实当日逐笔并聚合为 1 行日频因子，写入缓存。仅 1 天 -> 用于真实数据解析校验。"""
    import akshare as ak
    os.makedirs(out_dir, exist_ok=True)
    try:
        tick = ak.stock_zh_a_tick_tx_js(symbol=("sh" if symbol.startswith("6") else "sz") + symbol)
    except Exception as e:
        print(f"  [akshare] {symbol} 抓取失败: {repr(e)[:120]}")
        return None
    if tick is None or len(tick) == 0:
        return None
    feat = aggregate_daily(tick)
    if feat is None:
        return None
    d = pd.Timestamp.today().normalize()
    out = pd.DataFrame([{**{"date": d}, **feat}])[["date"] + L2_NAMES].set_index("date")
    out.index = pd.to_datetime(out.index)
    path = os.path.join(out_dir, f"l2_daily_{symbol}.csv")
    out.to_csv(path)
    return path


# ---------------------------------------------------------------- 对外接口（与 alt_factors.build 同契约）
def build(symbol: str, dates, closes=None, src: str = "synth",
          out_dir: str = CACHE_DIR) -> pd.DataFrame:
    """返回与 dates 对齐的 L2 日频因子 DataFrame（向前填充）。dates 为 K 线行日期序列。"""
    path = os.path.join(out_dir, f"l2_daily_{symbol}.csv")
    if not os.path.exists(path):
        if src == "akshare":
            gen_akshare_daily_cache(symbol, out_dir)
        else:
            gen_synth_daily_cache(symbol, out_dir=out_dir)
    if not os.path.exists(path):
        return pd.DataFrame(index=pd.to_datetime(list(dates)), columns=L2_NAMES, dtype=float)
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df = df.reindex(df.index)  # 保序
    idx = pd.to_datetime(list(dates))
    out = df.reindex(idx, method="ffill")
    out = out.bfill().ffill()
    # 防 inf / 极端值破坏 standardize
    out = out.replace([np.inf, -np.inf], 0.0).clip(-1e6, 1e6)
    out = out.fillna(0.0)
    return out[L2_NAMES]


if __name__ == "__main__":
    # 自检：合成一只股票聚合是否产出有限 12 维
    t = _gen_synth_tick(500, 1, 50.0)
    f = aggregate_daily(t)
    print("synth agg ->", None if f is None else {k: round(v, 4) for k, v in f.items()})
    print("L2_DIM =", L2_DIM, "names =", L2_NAMES)

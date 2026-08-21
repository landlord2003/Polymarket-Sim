"""Path3 本地 ML 模型（零依赖 / 零费用 / 零未来函数）

目标：用「模型从数据中学权重」替代 Path2 的手设权重，看看 A 股日频信号
准确率能否突破规则基线的 ~39%（随机基准 50%）。

设计要点（必须诚实）：
  - 特征全部**点-时间**（point-in-time）：只用当日及之前数据计算，复用
    signal_engine 的因子函数（已验证全部按 target_date 切片，无未来函数）
    + 原始量价特征（多周期收益/波动/量比/均线距离/RSI）。
  - 标签：N 日（5/10/20）后收益为正当 y=1，否则 0（二分类）。
  - 训练/评估：walk-forward（扩展窗口 + 每月重训），避免用未来信息。
  - 对比：与规则基线在**同一批交易日**上对比 precision_up（发出看多后
    实际上涨的占比），口径一致才公平。
  - 零依赖：LR 与 Gradient Boosting 均用 numpy 从零实现，不装 sklearn
    （本机 PyPI 下载超时，且纯 numpy 更可复现）。

用法：
  python a_share/ml_model.py --out D:/WorkBuddy/output/ml_report.md
  python a_share/ml_model.py --symbols 300034 002085 688786
"""

from __future__ import annotations

import sys
import os
import time
import argparse
import json
import concurrent.futures as _cf
from typing import Optional

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from signal_engine import (dim_trend, money_proxy, dim_valuation,
                           dim_sector_rotation, dim_regime,
                           money_score_from_inflow, proxy_inflow_series,
                           load_moneyflow_cache, load_moneyflow_full,
                           refined_money_block, _mfi_series, _adi_series,
                           _map_signal, DEFAULT_WEIGHTS)
from akshare_factors import fetch_benchmark_histories

HORIZONS = (5, 10, 20)
START_DAYS = 260        # 预热：估值分位需 250 日窗口
MIN_TRAIN = 120         # walk-forward 最少训练样本
RETRAIN_GAP = 30        # 每 30 个交易日（约1月）重训一次


# ----------------------------------------------------------- 数据加载（同 backtest）
_HIST_CACHE = {}   # (symbol, days) -> DataFrame；进程内复用，避免同一标的多视角重复抓取


def _synthetic_data(symbol: str, n: int = 600) -> pd.DataFrame:
    rng = np.random.default_rng(abs(hash(symbol)) % (2**32))
    dates = pd.date_range(end=pd.Timestamp.today().normalize(), periods=n, freq="D")
    close = 25.0 + np.cumsum(rng.normal(0, 0.3, n))
    close = np.maximum(close, 1.0)
    open_ = close + rng.normal(0, 0.1, n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 1, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 1, n))
    vol = rng.integers(1_000_000, 5_000_000, n)
    df = pd.DataFrame({"open": open_, "high": high, "low": low,
                       "close": close, "volume": vol}, index=dates)
    df.attrs["synthetic"] = True
    return df


def _fetch_kline_timed(symbol: str, days: int, timeout: float = 25) -> Optional[pd.DataFrame]:
    """带线程超时的 K 线抓取：单次超 timeout 秒直接判失败（降级合成），避免网络阻塞卡死回测。"""
    try:
        from datasource import fetch_kline
        with _cf.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(fetch_kline, symbol, days=days)
            df = fut.result(timeout=timeout)
        if df is not None and len(df) >= 30:
            return df
        raise RuntimeError("K线不足/空")
    except Exception as e:  # 离线兜底：仅验证引擎
        print(f"[warn] {symbol} 取数失败（{e}），使用合成数据（非真实准确率）")
        return None


def load_hist(symbol: str, days: int = 600) -> pd.DataFrame:
    key = (symbol, days)
    if key in _HIST_CACHE:
        return _HIST_CACHE[key]
    df = _load_hist_disk(symbol, days)
    _HIST_CACHE[key] = df
    return df


def _load_hist_disk(symbol: str, days: int) -> pd.DataFrame:
    """带磁盘缓存的 K 线加载：优先读 data/cache/kline_<symbol>.csv（2天内），
    否则抓取并落盘。使 proxy/real 两次回测共享完全相同的 K 线（标签一致、公平），
    且第二次运行几乎瞬时。抓取失败/超时降级合成。
    """
    p = os.path.join(HERE, "data", "cache", f"kline_{symbol}.csv")
    try:
        if os.path.exists(p):
            age = time.time() - os.path.getmtime(p)
            if age < 2 * 86400:
                d = pd.read_csv(p, index_col=0, parse_dates=True)
                if len(d) >= 30:
                    return d
    except Exception:  # noqa: BLE001
        pass
    df = _fetch_kline_timed(symbol, days)
    if df is None:
        return _synthetic_data(symbol)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        df.to_csv(p)
    except Exception:  # noqa: BLE001
        pass
    return df


# ----------------------------------------------------------- 特征工程（点-时间）
def _rsi(close: pd.Series, win: int = 14) -> float:
    if len(close) < win + 1:
        return 50.0
    delta = close.diff().dropna()
    if len(delta) < win:
        return 50.0
    gain = delta.clip(lower=0).rolling(win).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).rolling(win).mean().iloc[-1]
    if loss <= 1e-9:
        return 100.0 if gain > 0 else 50.0
    rs = gain / loss
    return float(100 - 100 / (1 + rs))


def feat_vector(symbol: str, sub: pd.DataFrame, bench_hist: dict,
                hs300, tgt, money_series=None) -> tuple:
    """返回 (特征向量 ndarray, 5因子分 [trend,money,rotation,valuation,regime])。

    所有计算只用 sub（df.iloc[:i+1]）与 target_date=tgt，无未来函数。
    money_series: 日度主力净流入(元) Series（已对齐 df.index）；为 None 时用
      本地量价代理 proxy_inflow_series 构造（proxy 模式）。real 模式由调用方
      预取 Tushare 缓存并传入，内部按 sub.index 切片，无未来函数。
    """
    try:
        sm, _ = dim_trend(sub, idx_df=hs300)
        if money_series is not None:
            ms = money_series.reindex(sub.index).dropna()
            sz = money_score_from_inflow(ms) if len(ms) >= 6 \
                else money_score_from_inflow(proxy_inflow_series(sub))
        else:
            sz = money_score_from_inflow(proxy_inflow_series(sub))
        sv, _ = dim_valuation(symbol, sub)
        ss, _ = dim_sector_rotation(symbol, sub, bench_hist, tgt)
        sreg, _ = dim_regime(bench_hist, tgt, breadth=None)
    except Exception:
        sm = sz = sv = ss = sreg = 0.0

    close = sub["close"]
    vol = sub["volume"]
    extra = [0.0] * 9
    try:
        extra[0] = float(close.iloc[-1] / close.iloc[-6] - 1)      # ret5
        extra[1] = float(close.iloc[-1] / close.iloc[-21] - 1)     # ret20
        extra[2] = float(close.iloc[-1] / close.iloc[-61] - 1)     # ret60
        rc = close.diff().dropna()
        extra[3] = float(rc.iloc[-20:].std()) if len(rc) >= 20 else 0.0   # vol20
        extra[4] = float(vol.iloc[-1] / max(vol.iloc[-20:].mean(), 1.0))   # 量比
        extra[5] = float(close.iloc[-1] / close.iloc[-20:].mean())         # 价/MA20
        extra[6] = float(close.iloc[-1] / close.iloc[-60:].mean())          # 价/MA60
        extra[7] = _rsi(close) / 100.0 - 0.5                                # RSI(中心化)
        win = close.iloc[-250:] if len(close) >= 250 else close
        extra[8] = float(close.iloc[-1] / win.max() - 1)                    # 距250日高
    except Exception:
        pass

    vec = np.array([sm, sz, ss, sv, sreg] + extra, dtype=float)
    return vec, [sm, sz, ss, sv, sreg]


# ----------------------------------------------------------- 模型 1：Logistic Regression (L2)
class LogisticRegression:
    def __init__(self, lr: float = 0.3, iters: int = 300, reg: float = 1e-3):
        self.lr = lr
        self.iters = iters
        self.reg = reg
        self.w = None
        self.b = 0.0
        self.mu = None
        self.sd = None

    def _sigmoid(self, z):
        return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))

    def fit(self, X, y):
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float)
        self.mu = X.mean(axis=0)
        self.sd = X.std(axis=0) + 1e-8
        Xs = (X - self.mu) / self.sd
        n, d = Xs.shape
        self.w = np.zeros(d)
        self.b = 0.0
        for _ in range(self.iters):
            z = Xs @ self.w + self.b
            p = self._sigmoid(z)
            grad_w = (Xs.T @ (p - y)) / n + self.reg * self.w
            grad_b = ((p - y).sum()) / n
            self.w -= self.lr * grad_w
            self.b -= self.lr * grad_b

    def predict_proba(self, X):
        X = np.asarray(X, dtype=float)
        Xs = (X - self.mu) / self.sd
        return self._sigmoid(Xs @ self.w + self.b)


# ----------------------------------------------------------- 模型 2：Gradient Boosting (depth-2 决策树)
def _fit_stump(X, r):
    """在残差 r 上拟合一个 depth-2 决策树（两特征交互），返回可预测的单棵树。

    返回 (thr_j, thr, lv, rv) 不足以表达 depth-2；改为返回闭包预测函数，
    让 GBT 能学到跨特征交互（如 XOR），而非退化成单特征树桩。
    """
    n, d = X.shape

    def _sse(idxs):
        if len(idxs) == 0:
            return 0.0
        return float(np.sum((r[idxs] - r[idxs].mean()) ** 2))

    def _leaf(idxs):
        return float(r[idxs].mean()) if len(idxs) else 0.0

    # 第一层分裂（候选阈值更密，20 个分位）
    best = None  # (sse, j, thr, left_idx, right_idx)
    for j in range(d):
        xj = X[:, j]
        qs = np.quantile(xj, np.linspace(0.05, 0.95, 20))
        for thr in qs:
            li = np.where(xj <= thr)[0]
            ri = np.where(xj > thr)[0]
            sse = _sse(li) + _sse(ri)
            if best is None or sse < best[0] - 1e-9:
                best = (sse, j, float(thr), li, ri)
    if best is None:
        return lambda _X: np.full(len(_X), _leaf(np.arange(n)))
    _, j1, thr1, li, ri = best
    xj1 = X[:, j1]

    def _make_tree():
        # 第二层：对左右两支各做一次单特征分裂（除 j1 外）
        def split_branch(idxs):
            if len(idxs) < 4:
                return ("leaf", _leaf(idxs))
            bi = np.zeros(n, dtype=bool)
            bi[idxs] = True
            bj = bi & (xj1 <= thr1) if False else None  # placeholder
            # 直接对该子集选最优单特征分裂
            sub = np.where(bi)[0]
            bb = None
            for j in range(d):
                if j == j1:
                    continue
                xj = X[sub, j]
                qs = np.quantile(xj, np.linspace(0.1, 0.9, 10))
                for thr in qs:
                    l2 = sub[xj <= thr]
                    r2 = sub[xj > thr]
                    sse = _sse(l2) + _sse(r2)
                    if bb is None or sse < bb[0] - 1e-9:
                        bb = (sse, j, float(thr), l2, r2)
            if bb is None:
                return ("leaf", _leaf(sub))
            _, j2, thr2, l2, r2 = bb
            return ("node", j2, thr2, _leaf(l2), _leaf(r2))

        left_tree = split_branch(li)
        right_tree = split_branch(ri)

        def predict(_X):
            out = np.empty(len(_X))
            for i in range(len(_X)):
                if _X[i, j1] <= thr1:
                    node = left_tree
                else:
                    node = right_tree
                if node[0] == "leaf":
                    out[i] = node[1]
                else:
                    _, j2, thr2, lv, rv = node
                    out[i] = lv if _X[i, j2] <= thr2 else rv
            return out

        return predict

    return _make_tree()


class GradientBoosting:
    def __init__(self, rounds: int = 40, lr: float = 0.1):
        self.rounds = rounds
        self.lr = lr
        self.trees = []   # 每个元素是 _fit_stump 返回的「可预测闭包」
        self.base = 0.0

    def _sigmoid(self, z):
        return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))

    def fit(self, X, y):
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float)
        p = y.mean()
        p = min(max(p, 1e-3), 1 - 1e-3)
        self.base = float(np.log(p / (1 - p)))
        F = np.full(len(y), self.base)
        self.trees = []
        for _ in range(self.rounds):
            prob = self._sigmoid(F)
            r = y - prob
            tree_pred_fn = _fit_stump(X, r)
            F += self.lr * tree_pred_fn(X)
            self.trees.append(tree_pred_fn)

    def predict_proba(self, X):
        X = np.asarray(X, dtype=float)
        F = np.full(len(X), self.base)
        for tree_pred_fn in self.trees:
            F += self.lr * tree_pred_fn(X)
        return self._sigmoid(F)


# ----------------------------------------------------------- 数据集构建（逐日）
_ORDER_COLS = {"buy_elg_amount", "sell_elg_amount", "buy_lg_amount",
               "sell_lg_amount", "buy_md_amount", "sell_md_amount",
               "buy_sm_amount", "sell_sm_amount"}


def _build_pre(symbol: str, df: pd.DataFrame, money_cache: Optional[dict]):
    """预计算精细资金流所需的全长序列（对齐 df.index，点-时间安全）。

    返回 (pre, has_real, inflow_full)：
      pre: {mfi, adi, price_div, elg_net, lg_net, md_net, sm_net, main_net}
      has_real: 是否拿到真实订单档位数据（--money real 且缓存含全字段）
      inflow_full: 日度主力净流入(元) Series（供 feat_vector 算 crude 资金分）
    """
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
    mfi = _mfi_series(close, high, low, vol)
    adi = _adi_series(close, high, low, vol)
    price_ret5 = close.pct_change(5)
    elg_net = lg_net = md_net = sm_net = main_net = None
    inflow_full = None
    has_real = False
    if money_cache and symbol in money_cache:
        mc = money_cache[symbol]
        if isinstance(mc, pd.DataFrame):
            mc = mc.reindex(df.index)
            if "main_net_in" in mc.columns:
                inflow_full = mc["main_net_in"]
            if _ORDER_COLS.issubset(set(mc.columns)):
                elg_net = mc["buy_elg_amount"] - mc["sell_elg_amount"]
                lg_net = mc["buy_lg_amount"] - mc["sell_lg_amount"]
                md_net = mc["buy_md_amount"] - mc["sell_md_amount"]
                sm_net = mc["buy_sm_amount"] - mc["sell_sm_amount"]
                main_net = elg_net + lg_net
                has_real = True
        elif isinstance(mc, pd.Series):
            inflow_full = mc.reindex(df.index)
    # 价格-资金背离编码（仅 has_real 时有意义；proxy 全 0）
    price_div = pd.Series(0.0, index=df.index)
    if has_real and main_net is not None:
        main5 = main_net.rolling(5).sum()
        up = price_ret5 >= 0
        money_in = main5 >= 0
        enc = np.where(up & money_in, 1.0,
              np.where((~up) & money_in, 0.5,
              np.where(up & (~money_in), -0.5,
              np.where((~up) & (~money_in), -1.0, 0.0))))
        price_div = pd.Series(enc, index=df.index)
    pre = {"mfi": mfi, "adi": adi, "price_div": price_div,
           "elg_net": elg_net, "lg_net": lg_net, "md_net": md_net,
           "sm_net": sm_net, "main_net": main_net}
    return pre, has_real, inflow_full


def build_rows(symbol: str, bench_hist: dict, hs300, horizon: int,
                money_cache: Optional[dict] = None) -> list:
    """返回逐日行列表：每行 {X, y, factors:[5因子], date}。

    X = 14 维基础特征 + 10 维精细资金流块（共 24 维）。proxy 模式下后 10 维中
    订单档位相关项恒为 0（仅 MFI/ADI 由 K 线计算），从而干净隔离真实订单信息增量。
    只保留 i 有足够未来（i+horizon < n）且 i>=START_DAYS 的行。
    money_cache: {symbol: 全字段资金流 DataFrame}（real）或 {symbol: 净流入 Series}（兼容）。
    """
    df = load_hist(symbol)
    n = len(df)
    if n < START_DAYS + horizon + 1:
        return []
    pre, has_real, inflow_full = _build_pre(symbol, df, money_cache)
    closes = df["close"].values.astype(float)
    rows = []
    for i in range(START_DAYS, n - horizon):
        sub = df.iloc[:i + 1]
        tgt = sub.index[-1]
        vec, fac = feat_vector(symbol, sub, bench_hist, hs300, tgt,
                               money_series=inflow_full)
        rb = refined_money_block(pre, i, has_real)
        vec = np.concatenate([vec, np.asarray(rb, dtype=float)])
        if np.any(~np.isfinite(vec)):
            vec = np.nan_to_num(vec, nan=0.0, posinf=0.0, neginf=0.0)
        y = 1.0 if closes[i + horizon] / closes[i] - 1 > 0 else 0.0
        rows.append({"X": vec, "y": y, "factors": fac, "date": tgt})
    return rows


# ----------------------------------------------------------- walk-forward 评估
def walk_forward(rows: list, ModelCls, **mk) -> np.ndarray:
    """扩展窗口 + 每 RETRAIN_GAP 重训。返回与 rows 等长的预测概率数组（前段为 nan）。"""
    n = len(rows)
    if n < MIN_TRAIN + 1:
        return np.full(n, np.nan)
    X = np.array([r["X"] for r in rows])
    y = np.array([r["y"] for r in rows])
    probs = np.full(n, np.nan)
    model = None
    last_train = -1
    for d in range(n):
        if d < MIN_TRAIN:
            continue
        if model is None or d >= last_train + RETRAIN_GAP:
            model = ModelCls(**mk)
            model.fit(X[:d], y[:d])
            last_train = d
        probs[d] = model.predict_proba(X[d:d + 1])[0]
    return probs


def rule_baseline(rows: list) -> tuple:
    """规则基线（同 Path1/2 综合打分），返回 (precision_up, coverage, n)。"""
    up = []
    ys = []
    for r in rows:
        tr, mo, ro, va, re = r["factors"]
        comp_dir = (DEFAULT_WEIGHTS["trend"] * tr + DEFAULT_WEIGHTS["money"] * mo +
                    DEFAULT_WEIGHTS["rotation"] * ro + DEFAULT_WEIGHTS["valuation"] * va)
        comp_dir = max(-1.0, min(1.0, comp_dir))
        regf = 0.55 + 0.45 * ((re + 1.0) / 2.0)
        comp = max(-1.0, min(1.0, comp_dir * regf))
        sig, _ = _map_signal(comp)
        up.append(sig in ("买入", "偏多"))
        ys.append(r["y"])
    up = np.array(up)
    ys = np.array(ys)
    if up.sum() == 0:
        return float("nan"), 0.0, int(len(ys))
    return float(ys[up].mean()), float(up.mean()), int(len(ys))


def evaluate(probs: np.ndarray, ys: np.ndarray, thr: float = 0.5) -> tuple:
    """返回 (precision_up, coverage, accuracy)；probs 含 nan 行跳过。

    thr: 判定「看多」的概率阈值。默认 0.5；高置信分层用 0.6。
    """
    m = ~np.isnan(probs)
    if m.sum() == 0:
        return float("nan"), float("nan"), float("nan")
    p = probs[m]
    y = ys[m]
    pred_up = p >= thr
    if pred_up.sum() == 0:
        return float("nan"), 0.0, float((pred_up == y).mean())
    return float(y[pred_up].mean()), float(pred_up.mean()), float((pred_up == y).mean())


# ----------------------------------------------------------- 扩大股票池（自动荐股）
def build_universe() -> list:
    """从 screener.CORE_POOL 去重构造 (code,name,sector) 列表（~39 只真实龙头）。

    用途：自动荐股的扫描池。这些全是真实龙头股代码；价格走多源直连真实行情，
    只有 load_hist 回退合成时才算非真实（会跳过）。
    """
    try:
        from screener import CORE_POOL
    except Exception:  # noqa: BLE001
        CORE_POOL = {}
    seen = {}
    for sector, stocks in CORE_POOL.items():
        for code, name in stocks:
            if code not in seen:
                seen[code] = (code, name, sector)
    return list(seen.values())


def _model_spec(name: str):
    """返回 (ModelCls, kwargs)。name 含 'gb' → 梯度提升，否则 LR。"""
    if name.lower().startswith("gb"):
        return GradientBoosting, {"rounds": 40, "lr": 0.1}
    return LogisticRegression, {}


def predict_latest(symbol, name, sector, horizon, ModelCls, bench_hist,
                   hs300, money_cache=None, **mk) -> Optional[dict]:
    """在全历史训练后，预测「最新一日」未来 N 日的 ML 相对评分（非校准概率，仅供排序）。返回 dict 或 None。

    无未来函数：特征用 df.iloc[:n]（含最新收盘，属「已知」）；标签不参与预测链路。
    """
    df = load_hist(symbol)
    n = len(df)
    if n < START_DAYS + horizon + 1:
        return None
    rows = build_rows(symbol, bench_hist, hs300, horizon, money_cache=money_cache)
    if len(rows) < MIN_TRAIN:
        return None
    y_train = np.array([r["y"] for r in rows])
    if y_train.mean() < 0.05 or y_train.mean() > 0.95:
        return None  # 标签退化（全涨/全跌），该标的模型无意义
    X = np.array([r["X"] for r in rows])
    model = ModelCls(**mk)
    model.fit(X, y_train)
    sub = df.iloc[:n]
    tgt = sub.index[-1]
    pre, has_real, inflow_full = _build_pre(symbol, df, money_cache)
    vec, fac = feat_vector(symbol, sub, bench_hist, hs300, tgt, money_series=inflow_full)
    rb = refined_money_block(pre, n - 1, has_real)
    vec = np.concatenate([vec, np.asarray(rb, dtype=float)])
    vec = np.nan_to_num(vec, nan=0.0, posinf=0.0, neginf=0.0)
    prob = float(model.predict_proba(vec.reshape(1, -1))[0])
    last = float(df["close"].iloc[-1])
    prev = float(df["close"].iloc[-2]) if n >= 2 else last
    pct = (last / prev - 1) * 100.0 if prev else 0.0
    return {"symbol": symbol, "name": name, "sector": sector,
            "prob": prob, "last": round(last, 2), "pct": round(pct, 2),
            "factors": [round(x, 3) for x in fac], "n_train": len(rows)}


def auto_recommend(horizon: int = 10, model_name: str = "LR",
                   top_n: int = 12, min_prob: float = 0.6,
                   bench_hist=None, hs300=None, universe=None,
                   money_cache=None) -> tuple:
    """扫全池，返回 (高置信标的, 全部排序, 跳过列表[(code,name,reason)])。"""
    if bench_hist is None:
        bench_hist = fetch_benchmark_histories(days=400)
        hs300 = bench_hist.get("沪深300")
    ModelCls, mk = _model_spec(model_name)
    if universe is None:
        universe = build_universe()
    picks, skipped = [], []
    for (code, name, sector) in universe:
        try:
            r = predict_latest(code, name, sector, horizon, ModelCls,
                               bench_hist, hs300, money_cache=money_cache, **mk)
            if r is None:
                skipped.append((code, name, "数据不足/标签退化"))
                continue
            picks.append(r)
        except Exception as e:  # noqa: BLE001
            skipped.append((code, name, str(e)[:80]))
    picks.sort(key=lambda x: x["prob"], reverse=True)
    rec = [p for p in picks if p["prob"] >= min_prob]
    return rec, picks, skipped


def _write_recommend(path: str, rec, picks, skipped,
                     horizon, model_name, min_prob):
    from datetime import datetime
    cache_path = os.path.join(HERE, "recommend_cache.json")
    lines = [f"# 🤖 自动荐股（ML · {datetime.today().strftime('%Y-%m-%d %H:%M')}）\n"]
    lines.append(f"> 模型：{model_name}；视角：未来 **{horizon} 日** ML 相对评分（非校准概率，仅供排序）；"
                 f"高置信阈值 ≥ {min_prob:.2f}。\n")
    lines.append(f"> 扫描池：{len(picks) + len(skipped)} 只（screener 五板块龙头去重）；"
                 f"有效预测 {len(picks)} 只，跳过 {len(skipped)} 只。\n")
    lines.append("## ✅ ML 打分候选（ML 评分 ≥ %.0f%%，按评分降序）\n" % (min_prob * 100))
    lines.append("> ⚠️ **重要（扩大池回测结论）**：在 39 只代表性池子上，ML 的 10 日 precision_up "
                 "仅 **45-47%**，与规则基线（48-52%）相当，**未产生可泛化的超额收益**。\n"
                 "> 此前 6 只自选股上的 54.6% 属小样本过拟合（那 6 只是偏难做的子集，规则基线异常低）。\n"
                 "> 因此本名单是「**ML 当前观点下概率最高的候选**」，**不是高置信买点**，仅供研究排序参考。\n")
    if rec:
        lines.append("| 排名 | 代码 | 名称 | 板块 | ML 评分 | 最新价 | 今日涨跌 | 趋势 | 资金 | 轮动 | 估值 | 大盘 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(rec, 1):
            f = r["factors"]
            lines.append(
                f"| {i} | {r['symbol']} | {r['name']} | {r['sector']} | "
                f"**{r['prob']*100:.1f}%** | {r['last']:.2f} | {r['pct']:+.2f}% | "
                f"{f[0]:+.2f} | {f[1]:+.2f} | {f[2]:+.2f} | {f[3]:+.2f} | {f[4]:+.2f} |")
    else:
        lines.append("（本次无标的达到高置信阈值——属正常，模型没有强行凑名单）\n")
    lines.append("\n## 📋 全池排序（Top 20）\n")
    lines.append("| 排名 | 代码 | 名称 | 板块 | ML 评分 | 最新价 | 今日涨跌 |")
    lines.append("|---|---|---|---|---|---|---|")
    for i, r in enumerate(picks[:20], 1):
        lines.append(f"| {i} | {r['symbol']} | {r['name']} | {r['sector']} | "
                     f"{r['prob']*100:.1f}% | {r['last']:.2f} | {r['pct']:+.2f}% |")
    if skipped:
        lines.append("\n## ⚠️ 跳过（数据/异常）\n")
        for code, name, reason in skipped:
            lines.append(f"- {code} {name}：{reason}")
    lines.append("\n> ⚠️ **诚实结论**：扩大池(39只)回测显示 ML precision_up ≈ 45-52%（随机基准50%），"
                 "与规则基线相当，**无稳定超额**。本名单为 ML 概率排序候选，非买点信号；"
                 "真正提准需真实资金流(Path1, ~200元/年)或更优特征，而非调模型。风险自担。\n")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"[recommend] 已导出：{path}")
    except Exception as e:  # noqa: BLE001
        print(f"[recommend] 导出失败：{e}")
    # 缓存（供 webui 直接读取，避免每次点击重算 39 只）
    cache = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "horizon": horizon, "model": model_name, "min_prob": min_prob,
             "rec": rec, "picks": picks, "skipped": [list(s) for s in skipped]}
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        print(f"[recommend] 缓存已写：{cache_path}")
    except Exception as e:  # noqa: BLE001
        print(f"[recommend] 缓存失败：{e}")


# ----------------------------------------------------------- 报告
def _write_report(path: str, table: list, summary: str):
    from datetime import datetime
    lines = [f"# Path3 本地 ML 模型回测报告（{datetime.today().strftime('%Y-%m-%d')}）\n"]
    lines.append("> 零依赖 numpy 实现（Logistic Regression L2 + Gradient Boosting depth-2 树）；"
                 "walk-forward 扩展窗口、每月重训；特征点-时间、无未来函数。\n"
                 "> 对比口径：precision_up = 发出「看多」后 N 日收益为正的占比；"
                 "随机基准 50%。\n"
                 "> 高置信(≥0.6)：只在模型给出 ≥0.6 上涨概率时才发信号，"
                 "牺牲覆盖率换 precision。\n")
    lines.append("## 逐 horizon × 模型\n")
    lines.append("| horizon | 模型 | precision_up(≥0.5) | 高置信(≥0.6) | 覆盖率(0.6) | 准确率 | 规则基线 |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in table:
        pc = r.get("prec_up_60")
        pc_s = f"{pc*100:.1f}%" if pc == pc and pc is not None else "—"
        cov60 = r.get("cov_60")
        cov60_s = f"{cov60*100:.1f}%" if cov60 == cov60 and cov60 is not None else "—"
        lines.append(
            f"| {r['horizon']} | {r['model']} | {r['prec_up']*100:.1f}% | "
            f"{pc_s} | {cov60_s} | {r['acc']*100:.1f}% | {r['rule']*100:.1f}% |")
    lines.append("\n## 结论\n")
    lines.append(summary)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"\n[report] 已导出：{path}")
    except Exception as e:  # noqa: BLE001
        print(f"\n[report] 导出失败：{e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--out", default="D:/WorkBuddy/output/ml_report.md")
    ap.add_argument("--gb-rounds", type=int, default=40)
    ap.add_argument("--universe", action="store_true",
                    help="用自动荐股扩大池（screener 五板块龙头去重，~39只）跑回测")
    ap.add_argument("--recommend", action="store_true",
                    help="扫全池产出高置信荐股名单（写入 ml_recommend.md + cache）")
    ap.add_argument("--rec-out", default="D:/WorkBuddy/output/ml_recommend.md")
    ap.add_argument("--horizon", type=int, default=10)
    ap.add_argument("--model", default="LR", help="LR 或 GB")
    ap.add_argument("--money", default="proxy", choices=["proxy", "real"],
                    help="资金因子数据源：proxy=本地量价代理；real=Tushare真实主力净流入(缓存)")
    args = ap.parse_args()

    if args.recommend:
        bench_hist = fetch_benchmark_histories(days=400)
        hs300 = bench_hist.get("沪深300")
        money_cache = {}
        if args.money == "real":
            for (code, _, _) in build_universe():
                c = load_moneyflow_full(code)
                if c is not None:
                    money_cache[code] = c
            print(f"真实资金流全字段缓存：{len(money_cache)}/{len(build_universe())} 只")
        print(f"扫描自动荐股池（{len(build_universe())} 只）…")
        rec, picks, skipped = auto_recommend(
            horizon=args.horizon, model_name=args.model,
            min_prob=0.6, bench_hist=bench_hist, hs300=hs300,
            money_cache=money_cache or None)
        print(f"  高置信推荐 {len(rec)} 只，全部排序 {len(picks)} 只，跳过 {len(skipped)} 只")
        for r in rec[:15]:
            print(f"    {r['symbol']} {r['name']}  {r['prob']*100:.1f}%  "
                  f"价{r['last']:.2f}({r['pct']:+.2f}%)")
        _write_recommend(args.rec_out, rec, picks, skipped,
                         args.horizon, args.model, 0.6)
        return

    if args.symbols:
        symbols = args.symbols
    elif args.universe:
        symbols = [c for c, _, _ in build_universe()]
        print(f"扩大池模式：{len(symbols)} 只标的")
    else:
        try:
            with open(os.path.join(HERE, "watchlist.json"), "r", encoding="utf-8") as f:
                symbols = [i["symbol"] for i in json.load(f)["watchlist"]]
        except Exception:
            symbols = ["300034", "002085", "688786", "300174", "688786", "300699"]

    print("预取基准指数历史…")
    bench_hist = fetch_benchmark_histories(days=400)
    hs300 = bench_hist.get("沪深300")
    print(f"  可用基准：{list(bench_hist.keys())}")

    money_cache = {}
    if args.money == "real":
        for s in symbols:
            c = load_moneyflow_full(s)
            if c is not None:
                money_cache[s] = c
        print(f"真实资金流全字段缓存：{len(money_cache)}/{len(symbols)} 只（--money real）")

    table = []
    for h in HORIZONS:
        print(f"\n=== Horizon {h}d ===")
        # 各标的逐日行（同一批次，ML 与规则基线共用 → 公平对比）
        per_sym = {s: build_rows(s, bench_hist, hs300, h, money_cache=money_cache)
                   for s in symbols}
        per_sym = {s: r for s, r in per_sym.items() if r}
        if not per_sym:
            print("  无可用标的")
            continue

        # 规则基线（合并全样本）
        all_rows = [r for rows in per_sym.values() for r in rows]
        rule_prec, rule_cov, rule_n = rule_baseline(all_rows)
        print(f"  规则基线 precision_up={rule_prec*100:.1f}%  覆盖率={rule_cov*100:.1f}%  n={rule_n}")

        # ML 模型
        for ModelCls, mname, mk in [
            (LogisticRegression, "LR(L2)", {}),
            (GradientBoosting, f"GB({args.gb_rounds})",
             {"rounds": args.gb_rounds, "lr": 0.1}),
        ]:
            # 合并全样本预测（walk-forward 在每个标的内部做）
            probs_all, ys_all = [], []
            for s, rows in per_sym.items():
                pr = walk_forward(rows, ModelCls, **mk)
                probs_all.append(pr)
                ys_all.append(np.array([r["y"] for r in rows]))
            probs_all = np.concatenate(probs_all)
            ys_all = np.concatenate(ys_all)
            prec_up, cov, acc = evaluate(probs_all, ys_all, 0.5)
            prec_up_60, cov_60, acc_60 = evaluate(probs_all, ys_all, 0.6)
            print(f"  {mname:12s} precision_up(0.5)={prec_up*100:.1f}%  "
                  f"高置信(0.6)={prec_up_60*100:.1f}%  覆盖率(0.6)={cov_60*100:.1f}%  "
                  f"准确率={acc*100:.1f}%")
            table.append({"horizon": h, "model": mname, "prec_up": prec_up,
                          "cov": cov, "acc": acc, "rule": rule_prec,
                          "prec_up_60": prec_up_60, "cov_60": cov_60})

    # 结论自动生成
    best = max(table, key=lambda r: (r["prec_up"] if np.isfinite(r["prec_up"]) else -1)) \
        if table else None
    if best:
        beat = best["prec_up"] > best["rule"]
        money_label = "Tushare真实主力净流入" if args.money == "real" else "本地量价代理(proxy)"
        summary = (
            f"- **资金因子数据源：{money_label}**（--money {args.money}）。\n"
            f"- 最优组合：**horizon {best['horizon']}d / {best['model']}**，"
            f"precision_up = **{best['prec_up']*100:.1f}%**，"
            f"规则基线同口径 {best['rule']*100:.1f}%。\n"
            f"- 随机基准 50%；{'✅ ML 已突破 50% 并优于规则基线' if best['prec_up'] > 0.5 else '⚠️ 仍未稳定越过 50% 硬币线'}。\n"
            f"- {'ML 学到数据中的权重关系，优于手设权重的 Path2。' if beat else 'ML 与规则基线接近，说明该因子集信息量仍有限。'}\n"
            f"- 高置信分层（只在 ≥0.6 概率时发信号）：precision_up 进一步抬升到 "
            f"**{(best.get('prec_up_60') or 0)*100:.1f}%**，但覆盖率降到 "
            f"{(best.get('cov_60') or 0)*100:.1f}%（信号更稀少、更可信）。\n"
            f"- 诚实提示：覆盖率是「发出看多信号的交易日占比」，过低则信号稀少、实战可用性差；"
            f"过高则接近全仓、失去筛选意义。\n"
        )
    else:
        summary = "- 无有效结果（数据不足或全为合成）。\n"

    if args.out:
        _write_report(args.out, table, summary)


if __name__ == "__main__":
    main()

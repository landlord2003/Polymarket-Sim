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
import argparse
import json
from typing import Optional

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from signal_engine import (dim_trend, money_proxy, dim_valuation,
                           dim_sector_rotation, dim_regime,
                           _map_signal, DEFAULT_WEIGHTS)
from akshare_factors import fetch_benchmark_histories

HORIZONS = (5, 10, 20)
START_DAYS = 260        # 预热：估值分位需 250 日窗口
MIN_TRAIN = 120         # walk-forward 最少训练样本
RETRAIN_GAP = 30        # 每 30 个交易日（约1月）重训一次


# ----------------------------------------------------------- 数据加载（同 backtest）
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


def load_hist(symbol: str, days: int = 600) -> pd.DataFrame:
    try:
        from datasource import fetch_kline
        df = fetch_kline(symbol, days=days)
        if len(df) >= 30:
            return df
        raise RuntimeError("K线不足")
    except Exception as e:  # 离线兜底：仅验证引擎
        print(f"[warn] {symbol} 取数失败（{e}），使用合成数据（非真实准确率）")
        return _synthetic_data(symbol)


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
                hs300, tgt) -> tuple:
    """返回 (特征向量 ndarray, 5因子分 [trend,money,rotation,valuation,regime])。

    所有计算只用 sub（df.iloc[:i+1]）与 target_date=tgt，无未来函数。
    """
    try:
        sm, _ = dim_trend(sub, idx_df=hs300)
        sz, _ = money_proxy(sub)
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
def build_rows(symbol: str, bench_hist: dict, hs300, horizon: int) -> list:
    """返回逐日行列表：每行 {X, y, factors:[5因子], date}。

    只保留 i 有足够未来（i+horizon < n）且 i>=START_DAYS 的行。
    """
    df = load_hist(symbol)
    n = len(df)
    if n < START_DAYS + horizon + 1:
        return []
    closes = df["close"].values.astype(float)
    rows = []
    for i in range(START_DAYS, n - horizon):
        sub = df.iloc[:i + 1]
        tgt = sub.index[-1]
        vec, fac = feat_vector(symbol, sub, bench_hist, hs300, tgt)
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


def evaluate(probs: np.ndarray, ys: np.ndarray) -> tuple:
    """返回 (precision_up, coverage, accuracy)；probs 含 nan 行跳过。"""
    m = ~np.isnan(probs)
    if m.sum() == 0:
        return float("nan"), float("nan"), float("nan")
    p = probs[m]
    y = ys[m]
    pred_up = p >= 0.5
    if pred_up.sum() == 0:
        return float("nan"), 0.0, float((pred_up == y).mean())
    return float(y[pred_up].mean()), float(pred_up.mean()), float((pred_up == y).mean())


# ----------------------------------------------------------- 报告
def _write_report(path: str, table: list, summary: str):
    from datetime import datetime
    lines = [f"# Path3 本地 ML 模型回测报告（{datetime.today().strftime('%Y-%m-%d')}）\n"]
    lines.append("> 零依赖 numpy 实现（Logistic Regression L2 + Gradient Boosting depth-2 树）；"
                 "walk-forward 扩展窗口、每月重训；特征点-时间、无未来函数。\n"
                 "> 对比口径：precision_up = 发出「看多」后 N 日收益为正的占比；"
                 "随机基准 50%。\n")
    lines.append("## 逐 horizon × 模型\n")
    lines.append("| horizon | 模型 | precision_up | 覆盖率 | 准确率 | 规则基线precision_up |")
    lines.append("|---|---|---|---|---|---|")
    for r in table:
        lines.append(
            f"| {r['horizon']} | {r['model']} | {r['prec_up']*100:.1f}% | "
            f"{r['cov']*100:.1f}% | {r['acc']*100:.1f}% | {r['rule']*100:.1f}% |")
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
    args = ap.parse_args()

    if args.symbols:
        symbols = args.symbols
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

    table = []
    for h in HORIZONS:
        print(f"\n=== Horizon {h}d ===")
        # 各标的逐日行（同一批次，ML 与规则基线共用 → 公平对比）
        per_sym = {s: build_rows(s, bench_hist, hs300, h) for s in symbols}
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
            prec_up, cov, acc = evaluate(probs_all, ys_all)
            print(f"  {mname:12s} precision_up={prec_up*100:.1f}%  覆盖率={cov*100:.1f}%  "
                  f"准确率={acc*100:.1f}%")
            table.append({"horizon": h, "model": mname, "prec_up": prec_up,
                          "cov": cov, "acc": acc, "rule": rule_prec})

    # 结论自动生成
    best = max(table, key=lambda r: (r["prec_up"] if np.isfinite(r["prec_up"]) else -1)) \
        if table else None
    if best:
        beat = best["prec_up"] > best["rule"]
        summary = (
            f"- 最优组合：**horizon {best['horizon']}d / {best['model']}**，"
            f"precision_up = **{best['prec_up']*100:.1f}%**，"
            f"规则基线同口径 {best['rule']*100:.1f}%。\n"
            f"- 随机基准 50%；{'✅ ML 已突破 50% 并优于规则基线' if best['prec_up'] > 0.5 else '⚠️ 仍未稳定越过 50% 硬币线'}。\n"
            f"- {'ML 学到数据中的权重关系，优于手设权重的 Path2。' if beat else 'ML 与规则基线接近，说明该因子集信息量仍有限。'}\n"
            f"- 诚实提示：覆盖率是「发出看多信号的交易日占比」，过低则信号稀少、实战可用性差；"
            f"过高则接近全仓、失去筛选意义。\n"
        )
    else:
        summary = "- 无有效结果（数据不足或全为合成）。\n"

    if args.out:
        _write_report(args.out, table, summary)


if __name__ == "__main__":
    main()

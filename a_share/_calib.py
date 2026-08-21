"""校准实测：模型自报概率 vs 真实上涨率（分箱）。
直接回答"自动荐股里的百分率准不准"。
"""
import numpy as np
import ml_model as M
from signal_engine import load_moneyflow_full

MODE = "proxy"  # proxy 与 real 差异<0.5pt，校准结论一致，用 proxy 加速


def run():
    symbols = [c for c, _, _ in M.build_universe()]
    allp, ally = [], []
    for i, s in enumerate(symbols):
        mc = {} if MODE == "proxy" else {s: load_moneyflow_full(s)}
        rows = M.build_rows(s, {}, None, 10, money_cache=mc)
        if not rows:
            continue
        pr = M.walk_forward(rows, M.LogisticRegression)
        y = np.array([r["y"] for r in rows])
        m = ~np.isnan(pr)
        allp.append(pr[m])
        ally.append(y[m])
        if (i + 1) % 10 == 0:
            print(f"  ...{i+1}/{len(symbols)}")
    P = np.concatenate(allp)
    Y = np.concatenate(ally)
    print(f"\n总样本: {len(P)}  整体真实上涨率: {Y.mean()*100:.1f}%\n")
    print(f"{'预测概率档':>12} | {'样本数':>7} | {'真实上涨率':>10} | {'偏差(预测-真实)':>14}")
    print("-" * 56)
    edges = [0.0, 0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 1.01]
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (P >= lo) & (P < hi)
        n = int(mask.sum())
        if n == 0:
            print(f"[{lo:.2f},{hi:.2f}) | {n:>7} | {'—':>10} | {'—':>14}")
            continue
        emp = Y[mask].mean()
        mid = (lo + hi) / 2
        print(f"[{lo:.2f},{hi:.2f}) | {n:>7} | {emp*100:>8.1f}% | {(mid-emp)*100:>+12.1f}pt")
    # 高置信档
    for thr in (0.55, 0.6, 0.65):
        mask = P >= thr
        n = int(mask.sum())
        emp = Y[mask].mean() if n else float("nan")
        print(f"\n高置信 ≥{thr:.2f}: n={n}  真实上涨率={emp*100:.1f}%")


if __name__ == "__main__":
    run()

# -*- coding: utf-8 -*-
"""P0-4 离线 walk-forward 回测：用真实盘口时序验证做市策略有效性。

读取 data/quotes_ts/*.jsonl 快照序列（按 ts 排序），对每个市场构造中间价时间序列，
用「横截面信息系数(IC) walk-forward」验证策略在真实盘口上是否存在可复现的 edge：

  信号：上一期收益 r_{i-1}（横截面，按 token）
  目标：本期收益 r_i
  IC_i = Spearman(信号, 目标) 跨所有同时有报价的 token
  train/oos 分窗求 IC 均值与 t 统计，按 train_IC vs oos_IC 范式判定：
    oos IC 均值符号与 train 一致、且 |t| 达到阈值 → PASS（真实盘口存在可复现结构）
    否则 → PENDING（继续累积数据或复核策略）

诚实边界：
- 盘口秒/分钟级极稳定，需足够时间跨度才有统计意义；快照/样本不足时明确报错，不伪造成功。
- 「未达标前不宣告研究完成」：oos 段 IC 需显著非零且与 train 同号才判 PASS。
- 早期 naive 的 pair_pnl（(ya1-yb0)+(ya0-yb1)）在固定价差下恒为 +2×spread，存在向上偏置，
  已弃用为判定指标，仅保留作参考函数。
"""
from __future__ import annotations
import os
import json
import glob
import math
import statistics

import compliance as C

QUOTES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "quotes_ts")


def load_snapshots():
    """读全部 jsonl 快照（含实时 quotes_* 与回填 quotes_backfill_*），按 ts 升序。"""
    files = sorted(glob.glob(os.path.join(QUOTES_DIR, "quotes_*.jsonl")))
    snaps = []
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        snaps.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            continue
    snaps.sort(key=lambda s: s.get("ts", 0))
    return snaps


def pair_pnl(s0, s1):
    """[参考，非判定指标] 相邻两快照对称双边一期收益（固定价差下恒为 +2×spread，向上偏置）。

    买腿：t 买@yb0，t+1 卖@ya1 -> ya1-yb0
    卖腿：t 卖@ya0，t+1 买@yb1 -> ya0-yb1
    净 = (ya1-yb0)+(ya0-yb1)
    """
    m0 = {m["token_id"]: m for m in s0.get("markets", []) if m.get("token_id")}
    m1 = {m["token_id"]: m for m in s1.get("markets", []) if m.get("token_id")}
    out = []
    for tid, a in m0.items():
        b = m1.get(tid)
        if not b:
            continue
        yb0, ya0 = a.get("yes_bid"), a.get("yes_ask")
        yb1, ya1 = b.get("yes_bid"), b.get("yes_ask")
        if None in (yb0, ya0, yb1, ya1):
            continue
        out.append((ya1 - yb0) + (ya0 - yb1))
    return out


def _rank(x):
    order = sorted(range(len(x)), key=lambda i: x[i])
    r = [0.0] * len(x)
    for pos, i in enumerate(order):
        r[i] = pos + 1.0
    return r


def _spearman(a, b):
    """两列等长的 Spearman 秩相关（剔除 None 对）。样本<5 返回 None。"""
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    n = len(pairs)
    if n < 5:
        return None
    ra = _rank([p[0] for p in pairs])
    rb = _rank([p[1] for p in pairs])
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((x - mb) ** 2 for x in rb)
    if va == 0 or vb == 0:
        return 0.0
    return cov / math.sqrt(va * vb)


def _build_token_series(snaps):
    """返回 (ts_list, {token_id: [mid_or_None,...]})，按全局有序 ts 对齐。
    合规红线：剔除政治/地缘/军事等敏感市场（含实时 quotes 快照里未过滤的部分）。"""
    ts_all = sorted({s["ts"] for s in snaps if s.get("ts") is not None})
    idx = {t: i for i, t in enumerate(ts_all)}
    series = {}
    dropped = 0
    for s in snaps:
        t = s.get("ts")
        if t is None or t not in idx:
            continue
        i = idx[t]
        for m in s.get("markets", []):
            tid = m.get("token_id")
            mid = m.get("mid")
            if tid is None or mid is None:
                continue
            if C.is_blocked(m.get("question", ""), m.get("tag")):
                dropped += 1
                continue
            series.setdefault(tid, [None] * len(ts_all))[i] = float(mid)
    return ts_all, series, dropped


def walk_forward(train_frac=0.5, min_tokens=5, min_periods=5):
    """横截面 IC walk-forward：验证真实盘口是否存在可复现收益结构。

    判定：oos IC 均值与 train 同号，且 |t| >= 1.5（oos 样本内显著非零）。
    """
    snaps = load_snapshots()
    if len(snaps) < min_periods:
        return {"error": "快照不足（需>=%d），盘口序列累积中，暂无法统计" % min_periods,
                "n_snaps": len(snaps)}
    ts_all, series, dropped = _build_token_series(snaps)
    L = len(ts_all)
    if L < 3:
        return {"error": "有效时间序列过短（%d 个时间点）" % L, "n_snaps": len(snaps)}

    # 逐期收益 r_i = mid_{i+1} - mid_i（跨 token）
    ics = []  # IC_i：用 r_{i-1} 预测 r_i
    for i in range(1, L - 1):
        tids = [tid for tid, v in series.items()
                if v[i - 1] is not None and v[i] is not None and v[i + 1] is not None]
        if len(tids) < min_tokens:
            ics.append(None)
            continue
        sig = [series[tid][i - 1] - series[tid][i] for tid in tids]   # r_{i-1}
        tgt = [series[tid][i] - series[tid][i + 1] for tid in tids]   # r_i
        ic = _spearman(sig, tgt)
        ics.append(ic)

    valid = [x for x in ics if x is not None]
    if len(valid) < min_periods:
        return {"error": "有效 IC 期数不足（%d），需>=%d" % (len(valid), min_periods),
                "n_snaps": len(snaps), "n_tokens": len(series)}

    n = len(valid)
    k = max(2, int(n * train_frac))
    train, oos = valid[:k], valid[k:]

    def _agg(xs):
        m = statistics.mean(xs)
        sd = statistics.pstdev(xs) if len(xs) > 1 else 0.0
        se = sd / math.sqrt(len(xs)) if len(xs) > 1 else 0.0
        t = (m / se) if se > 0 else 0.0
        return {"n": len(xs), "ic_mean": round(m, 4),
                "ic_std": round(sd, 4), "se": round(se, 4), "t": round(t, 2)}

    tr, oo = _agg(train), _agg(oos)
    same_sign = (tr["ic_mean"] * oo["ic_mean"]) > 0
    passed = same_sign and abs(oo["t"]) >= 1.5
    return {
        "n_snaps": len(snaps),
        "n_tokens": len(series),
        "n_periods": n,
        "compliance_dropped": dropped,
        "train": tr,
        "oos": oo,
        "oos_same_sign_as_train": same_sign,
        "verdict": ("PASS（oos IC 均值与 train 同号且 |t|>=1.5，真实盘口存在可复现结构）"
                    if passed else
                    "PENDING（oos IC 未达显著阈值或符号不稳，继续累积数据/复核策略）"),
    }


if __name__ == "__main__":
    import pprint
    pprint.pprint(walk_forward())

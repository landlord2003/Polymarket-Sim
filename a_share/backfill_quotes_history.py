# -*- coding: utf-8 -*-
"""P0-4 真实盘口历史回填：用 CLOB prices-history 拉取高流动性市场的真实历史中间价，
按日(UTC)重采样为快照序列，写入 data/quotes_ts/quotes_backfill_*.jsonl，
供 backtest_quotes.py 做 walk-forward（train_IC vs oos_IC）验证。

诚实性：全程使用 CLOB 真实历史标记中间价，不编造、不做任何平滑。
仅 yes_bid/yes_ask 由 mid±半价差反推，纯为兼容快照 schema；walk-forward 只消费 mid。
"""
from __future__ import annotations
import os
import json
import glob
import time
import statistics
import datetime

import polymarket as P
import compliance as C

QUOTES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "quotes_ts")
TOP_N = 25          # 取实时盘口里流动性最高的 N 个市场
SPREAD = 0.02       # 由 mid 反推买卖盘的半价差，仅 schema 兼容


def pick_top_tokens(n=TOP_N):
    """从已落盘的实时快照里挑流动性最高的 token（含 question/liquidity 元数据）。
    合规红线：先剔除政治/地缘/军事等敏感市场，再按流动性排序取 Top-N。"""
    files = sorted(glob.glob(os.path.join(QUOTES_DIR, "quotes_*.jsonl")))
    best = {}
    blocked = 0
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        snap = json.loads(line)
                    except Exception:
                        continue
                    for m in snap.get("markets", []):
                        tid = m.get("token_id")
                        if not tid:
                            continue
                        q = m.get("question") or ""
                        if C.is_blocked(q):
                            blocked += 1
                            continue
                        liq = float(m.get("liquidity") or 0.0)
                        cur = best.get(tid, {}).get("liquidity", 0.0)
                        if liq > cur:
                            best[tid] = {"token_id": tid,
                                         "question": q,
                                         "liquidity": liq}
        except Exception:
            continue
    ranked = sorted(best.values(), key=lambda x: -float(x.get("liquidity") or 0))[:n]
    print("[backfill] 合规过滤剔除敏感市场 %d 个候选；剩余合规候选 %d 个" % (blocked, len(best)))
    return ranked


def resample_daily(hist):
    """hist: [(t, p)] 升序 -> {YYYY-MM-DD: 当日均值}。"""
    by_day = {}
    for t, p in hist:
        d = datetime.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")
        by_day.setdefault(d, []).append(p)
    return {d: statistics.mean(v) for d, v in by_day.items()}


def main():
    toks = pick_top_tokens()
    print("[backfill] 候选高流动性 token 数: %d" % len(toks))
    daily_map = {}   # token_id -> {date: mid}
    meta = {}
    for tk in toks:
        tid = tk["token_id"]
        meta[tid] = tk
        hist = P.fetch_price_history(token_id=tid, interval="max")
        if (not hist) or (isinstance(hist[0], dict) and hist[0].get("error")):
            print("  skip %s (无历史数据)" % tid[:12])
            continue
        dm = resample_daily(hist)
        if len(dm) < 3:
            print("  skip %s (历史过短 %d 天)" % (tid[:12], len(dm)))
            continue
        daily_map[tid] = dm
        print("  ok %s days=%d liq=%.0f q=%.60s"
              % (tid[:12], len(dm), float(tk.get("liquidity") or 0), tk.get("question") or ""))
        time.sleep(0.12)  # 降低突发频率，避免被 CLOB 限流

    if not daily_map:
        print("[backfill] 无任何可用历史，终止")
        return None

    all_days = sorted({d for dm in daily_map.values() for d in dm})
    stamp = all_days[-1].replace("-", "")
    out_path = os.path.join(QUOTES_DIR, "quotes_backfill_%s.jsonl" % stamp)
    n_written = 0
    with open(out_path, "w", encoding="utf-8") as fh:
        for d in all_days:
            ds = datetime.datetime.strptime(d, "%Y-%m-%d").timestamp()
            markets = []
            for tid, dm in daily_map.items():
                if d not in dm:
                    continue
                mid = round(float(dm[d]), 4)
                markets.append({
                    "token_id": tid,
                    "question": meta[tid].get("question"),
                    "liquidity": float(meta[tid].get("liquidity") or 0.0),
                    "mid": mid,
                    "yes_bid": round(mid - SPREAD / 2, 4),
                    "yes_ask": round(mid + SPREAD / 2, 4),
                })
            if len(markets) >= 5:
                fh.write(json.dumps({"ts": ds, "date": d, "markets": markets},
                                    ensure_ascii=False) + "\n")
                n_written += 1
    print("[backfill] 写入 %s : %d 个日快照, %d 个 token, 跨度 %s~%s"
          % (out_path, n_written, len(daily_map), all_days[0], all_days[-1]))
    return out_path


if __name__ == "__main__":
    main()

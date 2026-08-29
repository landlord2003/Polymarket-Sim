# -*- coding: utf-8 -*-
"""MM 参数扫描（四维）：mm × adv × tick × size 蒙特卡洛轮次回放。

在真实 RigorVirtualBook.market_make / model_fill 摩擦模型上复现生产成交路径：
  每轮 = 先买(建仓)后卖(对冲)，measure 锁利 PnL（与 production 一致）。

四维含义：
- mm   (mm_min_spread)   : 策略接单的最小价差门槛（只接 s>=mm 的市场）
- adv  (adverse_frac)    : 对冲基准价不利漂移占价差比例（买腿吃完整价差后取得正期望）
- tick (rigor.tick)      : 订单簿档步（走簿滑点模型档距；真实 Polymarket 为 0.001/0.002/0.005/0.01）
- size (order size)      : 单笔份额（决定走簿档数；size>顶档深度才触发走簿滑点）

假设（与历史扫描一致）：
- 价差 s ~ 右偏: s = 0.004 + 0.056 * Beta(1.6, 4.5)
- 价格 p ~ 均匀[0.2,0.8]
- 流动性 L ~ 从真实日志197笔重采样
- depth_frac 固定 0.01；fee_rate 固定 0.01
- 单市场 max_skew 提到 300 以上以允许 size 维度扫描（生产真实上限 300，size 封顶 300 保持合规）

输出：mm_sweep4d_results.json（含每组合 EV / win% / 95% CI）+ 控制台摘要。
"""
import os, sys, json, math, random, statistics as st
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))
from sim_rigor import RigorVirtualBook, rigor_params_from_config

# 校准流动性分布（同历史扫描）
import glob
LIQ = []
for f in sorted(glob.glob(os.path.join(_HERE, "sim_logs", "trades_2026*.jsonl"))):
    for line in open(f, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("kind") == "mm" and r.get("liquidity"):
            LIQ.append(float(r["liquidity"]))
if not LIQ:
    LIQ = [18443] * 50

random.seed(20260830)


def beta(a, b):
    while True:
        x = random.random()
        y = random.random()
        f = (x ** (a - 1)) * ((1 - x) ** (b - 1))
        g = (0.5 ** (a - 1)) * (0.5 ** (b - 1))
        if y * g <= f:
            return x


def sample_spread():
    return 0.004 + 0.056 * beta(1.6, 4.5)


def sample_price():
    return round(random.uniform(0.2, 0.8), 3)


def sample_liq():
    return random.choice(LIQ)


def mk_opp(s, p, L, tick):
    # 真实盘口顶边按 tick 量化，使 tick 维度真实影响可锁价差
    half = round(s / 2.0 / tick) * tick
    bid = max(0.02, round((p - half) / tick) * tick)
    ask = min(0.98, round((p + half) / tick) * tick)
    if ask <= bid:
        ask = min(0.98, bid + tick)
    return {"buy_ask": bid, "sell_bid": ask, "liquidity": L,
            "buy_id": "MKT1", "sell_id": "MKT1", "question": "test",
            "end_date": None, "buy_venue": "poly", "sell_venue": "poly"}


def get_book(rigor):
    b = RigorVirtualBook(rigor=rigor)
    b._save = lambda: None                 # 扫描不落盘，提速
    b._record_volume = lambda *a, **k: None  # 禁日上限文件 I/O，提速
    b.max_skew = 3000                      # 允许 size 维度扫描（生产真实上限 300）
    return b


def reset(book):
    book.cash = 10000.0
    book.inventory = {}
    book.avg_cost = {}
    book.realized_pnl = 0.0
    book.positions = []
    book.daily_caps = {}
    book.last_mid = {}


def trial(rigor, mm_min_spread, tick, size, book):
    s = sample_spread()
    if s < mm_min_spread:
        return None
    p = sample_price()
    L = sample_liq()
    opp = mk_opp(s, p, L, tick)
    reset(book)
    r1 = book.market_make(opp, size)
    if not r1.get("ok"):
        return 0.0
    r2 = book.market_make(opp, size)
    return book.realized_pnl


def run_grid(mm_min_spread, adverse, tick, size, n=800):
    rigor = dict(rigor_params_from_config())
    rigor["adverse_frac"] = adverse
    rigor["tick"] = tick
    rigor["depth_frac"] = 0.01
    rigor["daily_cap_notional"] = 1e18     # 扫描内不门控（真实上限由 max_skew 体现）
    book = get_book(rigor)
    pnls = []
    for _ in range(n):
        v = trial(rigor, mm_min_spread, tick, size, book)
        if v is not None:
            pnls.append(v)
    if not pnls:
        return None
    ev = st.mean(pnls)
    sd = st.pstdev(pnls)
    se = sd / math.sqrt(len(pnls))
    z = 1.959963985
    win = sum(1 for x in pnls if x > 0) / len(pnls)
    return {
        "n": len(pnls), "ev": ev, "sd": sd, "se": se,
        "ci_low": ev - z * se, "ci_high": ev + z * se, "ci_half": z * se,
        "win": win, "pnl_min": min(pnls), "pnl_max": max(pnls),
        "med": st.median(pnls),
    }


def main():
    import traceback
    MM = [0.004, 0.008, 0.01, 0.012, 0.015, 0.02, 0.03]
    ADV = [0.05, 0.10, 0.15, 0.20, 0.30]
    TICK = [0.001, 0.002, 0.005, 0.01]
    SIZE = [50, 100, 200, 300]          # 封顶 300 = 生产真实 max_skew 上限
    N = int(os.environ.get("SWEEP_N", "800"))
    results = {}
    total = len(MM) * len(ADV) * len(TICK) * len(SIZE)
    done = 0
    print("mm | adv | tick | size | n | EV($/rnd) | 95%CI_low | 95%CI_high | win%")
    for mm in MM:
        for adv in ADV:
            for tick in TICK:
                for size in SIZE:
                    done += 1
                    try:
                        r = run_grid(mm, adv, tick, size, n=N)
                    except Exception as e:
                        print("ERR mm=%.3f adv=%.2f tick=%.3f size=%d: %r"
                              % (mm, adv, tick, size, e))
                        traceback.print_exc()
                        continue
                    if r is None:
                        results.setdefault(str(mm), {}).setdefault(str(adv), {}) \
                               .setdefault(str(tick), {})[str(size)] = None
                        continue
                    results.setdefault(str(mm), {}).setdefault(str(adv), {}) \
                        .setdefault(str(tick), {})[str(size)] = r
                    print("%-6.3f | %.2f | %.3f | %3d | %d | %.4f | %.4f | %.4f | %.1f%%"
                          % (mm, adv, tick, size, r["n"], r["ev"],
                             r["ci_low"], r["ci_high"], r["win"] * 100))
                    sys.stdout.flush()
    with open(os.path.join(_HERE, "mm_sweep4d_results.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    # 汇总：找全正期望且 CI 下界>0 的最稳健组合
    best = None
    for mm, d1 in results.items():
        for adv, d2 in d1.items():
            for tick, d3 in d2.items():
                for size, r in d3.items():
                    if not r:
                        continue
                    if r["ci_low"] > 0 and (best is None or r["ev"] > best[1]["ev"]):
                        best = ((mm, adv, tick, size), r)
    print("\nSaved mm_sweep4d_results.json | combos=%d/%d" % (done, total))
    if best:
        k, r = best
        print("最优(ci_low>0 且 EV 最大): mm=%s adv=%s tick=%s size=%s | EV=%.4f win=%.1f%% CI[%.4f,%.4f]"
              % (k[0], k[1], k[2], k[3], r["ev"], r["win"] * 100, r["ci_low"], r["ci_high"]))


if __name__ == "__main__":
    main()

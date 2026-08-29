# -*- coding: utf-8 -*-
"""MM 参数扫描：复用真实 RigorVirtualBook.market_make / model_fill 摩擦模型，
在真实流动性分布(日志校准) + 合理价差分布上做蒙特卡洛轮次回放，找正期望参数区。

假设明示：
- 价差分布 s ~ 右偏(多数窄、少数宽)：s = 0.004 + 0.056 * Beta(1.6, 4.5)  (中位~0.012, 长尾到0.06)
- 价格 p ~ 均匀[0.2,0.8]
- 流动性 L ~ 从真实日志197笔重采样
- 策略仅接 s >= mm_min_spread 的市场
- 每轮 = 先买(建仓)后卖(对冲)，measure locked PnL（与 production 一致）
- adverse_frac 取扫网格；depth_frac 固定 0.01；fee_rate 固定 0.01
"""
import os, sys, json, random, statistics as st
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))
from sim_rigor import RigorVirtualBook, rigor_params_from_config

# 校准流动性分布
import glob
LIQ = []
for f in sorted(glob.glob(os.path.join(_HERE, "sim_logs", "trades_2026*.jsonl"))):
    for line in open(f, encoding="utf-8"):
        line = line.strip()
        if not line: continue
        try: r = json.loads(line)
        except: continue
        if r.get("kind") == "mm" and r.get("liquidity"):
            LIQ.append(float(r["liquidity"]))
if not LIQ:
    LIQ = [18443] * 50  # fallback

random.seed(20260829)

def beta(a, b):
    # 简易 Beta 采样（BB 拒绝法）
    while True:
        x = random.random()
        y = random.random()
        f = (x**(a-1)) * ((1-x)**(b-1))
        g = (0.5**(a-1)) * (0.5**(b-1))
        if y * g <= f:
            return x

def sample_spread():
    return 0.004 + 0.056 * beta(1.6, 4.5)

def sample_price():
    return round(random.uniform(0.2, 0.8), 3)

def sample_liq():
    return random.choice(LIQ)

def mk_opp(s, p, L):
    bid = max(0.02, p - s/2)
    ask = min(0.98, p + s/2)
    return {"buy_ask": bid, "sell_bid": ask, "liquidity": L,
            "buy_id": "MKT1", "sell_id": "MKT1", "question": "test",
            "end_date": None, "buy_venue": "poly", "sell_venue": "poly"}

def reset(book):
    book.cash = 10000.0
    book.inventory = {}
    book.avg_cost = {}
    book.realized_pnl = 0.0
    book.positions = []
    book.daily_caps = {}

def trial(rigor, mm_min_spread, size):
    s = sample_spread()
    if s < mm_min_spread:
        return None  # 被策略门槛拒
    p = sample_price()
    L = sample_liq()
    opp = mk_opp(s, p, L)
    book = RigorVirtualBook(rigor=rigor)
    reset(book)
    r1 = book.market_make(opp, size)
    if not r1.get("ok"):
        return 0.0  # 建仓被拒（深度/偏斜），视为 0 而非亏损
    r2 = book.market_make(opp, size)
    pnl = book.realized_pnl  # 归零锁利额
    return pnl

def run_grid(mm_min_spread, adverse, size=100, n=800):
    rigor = dict(rigor_params_from_config())
    rigor["adverse_frac"] = adverse
    rigor["depth_frac"] = 0.01
    pnls = []
    for _ in range(n):
        v = trial(rigor, mm_min_spread, size)
        if v is not None:
            pnls.append(v)
    if not pnls:
        return None
    ev = st.mean(pnls)
    win = sum(1 for x in pnls if x > 0)
    return {"n": len(pnls), "ev": ev, "win": win/len(pnls),
            "pnl_min": min(pnls), "pnl_max": max(pnls),
            "med": st.median(pnls)}

def main():
    import traceback
    try:
        results = {}
        print("mm_min_spread | adverse | n | EV($/round) | win% | pnl_min | pnl_max")
        for mm in [0.004, 0.012, 0.02, 0.025, 0.03, 0.04, 0.05]:
            for adv in [0.10, 0.20, 0.30]:
                r = run_grid(mm, adv)
                if r is None:
                    print("%-14.3f | %.2f |  (no trades)" % (mm, adv))
                    continue
                results.setdefault(str(mm), {})[str(adv)] = r
                print("%-14.3f | %.2f | %d | %.4f | %.1f%% | %.3f | %.3f"
                      % (mm, adv, r["n"], r["ev"], r["win"]*100,
                         r["pnl_min"], r["pnl_max"]))
        # 持久化供报告生成
        with open(os.path.join(_HERE, "mm_sweep_results.json"), "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print("\nSaved mm_sweep_results.json")
    except Exception:
        traceback.print_exc()

if __name__ == "__main__":
    main()

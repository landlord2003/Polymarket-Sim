"""一次性抓取 5类正交信息源到 data/alt/ 本地缓存（CSV）。

覆盖区间：2024-01 ~ 2026-08（与回测 K线区间对齐）。
抓取后由 alt_factors.py 离线构造按(股票,日期)对齐的横截面因子。
"""
import os, sys, time, calendar, traceback
import pandas as pd
import akshare as ak

HERE = os.path.dirname(os.path.abspath(__file__))
ALT = os.path.join(HERE, "data", "alt")
os.makedirs(ALT, exist_ok=True)


def retry(fn, *a, tries=3, sleep=1.0, **k):
    last = None
    for i in range(tries):
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(sleep)
            sleep *= 1.5
    print("  [retry-fail] %s: %s" % (getattr(fn, "__name__", fn), last))
    return None


def save(name, df):
    if df is None or len(df) == 0:
        print("  [skip] %s empty" % name)
        return
    path = os.path.join(ALT, name)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print("  [saved] %s  rows=%d" % (name, len(df)))


# ---- 1. 龙虎榜（全市场，按季度范围拉全）----
def fetch_lhb():
    print(">> LHB")
    quarters = [
        ("20240101", "20240331"), ("20240401", "20240630"), ("20240701", "20240930"),
        ("20241001", "20241231"), ("20250101", "20250331"), ("20250401", "20250630"),
        ("20250701", "20250930"), ("20251001", "20251231"), ("20260101", "20260331"),
        ("20260401", "20260821"),
    ]
    frames = []
    for s, e in quarters:
        df = retry(ak.stock_lhb_detail_em, start_date=s, end_date=e)
        if df is not None and len(df):
            frames.append(df)
        time.sleep(0.5)
    save("lhb_detail.csv", pd.concat(frames, ignore_index=True) if frames else None)


# ---- 2. 业绩报表（按报告期循环，全市场）----
def fetch_yjbb():
    print(">> YJBB")
    periods = ["20240331", "20240630", "20240930", "20241231",
               "20250331", "20250630", "20250930", "20251231", "20260331"]
    frames = []
    for p in periods:
        df = retry(ak.stock_yjbb_em, date=p)
        if df is not None and len(df):
            df = df.copy()
            df["报告期"] = p
            frames.append(df)
        time.sleep(0.5)
    save("yjbb.csv", pd.concat(frames, ignore_index=True) if frames else None)


# ---- 3. 分析师评级（按月末发布日循环，全市场）----
def month_ends(start="2024-01", end="2026-08"):
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    res = []
    while (y, m) <= (ey, em):
        ld = calendar.monthrange(y, m)[1]
        res.append("%d%02d%02d" % (y, m, ld))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return res


def fetch_analyst():
    print(">> ANALYST")
    frames = []
    for d in month_ends("2024-01", "2026-08"):
        df = retry(ak.stock_rank_forecast_cninfo, date=d)
        if df is not None and len(df):
            frames.append(df)
        time.sleep(0.5)
    save("analyst.csv", pd.concat(frames, ignore_index=True) if frames else None)


# ---- 4. 事件：回购 / 增减持 / 解禁 ----
def fetch_events():
    print(">> REPURCHASE")
    save("repurchase.csv", retry(ak.stock_repurchase_em))
    print(">> GGCF (增减持, 全市场)")
    save("ggcg.csv", retry(ak.stock_ggcg_em, symbol="全部"))

    print(">> RESTRICT (解禁, 按39只)")
    sys.path.insert(0, HERE)
    import ml_model as M
    syms = [s for (s, _, _) in M.build_universe()]
    frames = []
    for s in syms:
        df = retry(ak.stock_restricted_release_queue_em, symbol=s)
        if df is not None and len(df):
            df = df.copy()
            df["代码"] = s
            frames.append(df)
        time.sleep(0.3)
    save("restrict.csv", pd.concat(frames, ignore_index=True) if frames else None)


# ---- 5. 北向（市场级，一次全历史）----
def fetch_north():
    print(">> NORTH")
    save("north_hist.csv", retry(ak.stock_hsgt_hist_em, symbol="北向资金"))


if __name__ == "__main__":
    print("=== fetch alt factors (2024-01 ~ 2026-08) ===")
    fetch_lhb()
    fetch_yjbb()
    fetch_analyst()
    fetch_events()
    fetch_north()
    print("=== ALL DONE ===")

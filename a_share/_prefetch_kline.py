"""预抓取 K 线缓存：直接覆盖写 data/cache/kline_<sym>.csv（不删除旧文件，
避免触发沙箱安全删除拦截）。用于把历史拉长到 days（如 2400）后再跑 _xsec。

用法：
  python _prefetch_kline.py --universe core --days 2400
  python _prefetch_kline.py --universe extended --days 2400
"""
import os
import sys
import argparse
import ml_model as M

HERE = M.HERE


def prefetch(codes, days):
    ok, fail = 0, []
    for i, (s, name, sector) in enumerate(codes):
        p = os.path.join(HERE, "data", "cache", f"kline_{s}.csv")
        df = M._fetch_kline_timed(s, days)
        if df is None or len(df) < 30:
            fail.append(s)
            print(f"  [{i+1}/{len(codes)}] {s} {name} 抓取失败/过短")
            continue
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            df.to_csv(p)
            ok += 1
            print(f"  [{i+1}/{len(codes)}] {s} {name} 写缓存 len={len(df)} ({str(df.index[0])[:10]}~{str(df.index[-1])[:10]})")
        except Exception as e:  # noqa: BLE001
            fail.append(s)
            print(f"  [{i+1}/{len(codes)}] {s} {name} 写缓存失败: {repr(e)[:120]}")
    print(f"\n完成：成功 {ok} 只，失败 {len(fail)} 只 -> {fail}")
    return ok, fail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="core", choices=["core", "extended"])
    ap.add_argument("--days", type=int, default=2400)
    args = ap.parse_args()
    if args.universe == "core":
        codes = M.build_universe()
    else:
        from _xsec import extended_universe
        codes = extended_universe()
    print(f"预抓取 {len(codes)} 只 K 线，days={args.days}")
    prefetch(codes, args.days)


if __name__ == "__main__":
    main()

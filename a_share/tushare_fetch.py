"""Tushare 个股资金流历史抓取（在独立 tushare venv 中运行）。

用法（token 仅作环境变量，不落盘）：
  set TUSHARE_TOKEN=xxxx
  <venv>/python.exe tushare_fetch.py [--symbols 300034 002085 ...] [--start 20190101]

产出：a_share/data/moneyflow/<symbol>.csv
  列：trade_date(YYYYMMDD), main_net_in(元, 主力=大单+特大单净额)
这些 CSV 是纯数据缓存，主回测环境（无 tushare）直接读，不依赖 tushare 安装。
"""

from __future__ import annotations

import os
import sys
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

CACHE_DIR = os.path.join(HERE, "data", "moneyflow")


def _ts_code(symbol: str) -> str:
    if symbol.startswith(("6", "5", "9")):
        return symbol + ".SH"
    if symbol.startswith(("4", "8")):
        return symbol + ".BJ"
    return symbol + ".SZ"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="20260820")
    args = ap.parse_args()

    tok = os.environ.get("TUSHARE_TOKEN")
    if not tok:
        print("[err] 需要环境变量 TUSHARE_TOKEN")
        return
    try:
        import tushare as ts
    except Exception as e:  # noqa: BLE001
        print(f"[err] 无法 import tushare: {e}")
        return
    ts.set_token(tok)
    pro = ts.pro_api()

    if args.symbols:
        symbols = args.symbols
    else:
        try:
            from screener import CORE_POOL
            seen = {}
            for stocks in CORE_POOL.values():
                for code, _ in stocks:
                    seen[code] = True
            symbols = list(seen.keys())
        except Exception:
            symbols = []

    os.makedirs(CACHE_DIR, exist_ok=True)
    print(f"抓取 {len(symbols)} 只资金流历史（{args.start}~{args.end}）…")
    ok = 0
    for s in symbols:
        try:
            df = pro.moneyflow(ts_code=_ts_code(s),
                               start_date=args.start, end_date=args.end)
            if df is None or len(df) == 0:
                print(f"  {s}: 空")
                continue
            # 主力净流入 = 大单 + 特大单 净额（元；Tushare 金额字段单位为千元）
            lg = (df["buy_lg_amount"] - df["sell_lg_amount"]).fillna(0.0)
            elg = (df["buy_elg_amount"] - df["sell_elg_amount"]).fillna(0.0)
            df = df.copy()
            df["main_net_in"] = ((lg + elg) * 1000.0).round(2)
            out = df[["trade_date", "main_net_in"]].sort_values("trade_date")
            out.to_csv(os.path.join(CACHE_DIR, f"{s}.csv"), index=False)
            ok += 1
            print(f"  {s}: {len(out)} 行 OK")
        except Exception as e:  # noqa: BLE001
            print(f"  {s}: 失败 {str(e)[:80]}")
    print(f"\n完成：成功 {ok}/{len(symbols)}；缓存目录 {CACHE_DIR}")


if __name__ == "__main__":
    main()

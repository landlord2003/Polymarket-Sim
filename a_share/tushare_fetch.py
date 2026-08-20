"""Tushare 个股资金流历史抓取（在独立 tushare venv 中运行）。

用法（token 仅作环境变量，不落盘）：
  set TUSHARE_TOKEN=xxxx
  <venv>/python.exe tushare_fetch.py [--symbols 300034 002085 ...] [--start 20190101]

产出：a_share/data/moneyflow/<symbol>.csv
  列（金额单位统一为「元」，千元×1000；成交量单位「手」）：
    trade_date,
    main_net_in,            # 主力净流入 = (大单+特大单)净额，元
    buy_elg_amount, sell_elg_amount,   # 特大单 买/卖 额
    buy_lg_amount,  sell_lg_amount,    # 大单
    buy_md_amount,  sell_md_amount,    # 中单
    buy_sm_amount,  sell_sm_amount,    # 小单
    buy_elg_vol, sell_elg_vol, buy_lg_vol, sell_lg_vol,
    buy_md_vol, sell_md_vol, buy_sm_vol, sell_sm_vol
这些 CSV 是纯数据缓存，主回测环境（无 tushare）直接读，不依赖 tushare 安装。
留存全字段是为了支持「更精细资金流构造」（订单档位背离 / 主力强度 / 散户-机构分化等），
而不仅是净额一个标量。
"""

from __future__ import annotations

import os
import sys
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

CACHE_DIR = os.path.join(HERE, "data", "moneyflow")

# 需要抓取并落盘的字段（Tushare moneyflow 原始列名）
AMT_FIELDS = ["buy_sm_amount", "sell_sm_amount",
              "buy_md_amount", "sell_md_amount",
              "buy_lg_amount", "sell_lg_amount",
              "buy_elg_amount", "sell_elg_amount",
              "buy_sm_vol", "sell_sm_vol",
              "buy_md_vol", "sell_md_vol",
              "buy_lg_vol", "sell_lg_vol",
              "buy_elg_vol", "sell_elg_vol"]


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
    print(f"抓取 {len(symbols)} 只资金流全字段历史（{args.start}~{args.end}）…")
    ok = 0
    for s in symbols:
        try:
            df = pro.moneyflow(ts_code=_ts_code(s),
                               start_date=args.start, end_date=args.end)
            if df is None or len(df) == 0:
                print(f"  {s}: 空")
                continue
            df = df.copy()
            # 金额：千元 → 元
            for c in AMT_FIELDS:
                if c in df.columns:
                    df[c] = (df[c].fillna(0.0) * 1000.0).round(2)
            # 主力净流入 = 大单 + 特大单 净额（元；与旧版口径保持一致）
            lg = (df["buy_lg_amount"] - df["sell_lg_amount"]).fillna(0.0)
            elg = (df["buy_elg_amount"] - df["sell_elg_amount"]).fillna(0.0)
            df["main_net_in"] = (lg + elg).round(2)
            keep = ["trade_date", "main_net_in"] + AMT_FIELDS
            out = df[[c for c in keep if c in df.columns]].sort_values("trade_date")
            out.to_csv(os.path.join(CACHE_DIR, f"{s}.csv"), index=False)
            ok += 1
            print(f"  {s}: {len(out)} 行 OK（含 {len(AMT_FIELDS)} 个订单档位字段）")
        except Exception as e:  # noqa: BLE001
            print(f"  {s}: 失败 {str(e)[:80]}")
    print(f"\n完成：成功 {ok}/{len(symbols)}；缓存目录 {CACHE_DIR}")


if __name__ == "__main__":
    main()

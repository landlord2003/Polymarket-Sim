"""探查 AkShare 5类信息源接口的真实字段与数据粒度。仅用于设计特征前的摸排。"""
import sys, traceback
import akshare as ak
import pandas as pd

pd.set_option("display.max_columns", 30)
pd.set_option("display.width", 200)

def probe(label, fn, *a, **k):
    print("=" * 70)
    print("PROBE:", label)
    try:
        df = fn(*a, **k)
        if isinstance(df, pd.DataFrame):
            print("  shape:", df.shape)
            print("  cols:", list(df.columns))
            print(df.head(2).to_string())
        else:
            print("  type:", type(df), "val:", str(df)[:200])
    except Exception as e:
        print("  ERROR:", type(e).__name__, str(e)[:200])
        traceback.print_exc()

# ---- 北向资金 ----
probe("hsgt_hold_stock_em(沪股通,月排行)", ak.stock_hsgt_hold_stock_em, market="沪股通", indicator="月排行")
probe("hsgt_fund_flow_summary_em", ak.stock_hsgt_fund_flow_summary_em)
probe("hsgt_hist_em(北向资金)", ak.stock_hsgt_hist_em, symbol="北向资金")
probe("hsgt_individual_em", ak.stock_hsgt_individual_em)
try:
    probe("hsgt_individual_detail_em", ak.stock_hsgt_individual_detail_em)
except Exception:
    print("  (hsgt_individual_detail_em 可能不存在，跳过)")

# ---- 龙虎榜 ----
probe("lhb_detail_em(20260801-20260819)", ak.stock_lhb_detail_em, start_date="20260801", end_date="20260819")
probe("lhb_stock_statistic_em(近一月)", ak.stock_lhb_stock_statistic_em, symbol="近一月")
probe("lhb_jgmmtj_em(20260801-20260819)", ak.stock_lhb_jgmmtj_em, start_date="20260801", end_date="20260819")

# ---- 基本面业绩 ----
probe("yjbb_em(20260331)", ak.stock_yjbb_em, date="20260331")
probe("financial_analysis_indicator_em(300034.SZ)", ak.stock_financial_analysis_indicator_em, symbol="300034.SZ", indicator="按报告期")

# ---- 分析师 ----
probe("profit_forecast_em(300034)", ak.stock_profit_forecast_em, symbol="300034")
probe("rank_forecast_cninfo(20260817)", ak.stock_rank_forecast_cninfo, date="20260817")
probe("analyst_detail_em", ak.stock_analyst_detail_em, analyst_id="11000200926", indicator="最新跟踪成分股")

# ---- 事件 ----
probe("restricted_release_queue_em(300034)", ak.stock_restricted_release_queue_em, symbol="300034")
probe("repurchase_em", ak.stock_repurchase_em)
probe("ggcg_em(全部)", ak.stock_ggcg_em, symbol="全部")

print("\n=== ALL PROBES DONE ===")

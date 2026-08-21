"""真实数据校验：用 AKShare 免费逐笔验证 l2_features.aggregate_daily 在真实数据上的解析。

AKShare 免费逐笔 stock_zh_a_tick_tx_js 仅返回「最新一个交易日」，无历史。
本脚本对若干 core 龙头股抓当日真实逐笔 -> 聚合 12 维因子 -> 打印样本，
证明真实买卖盘方向标记可被正确解析为订单失衡等 L2 特征（pipeline 真实数据路径打通）。

注意：仅 1 天 -> 不能跑 walk-forward IC；多日历史 edge 验证需付费历史逐笔(财富通 600元/年)。
"""
from __future__ import annotations
import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import l2_features as L2
import ml_model as M


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20, help="取 core 前 N 只")
    args = ap.parse_args()
    syms = M.build_universe()[:args.n]
    print(f"=== 真实 AKShare 逐笔 L2 因子校验（{len(syms)} 只，最新交易日）===")
    got = 0
    for (s, name, _) in syms:
        try:
            tick = __import__("akshare").stock_zh_a_tick_tx_js(
                symbol=("sh" if s.startswith("6") else "sz") + s)
        except Exception as e:
            print(f"  {s} {name}: 抓取失败 {repr(e)[:100]}")
            continue
        if tick is None or len(tick) == 0:
            print(f"  {s} {name}: 空数据")
            continue
        feat = L2.aggregate_daily(tick)
        if feat is None:
            print(f"  {s} {name}: 聚合失败")
            continue
        got += 1
        print(f"  {s} {name}: 逐笔{len(tick)}笔 | OI={feat['oi']:+.3f} "
              f"主动买比={feat['aggr_buy_ratio']:.3f} 大单比={feat['large_trade_ratio']:.3f} "
              f"尾盘失衡={feat['close_imbalance']:+.3f} 漂移={feat['price_drift']:+.4f}")
    print(f"=== 成功解析 {got}/{len(syms)} 只真实逐笔 -> L2 因子路径打通 ===")


if __name__ == "__main__":
    main()

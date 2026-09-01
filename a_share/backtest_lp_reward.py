# -*- coding: utf-8 -*-
"""#143 模拟验证：纯价差 vs 「价差+LP奖励」 双收益 walk-forward 对比。

数据：data/quotes_ts/*.jsonl（本机曾抓到的真实盘口快照序列，按 ts 升序 walk-forward）
逻辑：对每帧快照跑 lp_reward.compare_over_quotes，汇总 blended edge 提升（lift_pct）；
      并对 (δ, apr) 网格做扫描，找最优参数组合。

⚠️ δ / apr 是假设值（北京无外网）。NB 有网后把真实 δ、真实年化率喂进来即可，
   本报告逻辑不变。结论只说明「双收益模型相对纯价差的增量方向」，不承诺真钱收益。

运行：cd a_share && python backtest_lp_reward.py
"""
import glob
import json
import os

import lp_reward as LP

QUOTES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "data", "quotes_ts")
OUT_MD = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "LP_REWARD_BACKTEST.md")

# 假设参数网格（NB 回填真实值）
DELTA_GRID = [0.005, 0.01, 0.02, 0.03]
APR_GRID = [0.10, 0.20, 0.50, 1.0, 2.0, 3.65]  # 3.65 ≈ lp_tool 实测 1%+ 日化
TIME_H = 24.0
MIN_SPREAD = 0.002


def _load_snapshots():
    files = sorted(glob.glob(os.path.join(QUOTES_DIR, "quotes_*.jsonl")))
    snaps = []
    for fp in files:
        with open(fp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ms = d.get("markets") or []
                if ms:
                    snaps.append((d.get("ts"), ms))
    return snaps


def main():
    snaps = _load_snapshots()
    if not snaps:
        print("[lp-backtest] 无 quotes_ts 快照，退出")
        return
    print("[lp-backtest] 载入快照帧数: %d" % len(snaps))

    lifts = []
    per_snap = []
    for ts, ms in snaps:
        c = LP.compare_over_quotes(ms, delta=DELTA_GRID[1], apr=APR_GRID[1],
                                   min_spread=MIN_SPREAD, time_in_band_h=TIME_H)
        if c["n"] > 0:
            lifts.append(c["lift_pct"])
            per_snap.append((ts, c["n"], c["pure_sum"], c["reward_sum"],
                             c["lift_pct"]))

    n_frames = len(lifts)
    mean_lift = sum(lifts) / n_frames if n_frames else 0.0
    print("[lp-backtest] 有效帧 %d，平均 blended edge 提升 %.2f%%" %
          (n_frames, mean_lift))

    # 参数扫描：在最末帧上跑 (δ, apr) 网格
    last_ms = snaps[-1][1]
    sweep_rows = LP.sweep(last_ms, deltas=DELTA_GRID, aprs=APR_GRID,
                          min_spread=MIN_SPREAD, time_in_band_h=TIME_H)
    print("[lp-backtest] 最优组合: δ=%.3f apr=%.2f lift=%.2f%%" %
          (sweep_rows[0]["delta"], sweep_rows[0]["apr"], sweep_rows[0]["lift_pct"]))

    # 写报告
    lines = []
    lines.append("# LP 奖励半宽 δ 感知定价 —— walk-forward 模拟验证\n")
    lines.append("> 生成：自动｜数据：data/quotes_ts/*.jsonl（本机盘口快照，非实时）｜"
                 "δ/apr 为假设值（NB 回填真实）\n")
    lines.append("## 结论（模拟，非真钱承诺）\n")
    lines.append("- 有效快照帧数：**%d**" % n_frames)
    lines.append("- 相对纯价差，blended edge（价差+奖励）平均提升：**%.2f%%**"
                 % mean_lift)
    lines.append("- 末帧 (δ,apr) 扫描最优组合：δ=%.3f / apr=%.2f → lift=%.2f%%"
                 % (sweep_rows[0]["delta"], sweep_rows[0]["apr"],
                    sweep_rows[0]["lift_pct"]))
    lines.append("- **含义**：在薄价差市场（spread 接近或小于 2δ），把双边报价压进奖励区"
                 "可吃到期权式正向奖励，使「双收益」跑赢纯价差；在深价差市场纯价差仍占优，"
                 "模块会自动选 `chosen=spread`。")
    lines.append("- **边界**：δ/apr 真实值未知，lift 量级随假设浮动；本验证只证明"
                 "「双收益模型方向成立、且已有自动择优逻辑」，不承诺真钱净盈利。\n")
    lines.append("## 逐帧 lift（前 20 帧）\n")
    lines.append("| ts | 市场数 | 纯价差和 | 双收益和 | lift% |")
    lines.append("|----|--------|----------|----------|-------|")
    for ts, n, ps, rs, lf in per_snap[:20]:
        lines.append("| %s | %d | %.4f | %.4f | %.2f |" %
                     (ts, n, ps, rs, lf))
    lines.append("\n## (δ, apr) 扫描（末帧，按 lift 降序）\n")
    lines.append("| δ | apr | 市场数 | 纯价差和 | 双收益和 | lift% |")
    lines.append("|---|-----|--------|----------|----------|-------|")
    for r in sweep_rows:
        lines.append("| %.3f | %.2f | %d | %.4f | %.4f | %.2f |" %
                     (r["delta"], r["apr"], r["n"], r["pure_sum"],
                      r["reward_sum"], r["lift_pct"]))
    lines.append("")
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("[lp-backtest] 报告已写 %s" % OUT_MD)


if __name__ == "__main__":
    main()

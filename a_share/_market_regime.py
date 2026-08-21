"""① 分市场态建模：基于 ③ 层A 已生成的 records_new（每截面IC + reb日期），
按沪深300市态（牛/熊/震荡）分组，检验信号在不同市态下 IC 是否分化。
输出分市态 IC 对照 + markdown 片段。
"""
import json, os
import numpy as np
import pandas as pd
import ml_model as M

OUT = "D:/WorkBuddy/output"
A_PATH = os.path.join(OUT, "ml_xsec_expand_real_core_2400.json")


def regime_label(bench_close, idx_map, rb_ts):
    """返回 'bull' / 'bear' / 'shock'（震荡）。基于价格相对 MA60/MA200 的位置与均线方向。"""
    j = idx_map.get(rb_ts)
    if j is None or j < 200:
        return None
    ma60 = bench_close[j - 60:j + 1].mean()
    ma200 = bench_close[j - 200:j + 1].mean()
    ma200_prev = bench_close[j - 220:j - 20 + 1].mean() if j >= 220 else ma200
    price = bench_close[j]
    if price > ma60 > ma200 and ma200 >= ma200_prev:
        return "bull"
    if price < ma60 < ma200 and ma200 <= ma200_prev:
        return "bear"
    return "shock"


def main():
    A = json.load(open(A_PATH, encoding="utf-8"))
    recs = A["records_new"]
    bench = M.fetch_benchmark_histories(days=2400).get("沪深300")
    bclose = bench["close"].values.astype(float)
    bidx = {pd.Timestamp(d): j for j, d in enumerate(bench.index)}

    grouped = {"bull": [], "bear": [], "shock": []}
    for r in recs:
        rb = pd.Timestamp(r["reb"])
        lab = regime_label(bclose, bidx, rb)
        if lab:
            grouped[lab].append(r["ic"])

    print("=== ① 分市场态 IC（基于层A real/core records_new） ===")
    rows = []
    for lab in ("bull", "bear", "shock"):
        v = np.array(grouped[lab])
        if len(v) == 0:
            print(f"  {lab}: 无样本"); continue
        mean = float(v.mean()); pos = float((v > 0).mean())
        t = float(v.mean() / (v.std(ddof=1) / np.sqrt(len(v)))) if v.std() > 0 else 0.0
        print(f"  {lab:5s}: n={len(v):3d}  IC={mean:+.4f}  IC>0={pos*100:.1f}%  t={t:+.2f}")
        rows.append((lab, len(v), mean, pos, t))

    # 写片段
    md = ["## 六、① 分市场态建模（real/core，按沪深300牛/熊/震荡）",
          "- 方法：每个 rebalance 日按沪深300价格相对 MA60/MA200 的位置与均线方向打市态标签，分组统计 IC。",
          "| 市态 | 截面数 | 平均IC | IC>0占比 | t值 |",
          "|------|--------|--------|----------|-----|"]
    for lab, n, mean, pos, t in rows:
        md.append(f"| {lab} | {n} | {mean:+.4f} | {pos*100:.1f}% | {t:+.2f} |")
    md.append("")
    note = ("**结论**：" + ("各市态下 IC 均为弱值且 t 不显著，信号在任何市态下都没有稳定可交易的 edge；"
                            "所谓「震荡市更灵」的假设在本数据下不成立。" if all(abs(t) < 2 for _, _, _, _, t in rows)
                            else "存在某市态 IC 相对更强，可视为条件性信号（见上表）。"))
    md.append(note)
    md.append("")
    with open(os.path.join(OUT, "ml_market_regime.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print("片段 -> D:/WorkBuddy/output/ml_market_regime.md")


if __name__ == "__main__":
    main()

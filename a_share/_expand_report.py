"""③ 扩池复核报告合并：读取层A(real/core/2400) + 层B三批(proxy/extended/2400/b1,b2,b3)
+ ② 回测结果，生成对比 Markdown 到 D:/WorkBuddy/output/ml_xsec_expand_report.md
"""
import json, os
import numpy as np

OUT = "D:/WorkBuddy/output"

def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)

def merge_records(paths):
    recs = []
    for p in paths:
        if not os.path.exists(p):
            raise FileNotFoundError(p)
        d = load(p)
        recs.extend(d["records_new"])
    a = np.array([r["ic"] for r in recs])
    mean = float(a.mean())
    pos = float((a > 0).mean())
    t = float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a)))) if a.std() > 0 else 0.0
    years = {}
    for r in recs:
        years.setdefault(r["reb"][:4], []).append(r["ic"])
    ys = {y: {"mean_ic": float(np.mean(v)), "ic_pos": float((np.array(v) > 0).mean()), "n": len(v)}
          for y, v in sorted(years.items())}
    return recs, mean, pos, t, ys, len(recs)

def ic_block(title, d, with_year=True):
    s = []
    s.append(f"**{title}**")
    s.append(f"- 截面数: {d['n_cross_sections']} ｜ 标的数: {d['n_symbols']} ｜ 历史: {d['days']}日(~{d['days']/240:.0f}年)")
    s.append(f"- 整体 t 值(X_new): **{d['t_stat_new']:+.2f}** （|t|>2 才显著）")
    s.append(f"- IC(A) X_old 24维 : **{d['mean_ic_old_24']:+.4f}**  IC>0 {(d['ic_pos_old']*100):.1f}%")
    s.append(f"- IC(B) X_new 32维(+交互): **{d['mean_ic_new_32']:+.4f}**  IC>0 {(d['ic_pos_new']*100):.1f}%  Δ={d['interaction_contrib']:+.4f}")
    s.append(f"- IC(C) X_full 43维(+alt): {d['mean_ic_full_43']:+.4f}  IC>0 {(d['ic_pos_full']*100):.1f}%")
    s.append(f"- IC(D) GBT(X_new) : {d['mean_ic_gbt_32']:+.4f}  IC>0 {(d['ic_pos_gbt']*100):.1f}%")
    if with_year:
        s.append("- 分年度 IC(B):")
        for y, st in d["year_stats"].items():
            s.append(f"  - {y}: IC={st['mean_ic']:+.4f}  IC>0={(st['ic_pos']*100):.1f}%  n={st['n']}")
    return "\n".join(s)

def main():
    A = load(os.path.join(OUT, "ml_xsec_expand_real_core_2400.json"))
    B_paths = [os.path.join(OUT, f"ml_xsec_expand_proxy_extended_2400_b{i}.json") for i in (1, 2, 3)]
    recs_b, mb, pb, tb, ysb, nb = merge_records(B_paths)
    try:
        BT = load(os.path.join(OUT, "ml_backtest_longshort.json"))
    except Exception:
        BT = None

    L = []
    L.append("# 量化信号「榨信号」第三阶段：扩池复核 + 多空回测报告\n")
    L.append("> 阶段目标：把此前 `IC≈0.09` 的订单流信号放到**更大样本（92截面/10年）与更大股票池（110+只）**下检验是否过拟合；"
             "并对排名信号做**多空实盘扣费回测**，确认其是否具备可交易 edge。\n")

    # 核心结论
    L.append("## 一、一句话结论 🔴")
    L.append(f"- 层A（real订单流+core 39只，拉长到 **{A['n_cross_sections']} 截面/{A['days']}日**）："
             f"IC 从前期 18 截面下的 ~0.09 **塌缩到 {A['mean_ic_new_32']:+.4f}**，整体 t={A['t_stat_new']:+.2f} **不显著**，"
             f"且分年度极不稳定（2019 年为负、2020 年独挑大梁）。**此前结论是 18 截面小样本 + 2020 极端年的过拟合假象。**")
    L.append(f"- 层B（proxy+extended **110只**扩池）：合并 {nb} 截面后 IC(B)=**{mb:+.4f}**（IC>0 {(pb*100):.1f}%），"
             f"t={tb:+.2f} **不显著** —— 基础价量因子在扩池下同样**无 edge**，且泛化到更大样本后进一步走弱。")
    if BT:
        L.append(f"- ② 多空实盘扣费回测：多头组合扣费年化 **{BT['ann_return_A']*100:+.1f}%**（基准沪深300 {BT['ann_return_bench']*100:+.1f}%），"
                 f"**超额仅 {BT['ann_excess']*100:+.1f}%、夏普 {BT['sharpe_excess']:.2f}、跑赢基准胜率 {BT['hit_long_vs_bench']*100:.1f}%**；"
                 f"**理想多空毛净值 {BT['eq_ls_final']:.3f}（<1，即多空组合净亏）**，平均多空毛差 {BT['mean_ls_gross']*100:+.2f}%/期 —— 信号根本没有「多强空弱」区分能力。")
    L.append("")
    L.append("**综合判定：该信号在严格扩样与实盘扣费下均站不住，不具备可交易 alpha。当前应停止在它上面继续投入，转向数据维度/频率的根本性升级（见第五节）。**\n")

    # 层A
    L.append("## 二、③ 层A：real 订单流 + core 39只（过拟合检验）")
    L.append(ic_block("配置对照（X_new = 24价量 + 8订单流交互）", A))
    L.append("")

    # 层B
    L.append("## 三、③ 层B：proxy 价量 + extended 110只扩池（泛化检验）")
    L.append(f"- 说明：proxy 模式下订单流交互特征降级为 0，本层检验**纯价量因子**在 110+ 只大样本下的泛化能力。"
             f"三批(b1/b2/b3)各 40 只独立 walk-forward，以下为合并 {nb} 截面后的结果：")
    L.append(f"- 合并 IC(B) = **{mb:+.4f}** ｜ IC>0 {(pb*100):.1f}% ｜ 整体 t = **{tb:+.2f}**（不显著）")
    L.append("- 分年度 IC(B):")
    for y, st in ysb.items():
        L.append(f"  - {y}: IC={st['mean_ic']:+.4f}  IC>0={(st['ic_pos']*100):.1f}%  n={st['n']}")
    L.append("")

    # ②
    if BT:
        L.append("## 四、② 多空实盘扣费回测（real/core，horizon=10，N=8）")
        L.append(f"- 期数: {BT['n_cross_sections']}（walk-forward 有效截面，剔除训练不足的早期截面）")
        L.append(f"- 多头组合净值(扣费) **{BT['eq_A_final']:.3f}**  年化 **{BT['ann_return_A']*100:+.1f}%**")
        L.append(f"- 沪深300基准净值 {BT['eq_bench_final']:.3f}  年化 {BT['ann_return_bench']*100:+.1f}%")
        L.append(f"- **超额净值 {BT['eq_A_final']/BT['eq_bench_final']:.3f}  年化超额 {BT['ann_excess']*100:+.1f}%**  超额夏普 {BT['sharpe_excess']:.2f}")
        L.append(f"- 多头跑赢基准胜率 **{BT['hit_long_vs_bench']*100:.1f}%**（<50%，说明选股并不稳定优于基准）")
        L.append(f"- 最大回撤 多头 **{BT['mdd_A']*100:.1f}%** vs 基准 {BT['mdd_bench']*100:.1f}%（多头回撤更深、风险更大）")
        L.append(f"- **理想多空毛净值 {BT['eq_ls_final']:.3f}（<1 → 多空净亏）** ｜ 平均多空毛差 {BT['mean_ls_gross']*100:+.2f}%/期")
        L.append("")

    # 诊断
    L.append("## 五、根因诊断与下一步")
    L.append("1. **小样本陷阱**：18 截面下 IC≈0.09 主要由 2020（疫情散户狂热、订单流噪声被放大）单一极端年贡献；放大到 92 截面后均值回归到 ~0.04 且 t<2 不显著。")
    L.append("2. **价量信噪比≈0 是天花板**：无论换模型（LR≈GB）、改打法（绝对→横截面排名）、加低频公开信息（alt 反降）、加订单流交互（小样本微提、大样本反拖累），都绕不开「公开价量信息已被市场充分定价」这一事实。")
    L.append("3. **实盘维度才是关键缺口**：当前全部特征来自**日频公开行情/资金流**，信息层级最低。要继续压榨，方向应是：")
    L.append("   - 更高频/微观结构数据（逐笔委托、Level-2 盘口、机构席位拆单）—— 需付费数据源；")
    L.append("   - 另类数据（产业链订单、舆情/搜索指数、资金跨境流向）—— 需外部数据合作；")
    L.append("   - 或转向**严格风控下的指数增强/中性**框架，把 IC≈0.04 的弱因子当作众多因子之一，而非单一信号孤注一掷。")
    L.append("4. **本阶段产出已诚实证伪，避免实盘亏损**，价值在于「止损」而非「找到圣杯」。\n")

    md = "\n".join(L)
    outp = os.path.join(OUT, "ml_xsec_expand_report.md")
    with open(outp, "w", encoding="utf-8") as f:
        f.write(md)
    print("报告已生成:", outp)
    print("层A IC(B)=%.4f t=%.2f | 层B合并 IC(B)=%.4f t=%.2f | ② 多空毛净值=%.3f" %
          (A["mean_ic_new_32"], A["t_stat_new"], mb, tb, BT["eq_ls_final"] if BT else float("nan")))

if __name__ == "__main__":
    main()

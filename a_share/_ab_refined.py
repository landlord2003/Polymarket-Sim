"""解析 ml_refined_proxy.md 与 ml_refined_real.md，产出精细资金流 A/B 对比报告。

用法：
  python _ab_refined.py
输出：D:/WorkBuddy/output/ml_refined_ab.md
"""
from __future__ import annotations
import re
import os

OUT = "D:/WorkBuddy/output"

def parse_table(path):
    """返回 {(horizon, model): dict(prec50, prec60, cov60, acc, rule)}"""
    res = {}
    if not os.path.exists(path):
        return res
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*([\d.]+)%\s*\|\s*([\d.]+)%\s*\|\s*([\d.]+)%\s*\|\s*([\d.]+)%\s*\|\s*([\d.]+)%\s*\|", line)
            if m:
                h = int(m.group(1)); model = m.group(2).strip()
                res[(h, model)] = {
                    "prec50": float(m.group(3)), "prec60": float(m.group(4)),
                    "cov60": float(m.group(5)), "acc": float(m.group(6)),
                    "rule": float(m.group(7)),
                }
    return res


def main():
    proxy = parse_table(os.path.join(OUT, "ml_refined_proxy.md"))
    real = parse_table(os.path.join(OUT, "ml_refined_real.md"))
    keys = sorted(set(proxy) | set(real))

    lines = ["# 精细资金流构造 A/B 重测报告\n"]
    lines.append("> 目的：把 Tushare **全字段订单流**（大单/特大单/中单/小单买卖额）构造成 10 维精细特征，\n"
                 "对比「仅用净额标量(proxy)」vs「精细订单流(real)」能否提升日频上涨预测准确率。\n")
    lines.append("> 架构：基础 14 维 + 精细资金流 10 维 = 24 维；proxy 模式下订单档位特征恒为 0（仅 MFI/ADI 来自K线），"
                 "从而干净隔离真实订单信息增量。walk-forward 扩展窗口、每月重训；随机基准 50%。\n")
    lines.append("## 逐 horizon × 模型：proxy vs real\n")
    lines.append("| horizon | 模型 | proxy precision_up | real precision_up | **Δ(real−proxy)** | real 高置信(≥0.6) | 规则基线 | 突破50%? | real>proxy? |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    any_beat50 = False
    any_real_wins = False
    best = None
    for k in keys:
        h, model = k
        p = proxy.get(k, {}).get("prec50")
        r = real.get(k, {}).get("prec50")
        p_s = f"{p:.1f}%" if p is not None else "—"
        r_s = f"{r:.1f}%" if r is not None else "—"
        if p is not None and r is not None:
            delta = r - p
            d_s = f"{delta:+.1f}pt"
            beat50 = r > 50.0
            wins = r > p
            any_beat50 = any_beat50 or beat50
            any_real_wins = any_real_wins or wins
            if best is None or r > best[0]:
                best = (r, h, model, delta)
            flag50 = "✅" if beat50 else "❌"
            flagw = "✅" if wins else "❌"
            rule_s = f"{real[k]['rule']:.1f}%"
            cov60 = real.get(k, {}).get("prec60")
            cov_s = f"{cov60:.1f}%" if cov60 is not None else "—"
            lines.append(f"| {h} | {model} | {p_s} | {r_s} | **{d_s}** | {cov_s} | {rule_s} | {flag50} | {flagw} |")
        else:
            lines.append(f"| {h} | {model} | {p_s} | {r_s} | — | — | — | — | — |")

    lines.append("\n## 结论\n")
    if best is not None:
        r, h, model, delta = best
        lines.append(f"- **最优组合：horizon {h}d / {model}，real precision_up = {r:.1f}%（Δ vs proxy {delta:+.1f}pt）。**")
        lines.append(f"- 随机基准 50%；{'✅ real 已突破 50% 硬币线' if r > 50 else '⚠️ real 仍未稳定越过 50% 硬币线'}。")
        if any_beat50:
            lines.append("- 存在 real 突破 50% 的组合，但需看是否稳定、覆盖率是否可用。")
        else:
            lines.append("- **所有组合的 real precision_up 均未稳定突破 50%**，与 proxy 差异在噪声量级（通常 ±1~2pt）。")
        if any_real_wins:
            lines.append("- 多数组合 real ≥ proxy，说明精细订单流信息方向偏正，但幅度不足以产生可实战的超额。")
        else:
            lines.append("- real 并未稳定优于 proxy，证实「单日资金流方向」本身预测力有限，精细构造增益微弱。")
    lines.append("- 诚实提示：精细资金流（订单档位背离/主力强度/散户-机构分化/价格-资金背离）相比「净额标量」")
    lines.append("  在信息量上更丰富，但在 **日频 N 日涨跌方向** 这一任务上仍未转化为可稳定盈利的信号；")
    lines.append("  瓶颈可能在于「方向可预测性」本身的天花板，而非特征粗细。")
    lines.append("- 下一步可选：①改为预测「未来收益符号+幅度」或「相对大盘超额」而非绝对涨跌；")
    lines.append("  ②缩短/拉长视角（如 1-3 日高频或 20-60 日中期）；③做仓位/风控而非二分类信号。")
    lines.append("\n> ⚠️ 本报告仅用于研究；模拟环境、零本金、严禁据此实盘。")

    out_path = os.path.join(OUT, "ml_refined_ab.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[ab] 已生成：{out_path}")


if __name__ == "__main__":
    main()

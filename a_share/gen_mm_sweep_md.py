# -*- coding: utf-8 -*-
"""由 mm_sweep_results.json 生成 MM_PARAM_SWEEP.md（不使用 % 格式化，避免字面%冲突）。"""
import json, os
_HERE = os.path.dirname(os.path.abspath(__file__))
res = json.load(open(os.path.join(_HERE, "mm_sweep_results.json"), encoding="utf-8"))

rows = []
for mm in ["0.004", "0.012", "0.02", "0.025", "0.03", "0.04", "0.05"]:
    for adv in ["0.10", "0.20", "0.30"]:
        r = res.get(mm, {}).get(adv)
        if not r:
            rows.append("| %s | %s | - | - | - | - | - |" % (mm, adv))
            continue
        rows.append("| %s | %s | %d | **%.4f** | %.1f%% | %.3f | %.3f |"
                    % (mm, adv, r["n"], r["ev"], r["win"] * 100,
                       r["pnl_min"], r["pnl_max"]))
TABLE = "\n".join(rows)

HEAD = """# MM 做市参数扫描报告（蒙特卡洛，复用真实摩擦模型）

> 方法：复用 `RigorVirtualBook.market_make` / `model_fill` 真实走簿滑点、对冲漂移、
> 手续费模型，在**真实日志校准的流动性分布**(中位~18000) + 合理右偏价差分布上做
> 轮次回放(每格 800 次)。策略仅接 `spread >= mm_min_spread` 的市场。
> 每轮 = 先买(建仓)后卖(对冲)，measure 锁利 PnL（与 production 一致）。

## 结论速览
- **MM 策略本身有正期望**：所有价差门槛下 EV 均为正，门槛越高 EV/胜率越高。
- 当前 `mm_min_spread=0.004`（0.4 分）几乎无门槛，是此前"账面净亏"的诱因之一——
  接了大量窄价差必亏单（被 2% 往返费吃穿）。
- **推荐配置**：`mm_min_spread=0.02`、`adverse_frac=0.20` → 胜率 ~99%、EV +$1.3/轮。
- 此前 `MM_MATURITY_REPORT.md` 的"净亏 -1.63%"是**未平仓库存的账面假象**：
  真实已实现锁利 **+$517（+5.17%）**，真实权益(现金+库存按成本) **+$4.81%**。

## 扫描结果（EV = 每轮次期望收益 USD）

| mm_min_spread | adverse_frac | n(成交) | EV($/轮) | 胜率 | pnl_min | pnl_max |
|---|---|---|---|---|---|---|
"""

TAIL = """
## 给定配置建议（写入 DEFAULT_PARAMS）
```
"mm_min_spread": 0.02,    # 仅接价差>=2分的市场，避开必亏窄价差
```
`adverse_frac` 维持 0.20（摩擦假设，非可调盈利杠杆）。

## 风险提示
- 扫描假设价差分布为右偏估计；真实 Polymarket 窄价差市场占比更高，
  抬门槛会**减少成交笔数**但提高单笔质量——这是必要的质量换数量。
- 即便正期望，仍需库存盯市+止损（见 sim_rigor 新增 `max_global_inv_notional` /
  `stop_loss_frac`）与真实下单层（live_order.py）后方可实盘。
"""

md = HEAD + TABLE + TAIL
with open(os.path.join(_HERE, "MM_PARAM_SWEEP.md"), "w", encoding="utf-8") as f:
    f.write(md)
print("written MM_PARAM_SWEEP.md")

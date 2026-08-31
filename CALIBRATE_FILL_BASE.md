# FILL_BASE 真实盘校准手册（NB 实测回填）

> 适用：NB 合作伙伴实盘部署；北京研发机只跑 `--preview`，**真校准必须在 NB**。
> 配套脚本：`a_share/calibrate_fill.py`　配套文档：`DEPLOY_NB.md` / `USAGE_MANUAL.md`

---

## 0. 先回答你的问题：必须真实交易吗？

**是的，但"真实交易"≠"真实风险"。**

- **为什么必须真实订单**：模拟盘的 `fill_prob` 是**假设**的成交概率（默认地板 0.30，影子标定中位≈0.94）。它永远给不出"我在真实 CLOB 上挂的 maker 单，到底被打掉了多少"这个 ground truth。只有**真挂单 + 轮询成交状态**，才能测出真实成交率。
- **为什么不是真风险**：用 **$2–5/笔的极小 maker 单**就能测出真实成交率；总暴露受「钱包余额 + `risk_control` 限额」双重限制，未成交单观察结束自动撤单，随时可 `POST /api/kill_switch` 远程熔断。校准期几乎零资金消耗。
- **方法论要不要重写**：**不用**。代码/账本逻辑已自洽（累计锁利恒等式成立，见 `USAGE_MANUAL.md` §4）。所谓"再次方法论迭代"，只是把 NB 实测的真实成交率**回填进既有参数 `FILL_BASE`**（以及 `adverse_selection_frac` / 滑点），是一个**校准闭环**，不是推倒重来。

> 一句话：要拿到"实盘能不能赚"的答案，必须在 NB 用真实（极小）订单校准一次；模拟盘的 258K 只是工程链路正确的证明，不是实盘预期。

---

## 1. 两种"校准"的本质区别（务必分清）

| 名称 | 来源 | 是否真实 | 能否用于实盘回填 |
|---|---|---|---|
| **影子标定** `compute_fill_calibration`（`/api/fill_calibration`） | 合成 `fill_prob` 分布算 `intended_median≈0.94` | ❌ 假设 | **禁止**直接回填 |
| **真实标定** `calibrate_fill.py`（本手册） | 真挂单 → 轮询成交状态 → `observed_rate` | ✅ 真实 | ✅ 唯一可信回填值 |

> ⚠️ 不要把影子标定的 `recommended_base≈0.94` 当实盘成交率。0.94 是"假设意图成交率"，真实盘口会低得多（对手方不会乖乖来吃你的单）。

---

## 2. 前置条件（NB 机器）

1. IP 落 **CA / New Brunswick**：`curl ipinfo.io` 确认，勿挂 VPN（否则触发地域合规）。
2. 已按 `DEPLOY_NB.md` 完成部署：`COMPLIANCE_FILTER=0`、`PM_BOT_PK` 已填、`pip install -r requirements_nb.txt`（含 `py-clob-client` / `websockets`）。
3. 热钱包仅放 **$200–500**（校准期单笔 $3，几乎零消耗）。
4. 服务用 `LIVE_MODE=1` 启动（校准脚本会强制要求）。

---

## 3. 校准协议（calibrate_fill.py）

### 3.1 预览（不发单，北京也可跑结构验证）
```bash
python a_share/calibrate_fill.py --preview
```
- 拉 Gamma 高流动市场，按**策略同款定价**打印将挂的 BUY/SELL maker 单（价格/价差/毛价差%），核对报价合理。
- 北京沙箱无外网会拉盘口失败，但脚本结构与定价公式可离线验证（`_pricing` 与 `sim_server` 策略一致）。

### 3.2 真校准（NB，有网 + 真私钥）
```bash
python a_share/calibrate_fill.py --live --markets 30 --size 3 --window 600 --rounds 2
```
- 每轮对 30 个市场，按策略定价真挂极小 GTC maker 单：
  - `buy_base = yes_bid + adverse * spread`
  - `sell_base = yes_ask - adverse * spread`
  - （`adverse` = `CALIB_ADVERSE` 环境变量，须等于实盘 `rigor.adverse_frac`，默认 0.15）
- **观察窗口 600s**：maker 单需等 taker 主动来吃，挂完等约 10 分钟再判成交。
- 轮询成交状态（`clob_exec.get_order_status`）：`filled > 0` 记 hit；未成交单自动撤单，避免残留库存。
- 输出：`attempts / hits / observed_fill_rate_pct / avg_gross_improvement_pct / recommended_base`，并写入 `a_share/data/fill_calibration_live.json`。

### 3.3 为什么定价要复刻策略
脚本用与 `sim_server` **完全相同**的 `buy_base / sell_base` 公式 + 同一 `adverse`。这样测出的成交率 = 实盘会**真实遇到**的成交率，而不是一个抽象值——回填后模拟盘的成交假设才贴真实盘。

---

## 4. 回填 FILL_BASE 的步骤

- **法A（推荐，最直接）**：把 `recommended_base` 写进 `.env` 的 `FILL_BASE`（如 `FILL_BASE=0.42`），重启 `sim_server` 生效。
- **法B**：设 `FILL_CALIBRATE_APPLY=1`，并把 `recommended_base` 写入 `a_share/data/fill_calibration.json` 的 `recommended_base` 字段；重启后系统自动采用。
- **门槛**：若 `observed_rate < 0.30`，说明当前定价太被动（或盘口太薄），**先调 `adverse_frac` / 换高流动市场**，不要直接放大。

---

## 5. 盈利验收闸门（比 fill 率更关键）

回填 `FILL_BASE` 后，用 `LIVE_MODE=1` + 小资金跑 **≥1 周**，直接回答"模拟 258K 实盘能否兑现"：

1. 观察真实 `realized`（USD）增速（看板或 `/api/state`）。
2. 算 **真实净价差/笔** = 真实 `realized` 增量 ÷ 成交笔数 − 实际费率（maker 挂单 **0 费**，吃单方峰值 1.8%）。
3. **闸门**：真实净价差 > 实际费率 → 策略在 NB 真能赚，可放大仓位（提 `MAX_TOTAL_POS` / `MAX_POS_PER_MARKET`）；否则不放大，回看 `adverse_selection_frac` / 滑点假设。

> fill 率高 ≠ 赚钱。高成交率只说明单常被吃；赚不赚取决于"净价差是否覆盖费率 + 逆向选择 + 延迟"。闸门才是最终判据。

---

## 6. 迭代闭环（建议节奏）

| 周次 | 动作 | 产出 |
|---|---|---|
| 第1周 | `calibrate_fill.py --live` 测真实成交率 | 回填 `FILL_BASE` |
| 第2周 | 小资金实盘（`LIVE_MODE=1`）跑 ≥1 周 | 测真实净价差，调 `adverse_selection_frac` / 滑点 |
| 收敛后 | 逐步放大仓位（每步先小资金验证） | 稳态实盘参数 |

> 市场流动性随时变，建议每月重测一次 `FILL_BASE`（尤其大行情前后）。

---

## 7. 风险与兜底

- 单笔极小（$3），总暴露 capped；未成交自动撤单。
- 真挂单前**必跑 `--preview`** 核对价格。
- 任何时候 `POST /api/kill_switch?token=SHUTDOWN_TOKEN` 远程熔断（钉钉告警）。
- 校准产生的小额真实持仓（$3/市场）可忽略，或手动平仓。

---

## 8. 当前代码已知缺口（透明说明）

- `live_dispatch` 真发单后**未把真实成交结果记进 `STATE["fill"]`**（看板 fill 率仍显示合成 `fill_prob`）。所以跑实盘时，看板 fill 率是"假设值"，**真实成交率以 `calibrate_fill.py` 输出为准**。
- 若希望实盘运行中也实时显示真实成交率，可增强 `live_dispatch`：捕获 `clob_exec` 返回的 `order_id` + 异步轮询成交，喂入独立的 `LIVE_FILL_*` 计数（后续可选，不影响校准闭环）。

---

## 9. 常见问题

**Q：北京能跑 `calibrate_fill.py` 吗？**
A：只能 `--preview`（无外网/无私钥时真挂会失败）；真校准必须在 NB。

**Q：测一次够吗？**
A：不够。建议 ≥1 周、多轮、多时段，抵消流动性时段波动；大行情前后重测。

**Q：`observed_rate` 很高（如 0.9）但实盘还是亏？**
A：fill 率高 ≠ 赚钱，走 §5 闸门看真实净价差是否覆盖费率+逆向选择+延迟。

**Q：影子标定 `recommended_base≈0.94` 能直接填 `FILL_BASE` 吗？**
A：不能。那是假设值，会严重高估真实成交率，导致模拟/实盘预期脱节。必须用本手册真实标定。

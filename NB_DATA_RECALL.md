# NB 首跑数据回收清单（锚 C 翻盘验证）

> **用途**：用户锚定 C（赌「真实 LP 奖励 apr 够高」翻盘）。结论（净盈利 ~30%、双收益 +2.53% vs +50%）对 `apr` 一个参数极度敏感，而 `apr`/`δ`/`FILL_BASE` 北京无外网算不出，必须由 NB 实盘/实测回填。本清单是「把球传给 NB 伙伴」的接收标准——满 1 个月该回传哪 3 个数、格式、北京回填后重跑哪条命令、翻盘判定门槛。
>
> 代码 latest `5ad2012`（GitHub `landlord2003/Polymarket-Sim` master，2026-09-01）。

---

## 0. 为什么需要这份清单

| 卡死的输入 | 北京现状 | 必须由 NB 出 |
|-----------|---------|------------|
| 真实 `apr`（LP 持币奖励年化率） | 墙外连不上，假设 `0.20` | ✅ 实测 |
| 真实 `δ`（奖励半宽） | 假设 `0.01` | ✅ 真实盘口 |
| 真实 `FILL_BASE`（成交率） | 假设 `0.30` | ✅ 实盘日志 |

回测已证明 `apr` 是开关变量：`apr=20%`→双收益 +2.53%（撑不起正期望）；`apr≈365%`（lp_tool 实测量级）→ +50%（EV 拉正）。**这三个数一回来，"30%"才从先验下注变成有数据支撑的结论。**

---

## 1. 回收的 3 个核心数

### 1.1 真实 `apr` — LP 持币时间加权奖励年化率

- **定义**：在 Polymarket 做 LP、资金停留在奖励区内每单位时间拿到的奖励，年化后的比率。
- **来源（诚实说明）**：本平台 `sim_server` 是 **DRY_RUN 模拟盘，不发真单、不真做 LP**，因此**不直接产生 LP 奖励数据**。`apr` 需从真实来源测：
  1. **`polymarket_lp_tool`**（竞品分析里唯一同族工具）实测；或
  2. **CLOB `/rewards` 端点**（NB 直连可达）按市场读奖励率；或
  3. NB 实盘（`LIVE_MODE=1`）真做 LP 后，从成交奖励反算。
- **回填位置**：`arbitrage.py` 读 `LP_REWARD_APR` env（默认 0.20）。
- **采集**：跑 lp_tool / CLOB rewards，取安全池（合规过滤后 ~22 个市场）中位数 apr。

### 1.2 真实 `δ` — 奖励半宽

- **定义**：LP 奖励覆盖的价差半宽，奖励区 = `[mid-δ, mid+δ]`。
- **来源**：CLOB 市场元数据 / NB 实盘盘口（每个市场的奖励带宽度）。
- **回填位置**：`arbitrage.py` 读 `LP_REWARD_DELTA` env（默认 0.01）。
- **采集**：取安全池市场 `δ` 的中位数。

### 1.3 真实 `FILL_BASE` — 挂在市场最优价的成交率

- **定义**：挂单被打到的概率（价格改善幅度越大越难成交）。
- **来源**：**NB 实盘 `LIVE_MODE=1` 跑 1–3 个月**的真实成交/挂单比。本平台已有现成校准脚本 `a_share/calibrate_fill.py`（NB 真挂单 → 真观察成交 → 回填 FILL_BASE）。
- **回填位置**：`sim_server.py` 读 `FILL_BASE` env（默认 0.30）；或 `FILL_CALIBRATE_APPLY=1` 由影子标定自动写入 `data/fill_calibration.json` 的 `recommended_base`。
- **采集**：`python a_share/calibrate_fill.py`（详见 §3）。

---

## 2. 回收时间表

| 里程碑 | 时间窗 | 回传内容 | 用途 |
|--------|--------|---------|------|
| **M1 初值** | NB 实盘满 **1 个月** | `apr`/`δ` 首测 + `FILL_BASE` 首估 | 北京首次回填，跑出"有数据版"双收益 |
| **M3 标定** | NB 实盘满 **3 个月** | `apr`/`δ`/`FILL_BASE` 稳定值 | 标定完成，翻盘判定定论 |

> 不满 1 个月的数据样本太小，`apr`/`FILL_BASE` 噪声大，判定无效。

---

## 3. 伙伴采集命令 / 接口

### FILL_BASE（用现成脚本）
```bash
cd a_share
LIVE_MODE=1 PM_BOT_PK=<私钥> CLOB_HOST=<nb可达CLOB> \
  python calibrate_fill.py
# 输出：recommended_base 回填到 .env 的 FILL_BASE（或 FILL_CALIBRATE_APPLY=1 自动应用）
```

### apr / δ（lp_tool 或 CLOB rewards）
```bash
# 例：lp_tool 实测安全池中位 apr / δ（具体命令以 lp_tool 文档为准）
polymarket_lp_tool measure --markets <合规过滤后安全池> --out nb_rewards.json
# 或 CLOB /rewards 端点按市场读奖励率与带宽
```

---

## 4. 回填后北京侧重跑

伙伴回传数据后，北京侧只做「回填 + 重跑」，不瞎调算法：

```bash
cd a_share
# 1) 填真实参数（写入 .env 或 export）
export LP_REWARD_APR=<NB回传 apr>
export LP_REWARD_DELTA=<NB回传 δ>
export FILL_BASE=<NB回传 fill_base>

# 2) 用 NB 真实盘口快照替换 data/quotes_ts/*.jsonl（伙伴 scp 回传）
#    替换后重跑回测：
python backtest_lp_reward.py
# 产出：LP_REWARD_BACKTEST.md（看 lift = 双收益 vs 纯价差）

# 3) 顺带重跑成交率敏感性，确认 FILL_BASE 是否跌破盈亏临界点
python fill_sensitivity.py
# 参考既有结论：FILL_SENSITIVITY_RESULT.md（FILL_BASE 盈亏临界点附近策略转亏）
```

---

## 5. 翻盘判定门槛（诚实版）

重跑后看 `LP_REWARD_BACKTEST.md` 的 `lift_pct`：

| 条件 | 结论 |
|------|------|
| `lift > +15%~20%` **且** 真实 `FILL_BASE` 不低于假设太多（≥0.25） | "30% 净盈利"**站得住**，锚 C 成立 |
| `lift` 在 `+2%~5%` 附近（apr 仍低） | 回到"7 成不赚"，锚 C 不成立 |
| `lift ≤ 0` | 双收益为负，纯价差都优于带奖励，锚 C 证伪 |

> **诚实提醒**：即使锚 C 成立，"翻盘"也只是把 EV 从负拉到**接近零或微正**，不是稳赚。锚 C 本质是赌 `apr` 够高，赌注是 NB 那笔真实跑量。

---

## 6. 回传格式样例（请伙伴按此 JSON 回传）

```json
{
  "as_of": "2026-12-01",
  "sample_window_days": 30,
  "apr": 3.65,
  "delta": 0.015,
  "fill_base": 0.28,
  "markets_measured": 22,
  "notes": "安全池中位 apr 取自 lp_tool；FILL_BASE 取自 calibrate_fill.py 实盘 30 日"
}
```

---

## 7. 注意（避免伙伴误操作）

- ⚠️ `sim_server` DRY_RUN **不产生 LP 奖励**，所以 `apr`/`δ` 必须由 lp_tool / CLOB rewards / 真 LP 实测，不是从模拟盘读。
- ⚠️ 北京侧在回收前**不要改算法参数**——代码的定价逻辑已写好、单测全过、回测框架就位，缺的是输入数据不是代码。
- ⚠️ `FILL_BASE` 必须真钱小额挂单测（`calibrate_fill.py`），不能用模拟值回填。
- ✅ 回收期间 NB 伙伴照常跑（部署包 `DEPLOY_NB.md` + 交接 `HANDOFF_NB.md` 已就绪），满 1 月回传即可。

---

**一句话**：球已传给 NB。满 1 月回传 `apr`/`δ`/`FILL_BASE` 三个数 → 北京回填重跑 `backtest_lp_reward.py` → 看 `lift` 定锚 C 成不成立。在那之前，项目停手等数据。

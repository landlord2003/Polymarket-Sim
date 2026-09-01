# Polymarket 做市模拟验证平台（Polymarket-Sim）

> 一个 Polymarket 合规做市策略的**模拟验证平台**。默认 DRY_RUN（零真钱），用于验证风控纪律、报价逻辑与 LP 奖励感知定价；接真钱需在无合规限制的辖区独立部署并标定真实参数。

**主导**：老吴（吴自强） ｜ **协办**：Claw（AI） ｜ **状态**：研究/验证层已可用，收益层待 NB 实盘兑现

---

## 它是什么 / 不是什么

| ✅ 能做什么 | ❌ 不是什么 |
|------------|------------|
| 做市策略模拟器（库存中性双边报价锁定价差） | 自动赚钱机器 |
| 组合级风控熔断验证（日亏/回撤/本金下限） | 盈利能力保证 |
| LP 奖励半宽 δ 感知定价研究（价差+奖励双收益） | 金融投资建议 |
| 审计 / walk-forward 回测 / 钉钉报告 | 已实盘盈利的产品 |

**默认零真钱**：`LIVE_MODE=0` 时所有成交写在虚拟账本，不碰任何真实资金。平台验证的是**风控纪律与策略逻辑**，不是「能赚钱」。

---

## 架构

```
sim_server.py       策略引擎 + 看板(HTTP :8787) + 周期报告
   ├─ arbitrage.scan_poly_marketmaking()   做市机会扫描（含 LP 奖励感知 lp_reward 字段）
   ├─ risk_control.py                       风控闸门：日亏/回撤/本金下限自动 kill switch + 钉钉告警
   └─ sim_rigor.py / arb_book.py            虚拟账本 + 深度预过滤 + 时间门控
clob_exec.py         CLOB 实盘执行器（仅 LIVE_MODE=1 且持有 PM_BOT_PK 才发单）
polymarket.py        行情抓取（Gamma / CLOB 冗余源 / 本地缓存快照）
notify.py            钉钉 / 企微推送（周期报告 + 熔断告警）
lp_reward.py         #143 LP 奖励半宽 δ 感知定价（纯函数，零网络）
```

---

## 快速开始（本机 DRY_RUN）

```bash
# 1. 装依赖（Python 3.13 受管环境即可，纯标准库为主；rp 可选）
pip install -r requirements.txt

# 2. 配置（基于 NB 模板复制，至少改 SHUTDOWN_TOKEN）
cp .env.nb .env
#   LIVE_MODE=0          # 务必先 0！跑 ≥1 周观察再改 1
#   SHUTDOWN_TOKEN=...   # 随机长串，关停/复位鉴权
#   PM_BOT_PK=           # 留空（DRY_RUN 不需要）

# 3. 启动模拟盘（后台常驻）
python a_share/sim_server.py
# 浏览器开 http://127.0.0.1:8787 看看板（风控徽标/双收益/成交实时刷新）
```

北京（无外网）行情来自本地缓存快照，属正常，不影响常驻与验证。

---

## 关键能力

- **三重合规过滤**：剔除政治/地缘/军事等敏感市场（~278/300），只做 ~22 个安全池。
- **组合级自动熔断**：日亏（`DAILY_LOSS_LIMIT`）/ 回撤（`DRAWDOWN_LIMIT=0.15`）/ 本金下限（`BANKROLL_FLOOR_FRAC=0.70`）任一触限 → 自动停新单 + 钉钉告警 + 看板红灯。
- **LP 奖励感知定价（#143）**：把奖励半宽 δ 纳入报价，对比「纯价差」vs「区内挂单」blended edge 并自动择优；`/api/lp_reward` 实时看双收益 lift。
- **钉钉周期报告**：默认每 **2 小时**推送一次运行摘要（非任务完成时才推）。
- **可观测**：Prometheus 文本 `/metrics`、只读 `/api/state` `/api/risk` `/api/lp_reward`。

---

## 接真钱（必读，别跳过）

完整 SOP 见 **[POLYMARKET_ONBOARDING.md](./POLYMARKET_ONBOARDING.md)**。摘要六步：

1. 注册 polymarket.com（仅观盘）
2. KYC：Polygon ID / Civic 验证，证明**非美居民**（加拿大 NB 无碍）
3. 连 MetaMask，切 Polygon（chainId=137，与 `CLOB_CHAIN_ID` 一致）
4. 入金 USDC on Polygon（**只充 Polygon PoS，别充 ETH L1**），先小额试提
5. 填 `.env` → 零真钱预飞 `live_preflight.py` → 小资金 `LIVE_MODE=1`（先 $50–100）
6. 真实成交率标定 `FILL_BASE`（北京模拟盘 0.30 是假设值，需 NB 实盘回填）

> ⚠️ **盈利预期（诚实，非营销）**：当前 100% 模拟、0 真实样本。接真钱后 6 个月**净盈利概率约 30%（区间 10%–50%）**——这是「净收益 > 0」的概率，强依赖 ① LP 奖励套利跑通（把双边收益从纯价差扩到「价差 + 奖励」）② 真实成交率标定。**多数不赚钱的情景是微亏/持平而非大亏**（熔断锁死下行，本金不归零）**。平台验证的是纪律，不是盈利能力；任何更精确胜率要等 NB 实盘 1–3 个月真实数据。

---

## 部署（NB 伙伴交接）

见 **[DEPLOY_NB.md](./DEPLOY_NB.md)** + **[HANDOFF_NB.md](./HANDOFF_NB.md)**（9 阶段勾选清单）。

- **NB = 加拿大 New Brunswick 开放省**：无合规风险，本机直连无需代理。
- 北京实例为 DRY_RUN 模拟盘（零真钱、行情为缓存快照）；NB 部署后自动转实时 `gamma` 盘口。
- 迭代计划见 **[ITER_PLAN_2026-09-01.md](./ITER_PLAN_2026-09-01.md)**（#139 部署交接 / #140 钉钉2h / #141 风控增强 / #143 LP奖励定价 已完成；#142 本 README 进行中）。

---

## 目录结构（主线）

```
Quant-Trading/
├── a_share/                # Polymarket 模拟盘主线
│   ├── sim_server.py       # 策略引擎 + 看板 + 周期报告（入口）
│   ├── arbitrage.py        # 做市/套利机会扫描（含 LP 奖励感知）
│   ├── lp_reward.py        # #143 LP 奖励半宽 δ 感知定价（纯函数）
│   ├── risk_control.py     # 组合级风控熔断
│   ├── sim_rigor.py        # 虚拟账本 + 门控
│   ├── clob_exec.py        # CLOB 实盘执行器（LIVE_MODE 才发单）
│   ├── polymarket.py       # 行情抓取（Gamma/CLOB/缓存）
│   ├── backtest_lp_reward.py  # walk-forward 双收益验证
│   ├── test_lp_reward.py / test_risk_guard.py  # 单测（共 15 项全过）
│   └── config/strategies.json   # 策略参数（half_spread/fee/rigor）
├── POLYMARKET_ONBOARDING.md   # 注册/开户/接真钱 SOP
├── DEPLOY_NB.md / HANDOFF_NB.md  # NB 部署交接
├── ITER_PLAN_2026-09-01.md   # 迭代计划
├── Polymarket竞品分析.md       # 竞品对标（含 lp_tool 借鉴）
├── .env.example / .env.nb     # 配置模板
└── README.md
```

---

## 免责声明

- 本仓库为**研究与模拟验证软件**，不构成任何投资建议；一切交易风险由使用者自负。
- 默认 DRY_RUN 零真钱；接真钱须在合规辖区、完成 KYC、自担风险。
- 遵守你所在司法辖区的法律；本平台不鼓励规避任何合规要求。
- 模拟结果不代表未来真实表现。

---

## 参考

- 竞品对标：**[Polymarket竞品分析.md](./Polymarket竞品分析.md)**（4 项目 vs 我们的做市模拟盘；唯一同族 `polymarket_lp_tool` 的 LP 奖励定价已借鉴落地为 #143）
- 完整报告：Polymarket完整报告_2026-08-31.md / Polymarket报告_项目方意见_2026-08-31.md

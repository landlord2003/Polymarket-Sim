# NB 部署交接清单（Polymarket-Sim）

> 面向 **加拿大 New Brunswick 等开放省** 的部署伙伴。逐行打勾，走完即具备「模拟跑通 → 小资金实盘」条件。
> 详细步骤见 `DEPLOY_NB.md`（实盘 SOP）与 `DEPLOY_POLYMARKET.md`（跨机部署）。本清单只做勾选锚点。
> 代码 latest `d7450cd`（GitHub `landlord2003/Polymarket-Sim` master，2026-09-01）。
> 开户 / KYC / 接真钱 / 平台接入全步骤另见 [POLYMARKET_ONBOARDING.md](./POLYMARKET_ONBOARDING.md)。

---

## ☐ 阶段 0 · 网络与地区（硬性前置）

- [ ] 出口 IP 落 **CA / New Brunswick**（或任一开放省）：`curl -s ipinfo.io` 看 `"country":"CA"` 与 `"region":"New Brunswick"`
- [ ] **不挂 VPN**（尤其勿落 ON/AB/BC/QC 或受限国，否则 close-only / 封锁）
- [ ] 能访问 Polymarket Gamma 盘口 API（取真实 `gamma` 盘口；取不到会降级 `clob`/`cache`，看板仍可跑但非实时）

## ☐ 阶段 1 · 环境与代码

- [ ] Python 3.11+（3.13 已验证）
- [ ] `git clone https://github.com/landlord2003/Polymarket-Sim.git && cd Polymarket-Sim`
- [ ] `python -m venv .venv && source .venv/bin/activate`
- [ ] `pip install -r requirements_nb.txt`（websockets / py-clob-client / web3）
- [ ] `cp .env.nb .env` 并填值（见阶段 2）

## ☐ 阶段 2 · .env 关键配置

- [ ] `SHUTDOWN_TOKEN` = 随机长串（**务必改掉弱默认**，否则同网可关停）
- [ ] `PM_BOT_PK` = 钱包私钥（**仅 env，绝不提交**；`LIVE_MODE=1` 时必须）
- [ ] `COMPLIANCE_FILTER` = `0`（NB 无合规风险，关过滤交易所有市场）
- [ ] `LIVE_MODE` = 先 `0`（模拟跑通再改 `1`）
- [ ] `SIM_MODE` = `inv`（真实做市：跨轮持敞口、承波动、受止损/库存上限）
- [ ] `INITIAL_CAPITAL` = `5000`（默认 10000；按资金量调）
- [ ] `MM_N` = `20`、`MM_N_PER_CAT` = `5`（类别多样性上限）
- [ ] `DINGTALK_WEBHOOK` / `DINGTALK_SECRET` = （可选）配了手机每 2h 收周期报告 + 熔断告警
- [ ] 钱包往 Polymarket 充值地址打 **Polygon 上的 USDC**（最低 $3，校准用）

## ☐ 阶段 3 · 启动模拟盘（≥1 周，零风险）

- [ ] `python a_share/sim_server.py` → 浏览器开 `http://127.0.0.1:8787`
- [ ] 看 🛡️ 合规面板 = 「已关闭」、header「合规」徽章 = 「合规 关(NB无限制)」
- [ ] `/api/state` 的 `compliance_filter` = `false`、`quotes_source` = `gamma`
- [ ] `/api/risk` 正常：仓位 / 日亏 / kill switch 状态可见
- [ ] 看板新增观测项确认：行情源徽章（gamma=绿）、成交率徽章（模拟盘显示「模拟成交率 X% · 零真钱」）、做市类别分布、分散度健康行
- [ ] 点「💰 累计锁利」卡片 → 弹盈亏归因瀑布图（验证交互）
- [ ] 试「导出范围」下拉：选「最近 N 笔」→ 点「下载成交 CSV」与「导出报告」均按范围生成

## ☐ 阶段 4 · 真实成交率校准（必经，回填 FILL_BASE）

- [ ] `python a_share/calibrate_fill.py --preview`（预览价/量，不发单）
- [ ] `python a_share/calibrate_fill.py --live --markets 30 --size 3 --window 600 --rounds 2`（须 `LIVE_MODE=1` + `PM_BOT_PK`）
- [ ] 把 `recommended_base` 回填进 `.env` 的 `FILL_BASE`，重启生效
- [ ] 若 `observed_rate < 0.30`：先调 `adverse_frac` / 换高流动市场，**不要放大**

## ☐ 阶段 5 · 钉钉推送（可选但建议）

- [ ] `.env` 填 `DINGTALK_WEBHOOK` / `DINGTALK_SECRET`
- [ ] 周期报告每 **2 小时**自动推（默认 `AUTO_REPORT_MIN=120`）
- [ ] kill switch 触发即钉钉告警（状态落盘 `a_share/data/risk_state.json`，重启仍生效）

## ☐ 阶段 6 · 审计与导出（日常）

- [ ] 成交 CSV：看板「下载成交 CSV」+「导出范围」下拉（全部/最近N笔/时间区间/日期/轮次）
- [ ] 报告：看板「导出报告」或 `/api/export_report`
- [ ] 定时归档（可选）：`python a_share/sim_report.py --archive-daily`（cron / 计划任务）
- [ ] 历史 tag 回填（可选）：`python a_share/backfill_tags.py --gamma`（NB 有网，真实类目）

## ☐ 阶段 7 · 小资金实盘（热钱包 $200–500）

- [ ] 钱包只留小额热钱；主资金冷存
- [ ] `.env`：`LIVE_MODE=1`，确认 `PM_BOT_PK` 已填
- [ ] 重启 `python a_share/sim_server.py`
- [ ] 前 24–48h 盯 `/api/risk` 与钉钉告警；确认实盘挂单出现在 Polymarket 账户
- [ ] 🔴 实盘前确认：IP 自检通过、COMPLIANCE_FILTER=0、SHUTDOWN_TOKEN 已改、kill switch 钉钉可用

## ☐ 阶段 8 · 风控红线（务必知悉）

- [ ] 金融风控自动拒单：`MAX_POS_PER_MARKET` / `MAX_TOTAL_POS` / `DAILY_LOSS_LIMIT`（见 `risk_control.py`）
- [x] 组合级自动熔断（2026-09-01 已实现）：`risk_control.evaluate_portfolio_guard()` 由 `sim_server.risk_monitor_loop()` 每 `RISK_CHECK_SEC`(≈30s) 巡检——**日亏 `DAILY_LOSS_LIMIT` / 回撤 `DRAWDOWN_LIMIT`(0.15) / 本金下限 `BANKROLL_FLOOR_FRAC`(0.70) 任一触限即自动 kill switch + 钉钉告警**；看板 `#riskbadge` 转红 + 顶部横幅。已熔断幂等，需 `/api/kill_switch?action=off` 人工复位（详见 DEPLOY_NB.md §8）。
- [ ] 看板红灯自检：实盘前人为制造一次触限（或读 `/api/risk` 的 `guards` 字段）确认 `#riskbadge` 显示「⚠ 风控熔断」、钉钉收到告警
- [ ] kill switch：`/api/kill_switch?token=<SHUTDOWN_TOKEN>` 立即停新单并钉钉告警；`?action=off` 解除

## ☐ 阶段 9 · 合规与税务（属人义务不豁免）

- [ ] NB 可关合规过滤（`COMPLIANCE_FILTER=0`），但**审计留痕**始终开启（全量成交落盘）
- [ ] 税务：若构成加拿大税务居民，加密收益应税（CRA，报 T1）；保留全量交易记录
- [ ] 平台属人义务不豁免：自行承担母国资本管制 / 税务等义务

---

## 北京（DRY_RUN）实例说明（供老吴侧对照，不参与 NB 实盘）

- 北京无外网：`DRY_RUN` 零真钱、行情为离线缓存快照（非实时），`quotes_source=cache` 如实标注
- `COMPLIANCE_FILTER=1`（中国部署必须过滤敏感市场）；「模拟成交率」为模型假设值，非链上真实
- 钉钉推送同样每 2 小时（与 NB 一致），但北京不接真钱、不跑 `calibrate_fill.py --live`
- 北京实例作用 = **策略验证 + 审计链路 + 风控纪律沙盘**，为 NB 实盘提供已验证的参数与 SOP

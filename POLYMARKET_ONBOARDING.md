# Polymarket 开户 / 接真钱 / 平台接入 指南

> 配套文档：`DEPLOY_NB.md`（NB 部署步骤）、`HANDOFF_NB.md`（伙伴勾选清单）、`clob_exec.py`（实盘发单模块）。
> 状态：本指南为 **2026-09-01** 版，平台侧步骤以 Polymarket 官网实时流程为准；**平台接入段与代码 1:1 对应**。

---

## ⚠️ 适用边界（先读）

- **本平台当前 100% 模拟（DRY_RUN）**，北京实例零真钱、行情为缓存快照非实时。接真钱只在 **NB（加拿大 New Brunswick 等开放省）** 由合作伙伴部署后发生。
- **美国用户被 Polymarket 禁止交易**（地缘/监管）。NB 伙伴物理 IP 落加拿大开放省、无 KYC 障碍、可实盘。
- **私钥只进环境变量 `PM_BOT_PK`，绝不入代码 / 不入 git / 不打日志**（见 `.gitignore`、`.env` 已被排除）。
- 所有实盘发单前强制过 `risk_control.check_new_order()`；触限即放弃，绝不绕过。

---

## 一、注册 Polymarket 账号（平台侧）

1. 浏览器打开 **https://polymarket.com**（需 NB/海外网络，无墙）。
2. 点 Sign Up：
   - 方式 A：**邮箱 + 密码**（最省事，推荐伙伴用）；
   - 方式 B：连接钱包（MetaMask 等，见第三节）。
3. 邮箱验证 → 账号建立。此时**只能看盘、不能下注**。
4. 交易前必须完成 KYC（第二节），否则 `place_order` 会被拒。

> 平台侧注册与我们的代码无关，账号归伙伴自己所有，凭据自行保管。

---

## 二、KYC 开户（Polygon ID / Civic）

Polymarket 交易需身份验证（美国除外）。流程：

1. 在 polymarket.com 进入 **Account → Verify**（或首次尝试交易时弹窗引导）。
2. 走 **Polygon ID**（由 Civic 提供）验证：
   - 上传身份文件 / 人脸活体；
   - 证明你**非美国居民**（居住国填加拿大 NB 即可）。
3. 验证通过后账号解锁交易权限，生成你的 **Polymarket 用户身份**。
4. 耗时通常几分钟到数小时，取决于审核队列。

> **诚实提示**：KYC 是平台侧合规动作，我们代码不碰身份；万一 Civic 对某地区收紧，伙伴需按官网最新指引处理，本指南不担保一定能过。

---

## 三、连接钱包（我们的代码要读它）

我们的 `clob_exec.py` 用 **EOA 钱包私钥** 签名发单（CLOB L2 认证 + 链上结算），所以必须有一个 **Polygon 上的钱包**：

1. 装 **MetaMask**（或任意支持 Polygon 的 EOA 钱包）。
2. 网络切到 **Polygon Mainnet**（chainId = `137`）—— 与 `.env.nb` 里 `CLOB_CHAIN_ID=137` **必须一致**。
3. 新建/导入一个**专用于本机器人的钱包**（不要和日常钱包混用）。
4. 导出该钱包的 **私钥（64 位 hex）**，只填进 `PM_BOT_PK` 环境变量。
   - MetaMask：Account → Account Details → Export Private Key。
   - ⚠️ 这个私钥能直接转走钱包里所有 USDC，**务必专钱包、小额、离线备份**。

> CLOB 链是 Polygon（不是以太坊主网）。充错链（如充到 ETH L1）会丢币。

---

## 四、入金（USDC on Polygon）

1. 在交易所（Coinbase / Kraken /  etc.）买 **USDC**。
2. 提币到你的 Polygon 钱包地址，**网络选 Polygon (PoS)**，代币 **USDC**（不是 USDT、不是 ETH）。
3. 小额试提一笔（如 $10）确认到账，再补足额。
4. 机器人只动这个钱包的 USDC，入金动作完全在 Polymarket/CLOB 之外，由伙伴自行操作。

> 起步资金建议 **$50–100**（对应 HANDOFF_NB §9 小资金实盘），验证链路通了再加权。

---

## 五、我们的平台如何接入（架构）

```
┌─────────────────┐   策略意图    ┌──────────────────┐   风控闸门   ┌─────────────────┐
│  sim_server.py  │ ───────────▶ │  risk_control.py │ ──────────▶ │  clob_exec.py   │
│ (做市/套利策略) │              │ (限额/熔断/拒单) │             │ (CLOB 实盘发单) │
└─────────────────┘              └──────────────────┘             └────────┬────────┘
       │ 盘口来源                                                        │ L2 签名 + 链上
       ▼                                                                ▼
┌─────────────────┐                                          ┌─────────────────────┐
│ ws_polymarket.py│ 实时盘口(wss) / Gamma / CLOB             │  Polymarket CLOB     │
│  / Gamma / CLOB │ 降级                                       │  (真钱成交)          │
└─────────────────┘                                          └─────────────────────┘
```

- **策略层** `sim_server.py`：生成做市/套利挂单意图（买/卖、价、量）。
- **风控层** `risk_control.py`：每笔过 `check_new_order()`（单市场/总仓/日亏限额），触限拒单；**组合级熔断**（日亏/回撤/本金下限）自动 `trigger_kill_switch` + 钉钉告警 + 看板红灯。
- **执行层** `clob_exec.py`：LIVE 模式下惰性导入 `py-clob-client`，`ClobClient.create_or_derive_api_creds()` 派生 **L2 凭证**（api_key/secret/passphrase，HMAC-SHA256），`post_order` 真发单；DRY_RUN 仅模拟。
- **盘口层** `ws_polymarket.py` / `polymarket.fetch_poly_quotes`：实时盘口（需 NB 出网）；北京用缓存快照。

---

## 六、接真钱实操步骤（NB 伙伴 SOP）

> 前提：已完成 `DEPLOY_NB.md` §5 部署，服务跑通 DRY_RUN，钉钉/关停鉴权已配。

**① 填 .env（基于 `.env.nb` 复制）**
```bash
cp .env.nb .env
# 编辑 .env：
SHUTDOWN_TOKEN=<随机长串>
PM_BOT_PK=<Polygon 钱包私钥64位hex>
LIVE_MODE=0          # 先 0！跑 ≥1 周模拟观察再改 1
COMPLIANCE_FILTER=0  # NB 无合规风险可关；想保守保留也行
CLOB_HOST=https://clob.polymarket.com
CLOB_CHAIN_ID=137
```

**② 零真钱预飞（验证订单构造，不碰钱）**
```bash
python a_share/live_preflight.py --snapshot a_share/live_preflight_snapshot.json
# 或 NB 有网：python a_share/live_preflight.py --live
# 看 live_preflight_report.json：每单 12 条校验 + 拟发 OrderArgs + 影子成交
```

**③ 派生 L2 凭证自检（确认钱包/链连通）**
```bash
python a_share/clob_exec.py
# 输出 LIVE_MODE / 有 PM_BOT_PK / 凭证摘要（api_key 前缀）
```

**④ 小资金实盘（改 LIVE_MODE=1，先 $50–100）**
```bash
# 重启服务（stop 旧 8787 监听后再起）
LIVE_MODE=1 PM_BOT_PK=<...> python a_share/sim_server.py
```
- 首跑紧盯 `/api/state` 的 `live_mode` 与 `risk_guard`；
- 看板红灯 = 风控熔断，立即查钉钉告警。

**⑤ 真实成交率标定（FILL_BASE，盈利定论前提）**
```bash
# 实盘跑几天后，用真实成交回填 FILL_BASE（见 NB_CALIBRATE_FILL_SOP.md）
python a_share/calibrate_fill.py --live
# FILL_CALIBRATE_APPLY=1 时由 main 应用 recommended_base
```
> **关键**：北京模拟盘的 `FILL_BASE=0.30` 是假设。真实成交率只有 NB 小资金实盘才能测出，**这是"能否盈利"的定论前提**，不是可跳过项。

**⑥ 风控红线（永远不许动）**
- `DAILY_LOSS_LIMIT` / `DRAWDOWN_LIMIT=0.15` / `BANKROLL_FLOOR_FRAC=0.70` 触发即自动停新单；
- 手动 kill switch：`/api/kill_switch?token=<SHUTDOWN_TOKEN>`；解除 `?action=off`。

---

## 七、盈利预期（诚实，非营销）

### 一句话结论
接真钱后 6 个月**净盈利概率 ~30%（区间 10%–50%）**。当前 100% 模拟、0 真实样本，所以任何具体胜率都是**下注、不是证据**。

### 1. 为什么"当前不能保证盈利"
- 我们跑的是 **DRY_RUN**：所有成交写在模拟账本，零真钱进出。统计上没有任何一分钱真实 P&L，无法引用真实胜率。
- 我们验证过的是"**风控纪律不崩**"（熔断/拒单逻辑正确、看板红灯、钉钉告警链路通），**不是"策略能赚钱"**。

### 2. 为什么纯价差做市是负期望
- **taker fee**：吃单约 0.05%，被逆向选择时常是 taker，反被收费；
- **maker rebate 有限**：挂单返佣很薄，cover 不住不利成交；
- **逆向选择（最关键）**：信息优势方总吃你不利方向的单，有利单少成交 → 成交分布系统性不利；
- **库存风险**：持有结果代币，结果偏向不利时浮亏，且 Polymarket 结算前无法对冲。
- 业界共识：散户/业余做市者**多数净亏**。

### 3. 我们的差异化（保命，但不产生 alpha）
1. **三重合规过滤**：砍掉 ~278/300 政治/地缘/军事市场 → 只做 ~22 个安全池。减黑天鹅，但**不增收益**；
2. **组合级自动熔断**：日亏/回撤/本金下限触限即停新单 → **不归零，但也不赚钱**；
3. **LP 奖励套利（#143，待做）**：Polymarket LP 有持币时间加权奖励 δ，是**真实正向现金流**。模拟里**还没算这笔**，NB 有网才能回填真实 δ。

### 4. 为什么是 ~30%（区间 10%–50%）
| 情景 | 6 个月净盈利概率 | 说明 |
|------|------------------|------|
| 纯价差做市（当前设计） | < 25% | 负期望 + 逆向选择，多数亏 |
| + LP 奖励套利跑通（#143） | 抬到接近零 / 微弱正 | 双边收益 = 价差 + 奖励 |
| + FILL_BASE 真实标定 | 中值 ~30% | 0.30 是假设，真实值决定净锁利虚实 |
| **乐观（奖励跑通 + 成交率好 + 风控少误杀）** | **~50%** | 上限 |
| **悲观（奖励没跑通 / 成交率差 / 逆向选择重）** | **~10%** | 下限 |

→ 取中值 **30%**，区间 **10%–50%**。

### 5. 最大风险：薄利被吃光，不是亏光本金
- 熔断锁死下行，**本金不会归零**；
- 但做市是薄利生意，费率 + 逆向选择 + 库存可能让长期净收益在 **0 附近甚至微负** → "跑了但没赚到钱"比"亏光"更可能。

### 6. 数据前提
任何更精确胜率，要等 **NB 实盘 1–3 个月真实数据**（真实成交率、真实 δ、真实逆向选择强度）。在此之前都是先验下注。

---

## 八、常见坑

| 坑 | 现象 | 解决 |
|----|------|------|
| 充错链 | USDC 充到 ETH L1 | 只充 Polygon (PoS)，chainId 137 |
| 私钥泄露 | 钱包被转空 | 专钱包 + 小额 + 仅 env，绝不提交 |
| LIVE_MODE=1 但无 PK | `ClobExec` 抛 RuntimeError | 必须同时填 `PM_BOT_PK` |
| 北京误开 LIVE | 真发单 | 北京 `LIVE_MODE` 永远 0；实盘只在 NB |
| FILL_BASE 用假设值 | 净锁利虚高 | NB 小资金实盘标定后再 apply |
| token_id 格式 | 误当 0x 十六进制 | 真实是十进制大整数（最长 78 位） |
| 端口冲突 | 重启报 failed | 先杀旧 8787 监听 PID 再起 |

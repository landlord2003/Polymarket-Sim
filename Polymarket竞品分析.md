# Polymarket 开源竞品分析 —— 对照我们的 Quant-Trading 做市模拟平台

> 分析日期：2026-09-01
> 对照对象：我们的 `landlord2003/Quant-Trading`（DRY_RUN 合规做市模拟盘 + 真实盘口轮动）
> 竞品：PolymarketBTC15mAssistant / CloddsBot / Polymarket-bot / polymarket_lp_tool

---

## 0. 一句话定位对照（信号灯：🟢我们领先 🟡各有长短 🔴我们偏弱）

| 项目 | 真实定位 | 与我们同类度 | 实盘 | Dry-run | 核心差异 |
|---|---|---|---|---|---|
| **我们的 Quant-Trading** | 合规做市(MM spread capture) **研究/验证**平台 | — | ❌ 纯模拟 | ✅ 默认 | 三重合规过滤 + 本地零云 + 最深看板 |
| **PolymarketBTC15mAssistant** | BTC 15m **方向性下注**信号助手 | 低（方向性≠做市） | ✅ 有(私钥) | ✅ 有 | TA+Chainlink+Binance 多源冗余 |
| **CloddsBot** | 通用 **AI 交易 Agent**（跨 1000+ 市场） | 低（通用 Agent） | ✅ 多平台 | ✅ 默认 | 118+策略+22渠道+MCP+完备风控 |
| **Polymarket-bot**(guberm) | Polymarket **方向性事件交易** Agent | 低（方向性） | ✅ paper+live | ✅ paper | AI 公允价值集成 + Kelly + 链上校验 |
| **polymarket_lp_tool** | **LP 流动性奖励**被动调价工具 | 🟡 高（同属做市族） | ✅ 真挂单调价 | ⚠️ 不新建单 | 吃平台奖励，Rust 重写 |

**关键认知**：4 个里只有 `polymarket_lp_tool` 和我们同属「做市/提供流动性」族；其余 3 个都是**方向性下注/通用 Agent**，和我们的「双边报价吃价差」本质不同。所以「借鉴」要分两层——做市族借鉴定价/风控，方向性族借鉴风控/部署/工程纪律。

---

## 1. PolymarketBTC15mAssistant（@krajekis，Node.js 控制台）

- **定位**：BTC「Up/Down」15 分钟市场的实时控制台信号助手。
- **策略**：方向性 LONG/SHORT。输入 = Polymarket WS 盘口 + Chainlink BTC/USD（链上兜底）+ Binance 现货参考；信号引擎 = Heiken Ashi/RSI/MACD/VWAP/Delta 打分 → 输出 LONG/SHORT% 与 ENTER/NO_TRADE。
- **实盘**：支持私钥下单（`POLY_TRADING_ENABLED` + `DRY_RUN` 两道开关，先模拟后实盘）。
- **技术**：Node.js 18+，纯终端 UI（readline 清屏）。
- **风控**：TP/SL（`STRATEGY_TAKE_PROFIT_*`/`STOP_LOSS`）、EV 价格区间封禁（`STRATEGY_EV_STRICT_PRICE_BAND`）、RSI 背离过滤。
- **数据源**：Polymarket WS + Chainlink on Polygon（HTTP/WSS RPC 兜底）+ Binance。
- **优势（可借）**：
  - 🟡 **代理支持（HTTP+WS proxy）**——北京部署 Polymarket 必需，我们接真钱时必补。
  - 🟡 **多价格源冗余**（WS 挂了自动 Chainlink/Binance 兜底）——我们盘口目前单源。
  - 🟡 **回测**（PolyBackTest 重放快照跑策略引擎）——我们有 CLOB prices-history 回测，思路一致。
- **劣势**：只做 BTC 15m 单一标的；纯方向性（和我们做市不同类）；无 GUI、无合规过滤、无做市。

## 2. CloddsBot（alsk1992，TypeScript/Node AI Agent）

- **定位**：Claude 驱动的通用 AI 交易终端，跨 1000+ 市场（10 预测市场 + 7 期货合约 + Solana/EVM DeFi）。
- **策略**：118+ 策略（延迟套利/动量/均值回归/Penny Clipper/智能路由/DCA/到期衰减/鲸鱼/跟单），4 个专用 Agent（Main/Trading/Research/Alerts）。
- **实盘**：多平台下单，**默认 dry-run**；x402 机器对机器 USDC 支付。
- **技术**：TypeScript/Node；内置 WebChat（类 Claude 界面）+ 22 消息渠道（TG/Discord/WhatsApp…）+ **MCP Server**（119 技能暴露给 Claude Desktop/Code）。
- **风控**：统一风险引擎——circuit breaker、VaR/CVaR、波动率 regime 检测、压力测试、**Kelly sizing、每日亏损上限、kill switch**；交易账本 SHA-256 审计。
- **优势（可借）**：
  - 🔴 **完备风控引擎**（circuit breaker / daily loss / drawdown / bankroll floor）——我们只有 killSwitch + 合规过滤，这是最大短板可补项。
  - 🟡 **MCP Server 暴露能力**——把我们看板数据/归因/CSV 导出暴露为 MCP 工具，可让 WorkBuddy/Claude 直接调，契合老吴生态。
  - 🟡 默认 dry-run 哲学（和我们一致）、多语言含 ZH。
- **劣势**：过于庞杂（学习曲线陡、难审计）；强依赖 Anthropic key（API 费用）；隐私弱（需出网）；和我们的「做市研究」定位不同（它是通用 Agent，不是做市验证器）。

## 3. Polymarket-bot（guberm 为主，命名拥挤有多个变体）

> 注意 `Polymarket-bot` 是拥挤命名，主流有：guberm/polymarket-bot（AI 公允价值）、ynzheng/Polymarket-bot（HFT 双策略 105+测试）、nick9248/polymarket_bot（收益 farming+跟单）、tugcantopaloglu/polymarket-automation（多策略+Next.js+回测）。以下以 **guberm** 为主对照。

- **定位**：Polymarket 自主交易 Agent——AI 集成估公平概率 → 找错价 → Kelly 下单。
- **策略**：每 N 分钟：余额同步 → 幽灵检查（链上持仓校验）→ 持仓复核（止损/止盈/edge-gone/重估）→ 找净错价>10% → 新鲜 CLOB 订单簿算 VWAP → 分数 Kelly 下单。
- **实盘**：paper + live（CLOB GTC limit）；`bot.lock` 防 Python/.NET 并发抢同一钱包。
- **技术**：Python + .NET 双实现（逻辑/配置/数据格式一致）；Electron dashboard；dashboard 可把 bot 路由进 VPN/proxy。
- **风控**：**bankroll floor 硬停（组合价值<$1 即停）**、每日止损、回撤护栏、cooldown（平仓后 2 轮禁重入）、penny 仓位跳过。
- **数据源**：Gamma + CLOB + AI ensemble（Anthropic/Gemini/OpenAI，多供应商打分）。
- **优势（可借）**：
  - 🔴 **幽灵检查 / 链上持仓校验**——接真钱时防「tracked position 已不在链上」漂移，模拟盘可预留接口。
  - 🔴 **bankroll floor 硬止损 + 每日亏损上限**——和 CloddsBot 风控互补，我们缺。
  - 🟡 **AI 多供应商公平值**——可用来给做市报价加 skew（公平值偏离中点时偏移报价）。
  - 🟡 **双语言实现一致**——工程参考（我们也可考虑核心逻辑单测与多前端解耦）。
- **劣势**：方向性事件交易（非做市）；需 AI key 费用；无合规过滤；实盘风险高；dashboard 仅 Electron。

## 4. polymarket_lp_tool（lihanyu81，Python + Rust）

- **定位**：**LP 流动性奖励**工具——你手动在前端挂单，它只轮询你密钥下的未成交单，按订单簿 + 激励半宽 δ 做 保持/撤单/同量改价重挂。**不是自动做市机器人（不新建单）**。
- **策略**：确定性简单规则（粗 tick / 细 tick / 自定义规则），目标是把报价压在平台**奖励区**内赚流动性奖励（实测 1%+ 日化）。
- **实盘**：✅ 真挂单调价（但依赖你先手动挂）；2.0 已 **Rust 重写**（tokio + tungstenite + tracing，WS 优先）。
- **技术**：Python 保留参考 + Rust 主实现；web panel（`run_web_panel.py`）+ 每单自定义定价规则。
- **风控**：midpoint jump filter（反狙击）、EMA/中位数过滤、fill 后 cooldown、单次最大追价限制、风险指标告警。
- **数据源**：CLOB V2 orderbook WS。
- **优势（最该借，同族）**：
  - 🔴 **LP reward 感知定价**——我们当前做市**只吃价差，完全没吃 Polymarket 真实 LP 奖励**。把「奖励半宽 δ」纳入报价模型，既赚价差又赚平台奖励，是真实做市最被低估的收益源。可在 sim_server 的 MM 定价里做**模拟验证**。
  - 🟡 **反狙击保护**（midpoint jump / EMA 过滤 / fill cooldown）——我们做市成交模拟可加同类防护，避免被夹。
  - 🟡 **Rust 性能路径**——我们 Python 看板/模拟够用，但真高频执行可参考其 WS-first Rust 架构。
- **劣势**：只做 LP reward farming（非价差做市）；不新建单（依赖手动先挂）；无方向性/套利；需真钱挂单才有意义；北京无外网部署难。

---

## 5. 我们的独门优势（别人都没有）

| 优势 | 说明 |
|---|---|
| 🟢 **三重合规过滤** | 政治/地缘/军事/中东航运咽喉市场自动剔除——别人全无此隔离，而这恰是 Polymarket 最高不确定性/政策风险区 |
| 🟢 **本地零云 + 隐私强** | 北京无外网也能跑模拟（离线缓存盘口），数据不出本机；别人大多需出网/API key |
| 🟢 **参数扫描面板 + CLOB 回测** | half_spread×fee_rate 网格扫描 + prices-history 回测，别人多无此研究向工具 |
| 🟢 **看板交互最深** | 三表点击弹详情 + 复制/搜索 + 成交表筛选 + CSV/报告范围三选一 + KPI 归因弹窗 + 权益曲线 K 线，别人多为控制台/Electron/WebChat |
| 🟢 **DRY_RUN 验证闭环完整** | 守护进程 + killSwitch + 范围导出 + 回归测试(20项)，接真钱前可充分验证 |

---

## 6. 可借鉴清单（按优先级，带落地点）

### 🔴 高价值、可直接落地（建议优先）
1. **LP reward 感知定价**（借 lp_tool）
   - 在 `sim_server.py` 的 MM 报价模型加「奖励半宽 δ」：把双边报价压在 Polymarket 奖励区内，模拟验证「价差+奖励」双收益。
   - 落点：新增 `lp_reward_band()` 定价函数 + 看板加「奖励区」可视化 + 回测对比纯价差 vs 价差+奖励。
2. **风控引擎**（借 CloddsBot + guberm）
   - 加：每日亏损上限(daily loss limit)、回撤护栏(drawdown guard)、资金底线硬停(bankroll floor)。
   - 落点：`sim_server.py` 加 `RISK_GUARD` 配置 + 触发时自动 killSwitch + 看板红灯告警。
3. **幽灵检查/链上持仓校验接口**（借 guberm）
   - 模拟盘预留 `ghost_check()` 接口，接真钱时校验 tracked position 是否还在链上。

### 🟡 中价值
4. **代理支持**（借 BTC15mAssistant + guberm）：CLOB client 层加 HTTP+WS proxy 配置——北京接真钱部署 Polymarket 必需。
5. **测试纪律**（借 ynzheng 105+ tests）：我们刚 13 项，补做市定价/风控/fill 模拟单测。
6. **自动优化器**（借 ynzheng）：half_spread×fee_rate 面板已有，加自动寻优（网格/贝叶斯）输出最优参数。
7. **MCP Server 暴露**（借 CloddsBot）：把看板数据/归因/CSV 导出暴露为 MCP 工具，供 WorkBuddy/Claude 直接调。

### 🟢 低/观望（超出当前 scope）
8. AI fair-value ensemble（guberm）：方向性估值，可给报价加 skew，但非必需。
9. 多平台/跟单/鲸鱼（CloddsBot/nick9248）：与「合规做市研究」定位不符，暂不做。

---

## 7. 结论

我们**不是**要变成 CloddsBot 那样的通用 Agent，也**不是** BTC 15m 方向下注。我们的定位是**合规做市研究/验证平台**，最该借的是：
- `polymarket_lp_tool` 的 **LP-reward 定价**（同族、直接增益）
- `CloddsBot`/`guberm` 的 **风控引擎**（接真钱前的必需护栏）
- `BTC15mAssistant`/`guberm` 的 **代理支持**（北京部署必需）

其余（AI Agent、多平台、跟单、鲸鱼）是噪音，不碰。

**建议下一步**：先落地 🔴① LP-reward 感知定价（在模拟盘验证双收益），再补 🔴② 风控引擎。两者都不碰真钱，纯模拟验证，符合我们数据边界。

> **进度（2026-09-01）**：🔴① LP-reward 感知定价**已落地**（`lp_reward.py` + `scan_poly_marketmaking` 接入 + `backtest_lp_reward.py` 1316 帧 walk-forward，保守假设下双收益 +2.53%、高奖励率下 +50%）；🔴② 风控引擎**已落地**（#141 组合级自动熔断 + 看板红灯）。两者均纯模拟验证，零真钱。

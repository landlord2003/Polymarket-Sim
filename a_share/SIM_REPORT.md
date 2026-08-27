# Polymarket 模拟盘运行报告（真实市场数据 · 虚拟资金）

> 生成时间：2026-08-28 ｜ 数据：Polymarket Gamma 实时行情 ｜ 资金：虚拟 $10,000（绝不触碰真实下单/钱包）
> 状态：**实盘化严谨度建模（Task #66）已接入自动化**；**时间衰减门控 + 单市场日成交上限（Task #71）已接入**；**Phase 6 自动流水线 sim_pipeline.py + 钉钉推送（Task #73）已完成**；当前为模拟层稳定期累积中。

## 一、目标对齐
- **终点**：由你拍板是否实盘（USDC/加密类市场）。
- **本阶段**：真实数据模拟盘 → 自动跑 → 逐笔记录 → 反馈迭代 → 看稳定性 → 你决定是否接真钱。
- **红线（已守住）**：所有"成交"只在 `VirtualBook` / `RigorVirtualBook` 内走虚拟资金；不调用任何真实下单/钱包接口。

## 二、MVP 基线（静态账本 · 无摩擦，仅供对照）
| 轮 | 纯套利候选 | 做市机会 | 已实现盈亏 |
|---|---|---|---|
| 0 | 20 | 20 | $0.00 |
| 1 | 20 | 20 | **$49.90** |
| 3 | 20 | 20 | **$99.79** |
| 5 | 20 | 20 | **$149.69** |

- 做市 30 笔成交**全部成功**，累计 $149.69，**胜率 100%**（静态账本高估，见第三节）。

## 三、关键认知修正（避免假利润）
1. **单二元市场内不存在 `yes_ask+no_ask<1` 的瞬时套利**：YES/NO 严格互补，`yes_ask+no_ask = 1+spread ≥ 1` 恒成立。
2. **真实无风险套利 = 同事件多结果完备集 Dutch Book**（买齐 A/B/C 所有结果 ask，若 sum<1 则到期必兑付 $1）。但**完备性无法自动验证**。
3. **纯套利默认只作"待确认候选"展示、不自动执行**：扫到的候选（平均 edge ~$0.88）多为价格极低的**不完备集假信号**，全部标 `need_confirm`，自动执行被禁用。

## 四、实盘化严谨度建模（Task #66 · 已接入）
以 `RigorVirtualBook(VirtualBook)` 子类实现，**完全不影响 webui 使用的 VirtualBook**。

| 摩擦项 | 模型 | 参数（config/strategies.json → sim_rigor） |
|---|---|---|
| 走簿滑点 walk-the-book | 单笔 size 超过顶档深度时逐档向更差价成交 | `depth_frac=0.01`, `tick=0.002` |
| 对冲不利漂移 adverse selection | 对冲基准价 = `ask − adverse_frac×spread` 再走簿 | `adverse_frac=0.30` |
| 多腿腿风险 FOK/leg-risk | 纯套利买齐时最薄腿成交率<1 → 残余未对冲库存 | 复用 `depth_frac` 推导每腿深度 |
| 深度可行性门槛 | 单笔成交额 > `liquidity×min_depth_ratio` 直接跳过 | `min_depth_ratio=0.10` |

> 真实 CLOB `/book` 深度在本环境被地域限制 404，故用 `liquidity` 推导**合成深度曲线**（模拟假设，可调）。参数全部外置到 `config/strategies.json`。

## 五、严谨度模型 + 门控下的运行结果（最新实时）
截至 2026-08-28 00:52（方法论版本 v20260828_005201，流水线单轮 2 轮取样）：
| 指标 | 数值 | 说明 |
|---|---|---|
| 虚拟本金（equity） | **$10,148.02** | 相对初始 $10,000 已累积 |
| 本轮新增做市成交 | 6 笔 | 本轮取样 |
| MM 累计实现盈亏 | **$96.02** | 含滑点/漂移后真实口径 |
| MM 胜率 / 净胜率 | 100% / 100% | 当前样本集中于高流动性市场（见警示） |
| 累计滑点成本 | $0.70 | 薄市场走簿产生 |
| **门控跳过 — 深度** | 0 笔 | 深度门槛 |
| **门控跳过 — 时间衰减** | 22 笔 | `min_time_to_settle_h=6h` 临近结算硬门控 |
| **门控跳过 — 单市场日上限** | 5 笔 | `daily_cap_notional=$500/24h` 滚动窗口 |
| 纯套利候选 | 60 个 | 平均 edge $0.8841，成交率 100%，**全部 need_confirm** |

- **核心结论**：严谨度模型 + 双门控把"看起来稳赚"的机会做了诚实过滤——时间衰减门控已主动跳过 22 笔临近结算、价格极易跳变的市场；单市场日上限已跳过 5 笔过度暴露的重复锁利。这是对 MVP 100% 胜率高估的**进一步修正**。
- ⚠️ 当前 100% 胜率**不可外推实盘**：样本集中于高流动性、远离结算的市场（正是门控放行的子集）；需拉长周期、覆盖更多时段/流动性环境再下稳定性结论。

## 六、反馈机制输出（sim_feedback.py）
- 分析逐笔记录：做市全胜、含滑点；纯套利 0 执行 / N 候选（edge ~$0.88 为假信号）。
- 自动建议：①纯套利须人工审核事件结果互斥性后才可启用；②即便启用滑点，样本仍偏流动性高，拉长周期再定稳定性；③若出现净亏笔，应降 `default_size`/提 `min_liquidity`/`mm_min_spread`。
- 方法论版本记录于 `a_share/sim_logs/feedback_YYYYMMDD.json`（可演进）。

## 七、理想化假设整改进度
| 假设 | 状态 |
|---|---|
| 盘口静止 / 对手必成交 | ✅ 已由走簿滑点 + 对冲不利漂移修正 |
| 无滑点 | ✅ 已建模（walk-the-book） |
| 多腿完备集腿风险 | ✅ 已建模（成交率/残余库存） |
| 无时间衰减 / 临近结算风险 | ✅ **已加 `min_time_to_settle` 时间衰减门控（Task #71）** |
| 单市场重复锁利无上限 | ✅ **已加单市场日成交上限（Task #71）** |

### 七-B、时间衰减门控 + 单市场日成交上限（Task #71 · 已接入）
在 `RigorVirtualBook` + `sim_trader` 做市主循环前增加两道硬门控（参数外置于 `config/strategies.json → sim_rigor`）：

**1) 时间衰减门控（临近结算风险）**
- `min_time_to_settle_h=6.0`：距结算 < 6 小时的市场直接**硬跳过**（价格极易在最后时刻跳变，模拟无法对冲）。
- `time_decay_ref_h=72.0` / `time_decay_max=0.20`：在 [6h, 72h] 区间**线性软惩罚**——距结算越近，对冲基准价额外叠加 `penalty×spread`（0 在 72h → 0.20×spread 在 6h），越临近越保守，自然压缩临近暴露。
- 无 `end_date` 的市场不门控（如永续/无到期，照常参与）。

**2) 单市场日成交上限（过度暴露风险）**
- `daily_cap_notional=500.0` / `daily_cap_window_h=24.0`：按 `market_id` 维护**滚动 24h 累计名义成交额**，存于 `a_share/sim_daily_caps.json`；单笔 `size×price` 加进去若超 $500 则**跳过**该市场，防止对同一市场反复锁利导致尾部集中暴露。
- 状态文件每笔成交后追加并剪枝旧窗口，持久化、可跨运行累积。

**落地位置**：`a_share/sim_rigor.py`（门控函数 `time_gate_ok` / `time_decay_penalty` / `RigorVirtualBook.volume_gate_ok` + 持久化）、`a_share/sim_trader.py`（主循环两道 `continue` 跳过 + 落账 `sim_daily_caps.json`）、`a_share/sim_feedback.py`（新增 `mm_skip_time` / `mm_skip_cap` 计数与建议）。

### 七-C、Phase 6 自动流水线 sim_pipeline.py + 钉钉推送（Task #73 · 已接入）
把"跑模拟 → 写 summary → 跑反馈 → 推钉钉"串成一条命令，替代原先手动两步：
- `python a_share/sim_pipeline.py --runs N [--push-dingtalk] [--verbose]`
- 流程：强制走 `127.0.0.1:18081` 代理 → 跑 `sim_trader.run_once` N 轮 → 写 `a_share/sim_logs/summary_YYYYMMDD.json` → 跑 `sim_feedback.main()` → 读最新反馈 → `build_markdown` 组装报告 → 经 `notify.send_markdown`（凭证 `DINGTALK_WEBHOOK`/`DINGTALK_SECRET` 来自 `.env`）推送。
- 已实跑验证：6 轮 + 4 轮 `--push-dingtalk` 均 `errcode=0` 推送成功，消息含 MM pnl / 胜率 / 滑点 / 门控跳过数 / 纯套利候选数 / 方法论版本。
- 自动化已改跑该流水线（见第九节），你将在手机上周期性收到运行报告。

## 八、文件清单
- `a_share/sim_rigor.py` — 严谨度模型 + `RigorVirtualBook` + **Task #71 时间衰减/日上限门控与持久化**
- `a_share/sim_trader.py` — 模拟盘引擎（已切 `RigorVirtualBook`，含深度预过滤 + 双门控跳过 + 滑点日志）
- `a_share/sim_feedback.py` — 反馈迭代（滑点/净亏/成交率 + **门控跳过计数**）
- `a_share/sim_pipeline.py` — **NEW（Task #73）** Phase 6 流水线：跑 N 轮 → summary → feedback → 钉钉推送
- `a_share/sim_daily_caps.json` — **NEW** 单市场日成交上限滚动状态（持久化）
- `a_share/arbitrage.py` — 扫描器（marketmaking / event_arb / pure_arb），MM 机会现带 `end_date`
- `a_share/polymarket.py` — 行情抓取，quote 现带 `end_date`（供时间门控）
- `a_share/arb_book.py` — VirtualBook（webui 用，未改动）
- `a_share/config/strategies.json` — **新增 `sim_rigor` 段**（含 Task #71 五个门控参数）
- `a_share/sim_logs/*` — 逐笔成交 / summary / feedback（按日）
- `a_share/sim_book_poly.json` — 模拟盘独立账本（不污染 webui）

## 九、自动化调度
- 已建自动化「Polymarket 模拟盘自动交易+反馈」（每 6 小时）：现改跑 `sim_pipeline.py --runs 1 --push-dingtalk`，**先检测代理 127.0.0.1:18081 连通性，不通则跳过**，通则跑流水线并推钉钉。
- 你将在手机钉钉周期性收到：MM 盈亏 / 胜率 / 滑点 / 门控跳过数 / 纯套利候选数 / 方法论版本。
- 依赖代理常驻；代理宕时运行会跳过（不报错、不污染数据）。

## 十、风险与下一步
- ⚠️ 当前 100% 胜率为"高流动性样本 + 模拟摩擦 + 门控放行子集"下的结果，**不可直接外推实盘**；须累积更长周期（跨多时段/多流动性环境）再定稳定性。
- 下一步：①**纯套利完备性人工审核流程**（你拍板后才可能放开 `allow_pure_unconfirmed`）；②稳定性达标后，再谈 CLOB 真实执行（涉真钱，须你明确授权）；③门控参数（6h / $500 / 衰减曲线）可在 `config/strategies.json` 微调校准。

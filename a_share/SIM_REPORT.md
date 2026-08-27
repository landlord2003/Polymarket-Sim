# Polymarket 模拟盘运行报告（真实市场数据 · 虚拟资金）

> 生成时间：2026-08-28 ｜ 数据：Polymarket Gamma 实时行情 ｜ 资金：虚拟 $10,000（绝不触碰真实下单/钱包）
> 状态：**实盘化严谨度建模（Task #66）已接入自动化**；**时间衰减门控 + 单市场日成交上限（Task #71）已接入**；**Phase 6 自动流水线 sim_pipeline.py + 钉钉推送（Task #73）已完成**；**纯套利完备性人工审核流程（Task #75）已落地**（白名单机制接入门控）；当前为模拟层稳定期累积中（Task #78，靠 6h 自动化自动拉长周期）。

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
截至 2026-08-28 07:04（方法论版本 v20260828_070413，累计 170 条逐笔记录 / 50 笔做市执行）：
| 指标 | 数值 | 说明 |
|---|---|---|
| 虚拟本金（equity） | **$10,107.84**（现金 $9,987.10 + 持仓市值） | 相对初始 $10,000 已累积 |
| MM 累计实现盈亏 | **$120.74** | 含滑点/漂移后真实口径，样本 50 笔 |
| MM 胜率 / 净胜率 | 100% / 100% | 0 亏损笔；但见下方稳定性警示 |
| 累计滑点成本 | $0.90 | 薄市场走簿产生 |
| **门控跳过 — 深度** | 0 笔 | 深度门槛（市场流动性充足） |
| **门控跳过 — 时间衰减** | 30 笔（↑自 22） | `min_time_to_settle_h=6h` 临近结算硬门控，随运行累积增多 |
| **门控跳过 — 单市场日上限** | 5 笔 | `daily_cap_notional=$500/24h` 滚动窗口，稳定命中 |
| 纯套利候选 | 85 个（今日清单 17 个） | 平均 edge $0.8680，成交率 99.8%，**全部 need_confirm** |
| 纯套利残余库存 | 15 份（本轮首现） | fill_ratio 0.998，最薄腿未完全成交 → 腿风险实证信号 |

- **核心结论**：严谨度模型 + 双门控把"看起来稳赚"的机会做了诚实过滤——时间衰减门控随运行累积已跳过 30 笔临近结算、价格极易跳变的市场（较上周 +8 笔，证明它在持续保护）；单市场日上限稳定命中 5 笔过度暴露的重复锁利。这是对 MVP 100% 胜率高估的**进一步修正**。
- ⚠️ **100% 胜率是结构性而非样本偏差**：本模拟盘的成交利润由 `RigorVirtualBook` 的均值回归/价差捕获假设驱动，在"门控放行子集"（高流动性、远离结算）内**按构造近 100% 盈利**。真正的稳健性信号不是胜率，而是**门控在主动过滤风险**（时间衰减 30 笔、日上限 5 笔）；以及**残余库存首次出现**（15 份）证明腿风险真实存在。实盘结论仍需更长时间跨度的多时段/多流动性环境覆盖。

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

### 七-D、纯套利完备性人工审核流程（Task #75 · 已落地）
纯套利（同事件多结果 Dutch Book）的**完备性无法自动验证**——自动扫到的候选多为价格极低的不完备集假信号（平均 edge ~$0.88）。为避免"假无风险利润"被自动执行，落地一套**白名单 + 人工审核**机制：

**1) 候选清单生成（`a_share/sim_review.py`）**
- `python a_share/sim_review.py` → 拉实时行情 → `scan_poly_pure_arb` → 逐事件生成**人工可核对清单**，写入 `a_share/sim_logs/pure_arb_review_YYYYMMDD.md`（总览表 + 候选明细含每个子结果 ask/id）+ `.json`。
- 自带轻量启发式 `completeness_hint`：二元(2结果)提示"是否缺平局/第三结果"；多结果含 Other/其它 类提示"完备性大概率成立"；多结果无 Other 提示"需人工确认互斥且覆盖所有可能"。**仅辅助，不替代人工判断**。

**2) 人工确认 → 写白名单（`a_share/sim_logs/approved_pure_sets.json`）**
- 你逐组核对"结果互斥且覆盖所有可能（买齐 ask 后 sum<1，到期必兑付 $1）"。
- 确认后把 `event_id` 加入 `approved_event_ids` 数组并写 `notes`。示例：
  ```json
  { "approved_event_ids": ["202857", "746379"],
    "notes": { "202857": "NFL 2027 冠军队，结果互斥完备",
               "746379": "WTI 8月多档价位，互斥完备" } }
  ```

**3) 白名单接入门控（`a_share/sim_trader.py`）**
- `run_once` 内 `confirmed` 判定升级为三选一：**①无需确认** 或 **②全局 `allow_pure_unconfirmed=true`** 或 **③`event_id ∈ approved_pure`**。
- 命中白名单的候选即便全局开关关闭也**视为已确认、允许自动执行**；未命中仍记 `pure_candidate` 日志（含 event_id）并跳过。
- 同一逻辑对 `sim_pipeline`（含钉钉推送）自动生效——白名单数量会出现在报告"纯套利候选"备注里。
- 安全护栏：绝不提供"一键全开"隐式默认；`allow_pure_unconfirmed` 默认 `False`，仅白名单逐项放行。

**4) 当前状态**：白名单文件已建（空），今日清单 17 个候选（见 `pure_arb_review_20260828.md`）待你逐组审核。未确认前纯套利始终 0 执行。

## 八、文件清单
- `a_share/sim_rigor.py` — 严谨度模型 + `RigorVirtualBook` + **Task #71 时间衰减/日上限门控与持久化**
- `a_share/sim_trader.py` — 模拟盘引擎（已切 `RigorVirtualBook`，含深度预过滤 + 双门控跳过 + 滑点日志）
- `a_share/sim_feedback.py` — 反馈迭代（滑点/净亏/成交率 + **门控跳过计数**）
- `a_share/sim_pipeline.py` — **NEW（Task #73）** Phase 6 流水线：跑 N 轮 → summary → feedback → 钉钉推送
- `a_share/sim_review.py` — **NEW（Task #75）** 纯套利完备性人工审核清单生成器（只读行情、不成交、不推送）
- `a_share/sim_logs/approved_pure_sets.json` — **NEW（Task #75）** 人工确认的 event_id 白名单（持久化，空）
- `a_share/sim_logs/pure_arb_review_YYYYMMDD.md` / `.json` — **NEW（Task #75）** 当日候选审核清单
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
- ⚠️ 当前 100% 胜率为"高流动性样本 + 模拟摩擦 + 门控放行子集"下的**结构性结果**（非样本偏差），**不可直接外推实盘**；真正的稳健性信号是门控在主动过滤（时间衰减 30 笔 / 日上限 5 笔）与残余库存（15 份）已现。
- 下一步：①**纯套利审核**（Task #75 流程已就绪）——你逐组核对 `pure_arb_review_20260828.md`，把确认完备的 `event_id` 写入 `approved_pure_sets.json`，下一轮即自动执行；②**稳定性拉长周期（Task #78）**靠 6h 自动化自动累积（每次 fresh 拉盘+成交+推送，跨多时段/多流动性覆盖），重点观察：a) 门控跳过数随周期增长是否稳定；b) 残余库存是否随流动性变差而放大；c) 是否出现首笔净亏（出现则降 `default_size`/提 `min_liquidity`/`mm_min_spread`）；③稳定性达标后再谈 CLOB 真实执行（涉真钱，须你明确授权）；④门控参数（6h / $500 / 衰减曲线）可在 `config/strategies.json` 微调校准。

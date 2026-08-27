# Polymarket 模拟盘运行报告（真实市场数据 · 虚拟资金）

> 生成时间：2026-08-28 ｜ 数据：Polymarket Gamma 实时行情 ｜ 资金：虚拟 $10,000（绝不触碰真实下单/钱包）
> 状态：**实盘化严谨度建模（Task #66）已接入自动化**；**时间衰减门控 + 单市场日成交上限（Task #71）已接入**；**Phase 6 自动流水线 sim_pipeline.py + 钉钉推送（Task #73）已完成**；**纯套利完备性审核流程（审核角色自动化，Task #75）已落地**（白名单机制接入门控）；**每轮运行报告自动提交 git**（流水线末尾刷新实时表 + commit）；当前为模拟层稳定期累积中（Task #78，靠 6h 自动化自动拉长周期）。

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
<!-- LIVE_DATA_START -->
截至 2026-08-28 07:44（方法论版本 v20260828_074410，自动刷新）：
| 指标 | 数值 | 说明 |
|---|---|---|
| 虚拟本金（账面现金） | **$10044.61** | 相对初始 $10,000 累积（含未实现持仓市值更高） |
| MM 累计实现盈亏 | **$184.41** | 含滑点/漂移后真实口径 |
| MM 胜率 / 净胜率 | 100% / 100% | 0 亏损笔（结构性，见下方警示） |
| 累计滑点成本 | $0.90 | 薄市场走簿产生 |
| **门控跳过 — 深度** | 0 笔 | 深度门槛（市场流动性充足） |
| **门控跳过 — 时间衰减** | 72 笔 | `min_time_to_settle_h=6h` 临近结算硬门控 |
| **门控跳过 — 单市场日上限** | 5 笔 | `daily_cap_notional=$500/24h` 滚动窗口 |
| 纯套利候选 | 145 个 | 平均 edge 0.8518，成交率 100%，**全部待审核角色判定** |
| 纯套利残余库存 | - 份 | 腿风险实证信号 |
| 累计逐笔记录 / 做市执行 | 0 / 68 | 本轮截至自动刷新时刻 |
<!-- LIVE_DATA_END -->

- **核心结论**：严谨度模型 + 双门控把"看起来稳赚"的机会做了诚实过滤——时间衰减门控随本轮加速 10 轮已跳过 **70 笔**（较本轮初 +40，证明它在随市场临近结算持续加力保护）；单市场日上限稳定命中 5 笔。这是 MVP 100% 胜率高估的**进一步修正**。
- ⚠️ **100% 胜率是结构性而非样本偏差**：本模拟盘利润由 `RigorVirtualBook` 的均值回归/价差捕获假设驱动，在"门控放行子集"（高流动性、远离结算）内**按构造近 100% 盈利**。真正的稳健性信号是**门控在主动过滤风险**（时间衰减 70 笔、日上限 5 笔）+ **残余库存已现**（15 份）。实盘结论仍需更长周期的多时段/多流动性覆盖。
- 🔴 **纯套利扫描器固有缺陷（新发现）**：`scan_poly_pure_arb` 按 `event_id` 聚合实时二元市场，而 Polymarket 的 `event_id` 多 grouping 独立二元盘（非单一多结果划分市场），故 `sum(ask)<1` 多为**假 Dutch Book**。今日 17 个候选经逐组研判**全部为假信号**（只列部分候选/重叠时间窗/跨市场拼凑/缺第三结果），已全部拒入白名单（见 `approved_pure_sets.json` 的 `rejected_event_ids`）。→ 纯套利执行路径暂未实战，仅作候选展示；后续须改扫描器才可能产生真套利。

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

**4) 当前状态 — 审核角色（代理吴总）已自动化**：你明确表示无暇人工逐组审核，故将审核权委托给**固化在 `sim_review.py` 的"审核角色（auto-proxy / 吴总代理）"**——判据（`reviewer_judge`）对候选施加"互斥+完备"负面测试（独立二元盘拼凑 / 时间窗嵌套 / 部分子集 / 跨类混搭），命中即拒；仅出现明确真划分正向证据才放行。该角色由 `sim_pipeline` 每轮自动调用 `auto_review()`：重新扫候选 → 判决 → 写回 `approved_pure_sets.json`（含 `rejected_event_ids` 逐条拒因审计）+ 刷新 `pure_arb_review_*.md` 清单。**用户在 `approved_event_ids` 手动追加的 event_id 会被保留（override 代理判定）**，下一轮即自动执行。

- 今日 17 个候选经该角色研判**全部为假 Dutch Book**（不完备/不互斥/跨市场拼凑），`approved_event_ids` 保持空，`rejected_event_ids` 存逐条拒因（见 `approved_pure_sets.json`）。由此暴露**扫描器固有缺陷**（单开任务跟踪，见第十节 ④）：按 `event_id` 聚合会混入独立二元盘，须改扫描器才可能产生真套利。未确认前纯套利始终 0 执行。

### 七-E、反馈迭代机制（sim_feedback.py + sim_pipeline.py · 如何建立 / 如何迭代）
**1) 数据来源（闭环起点）**：`sim_trader.run_once` 每成交/跳过一笔都写一条结构化 JSONL 到 `sim_logs/trades_YYYYMMDD.jsonl`（`kind` ∈ `mm`/`pure`/`pure_candidate`/`mm_skip_depth`/`mm_skip_time`/`mm_skip_cap`，含 `pnl`/`slip`/`fill_ratio`/`residual`/`msg` 等）。这是反馈机制的"原料"。

**2) 反馈分析（`sim_feedback.py`）**——`load_trades()` 读当日全量 JSONL → `analyze()` 统计：做市执行/胜率/净胜率/滑点成本/亏损笔数、三类门控跳过计数、纯套利已执行/候选数/平均 edge/平均成交率/残余库存 → `suggest()` 按规则生成自然语言建议 + `proposed_params`（与当前配置同源的独立副本，避免触发网络导入）。每次运行 `main()` 把 `{methodology_version, analysis, suggestions, proposed_params}` **追加**进 `sim_logs/feedback_YYYYMMDD.json`（同文件多版本数组 = 方法论演进轨迹）。

**3) 流水线串联（`sim_pipeline.py`，Phase 6）**：`run_pipeline(runs)` = 跑 N 轮 `sim_trader.run_once` → 写 `summary_*.json` → 调 `sim_feedback.main()` → **调 `sim_review.auto_review()`（审核角色自动审纯套利完备性）** → 读最新 feedback → 刷新 `SIM_REPORT.md` 实时表 → `git` 自动提交运行报告 → `build_markdown` 组装钉钉报告 → 按需 `notify.send_markdown` 推送。一条命令完成"跑→记→析→审→刷新→提交→推"。

**4) 迭代如何完成（闭环）**：
- **自动层（每 6h 自动化）**：`sim_pipeline.py --runs 1 --push-dingtalk` 周期触发 → 新行情/新成交追加 → 新 feedback 版本生成 → **审核角色自动刷新白名单** → **报告实时表自动刷新并提交 git** → 钉钉推送建议。门控跳过数与候选数是**实时监控指标**，异常自动出现在建议里。
- **人工层（你/AI 调参）**：读 feedback 的 `suggestions` + `proposed_params`，若认可则改 `config/strategies.json`（如调 `mm_min_spread`/`min_liquidity`/`daily_cap_notional`/时间衰减曲线）→ 下一轮自动化即用新参数，feedback 下一版本反映变化 → 形成"数据→反馈→调参→再数据"的迭代环。
- **关键设计**：`sim_feedback` 不自动改参数（只建议），避免无监督漂移；所有调参显式、可追溯（feedback 文件保留每日全版本）。纯套利 `proposed_params` 仅含 `pure_buffer` 微调用途提示，完备性由**审核角色**每轮自动白名单放行（你仍可在 `approved_event_ids` 手动 override）。

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
- **每轮自动提交运行报告**：流水线末尾自动刷新 `SIM_REPORT.md` 第五节实时表（锚点 `LIVE_DATA_START/END`），并将 `SIM_REPORT.md` + `sim_daily_caps.json` 提交到 git（best-effort push）。注：`sim_logs/`、`sim_book_poly.json` 已被 `.gitignore` 忽略（运行时态不入库），故"运行报告"以 `SIM_REPORT.md` 为准，每轮在 git 历史留下快照。

## 十、风险与下一步
- ⚠️ 当前 100% 胜率为"高流动性样本 + 模拟摩擦 + 门控放行子集"下的**结构性结果**（非样本偏差），**不可直接外推实盘**；真正的稳健性信号是门控在主动过滤（时间衰减 70 笔 / 日上限 5 笔）与残余库存（15 份）已现。
- 下一步：①**纯套利审核已自动化**（Task #75）——"审核角色（代理吴总）"每轮由 `sim_pipeline` 自动调用 `auto_review()` 判定并写白名单，你无暇时全自动履行；若你认可某 `event_id` 亦可手动写入 `approved_event_ids` override。②**稳定性拉长周期（Task #78）**靠 6h 自动化自动累积（每次 fresh 拉盘+成交+推送+提交报告，跨多时段/多流动性覆盖），重点观察：a) 门控跳过数随周期增长是否稳定；b) 残余库存是否随流动性变差而放大；c) 是否出现首笔净亏（出现则降 `default_size`/提 `min_liquidity`/`mm_min_spread`）。③**扫描器固有缺陷（单开任务跟踪）**——`scan_poly_pure_arb` 按 `event_id` 聚合混入独立二元盘，须改造才可能产出真 Dutch Book，列为独立改进项，不在本轮稳定期工作中混做。④稳定性达标后再谈 CLOB 真实执行（涉真钱，须你明确授权）；门控参数（6h / $500 / 衰减曲线）可在 `config/strategies.json` 微调校准。

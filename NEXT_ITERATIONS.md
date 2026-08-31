# Polymarket 模拟盘 — 后续迭代评估

> 状态基线：P0-1~P0-4 / P1-1~P1-3 / P2-1~P2-5 共 13 项已完成（commit `7934640`）。
> 本文评估"还差什么"，按优先级排序，供下一轮迭代直接认领。

---

## ✅ 本轮（2026-08-31）已补完

- **P0-A 关停端点鉴权** ✅：新增 `SHUTDOWN_TOKEN` 校验（`?token=` 或 Bearer 头），缺失/错误返回 403；绑定仍 `0.0.0.0`（需局域网访问），靠 token 防同网误关。
- **P0-B 流水/权益跨重启持久化** ✅：经核实 `run_meta.json`/`trades.jsonl`/`equity.jsonl`/`sim_book_poly.json` 已全量落盘并按日/按轮重建，`/api/state` 新增 `persistence` 字段可观测（重启曲线不断片、归因累计连续）。
- **P0-C 报告自动推钉钉** ✅：`auto_report_loop` 每份报告生成后自动 `send_markdown` 摘要推手机（未配机器人静默跳过）。
- **P1-A 成交率影子标定** ✅：新增 `compute_fill_calibration()` + `/api/fill_calibration`，测量当前盘口意图成交率分布+实际观测成交率，输出 `recommended_base`；`FILL_CALIBRATE_APPLY=1` 时由 `main` 应用。
- **P1-B 测试覆盖** ✅：新增 `test_attribution_identity` / `test_compliance_filter` / `test_lock_alive` + `run_tests.py`（归因恒等式/合规过滤/锁判活），`python run_tests.py` 全绿。
- **P1-C 配置外提+启动自检** ✅：新增 `preflight()` 启动打印配置清单+健康告警（钉钉/关停鉴权/成交率/模式/数据目录），非致命仅提示。
- **P1-D 盘口冗余数据源** ✅：`polymarket.fetch_poly_quotes` 主源 Gamma 失败后降级 CLOB `/markets` → 持久化 last-good 缓存（`quotes_source` 可观测），模拟不中断。

> 剩余待办见下方 P2 段（可观测升级 / 旧文档清理 / 多策略并行 / 回测加厚）。

---

## 🔴 P0（已全部完成 ✅，见上）

### P0-A 关停端点鉴权（安全）
- **现状**：`/api/shutdown` 无 token 保护，且服务绑定 `0.0.0.0:8787`。任何能访问该端口的人（同局域网 / 公网暴露时）都能关停服务。
- **风险**：生产/局域网部署下被误关或恶意关停。
- **建议**：① 默认改绑 `127.0.0.1`（仅本机），需局域网访问时显式开 `BIND=0.0.0.0`；② `/api/shutdown` 加 `SHUTDOWN_TOKEN` 校验（或仅允许本机调用）。

### P0-B 成交流水与权益序列跨重启持久化（正确性/可观测）
- **现状**：`run_meta.json` 只持久化 `last_round / last_equity`；完整成交流水、权益曲线、归因累计在内存，**重启即清零**。
- **风险**：长期运行看板历史曲线断片；归因瀑布累计净锁利无法跨重启连续。
- **建议**：把成交流水与权益序列追加落盘 `output/`（`a_share/data/` 已 gitignore），启动时回放重建；权益曲线图直接读盘。

### P0-C 报告自动推送钉钉（闭环）
- **现状**：`auto_report_loop`（P2-3）只写 `output/sim_report_*.html/.md`，**不推送**；目前钉钉只推迭代进度（`notify_progress`）。
- **建议**：周期报告生成后自动 `send_markdown` 摘要（含净锁利、拦截数、round）到手机，与迭代进度推送同一通道。

---

## 🟡 P1（已全部完成 ✅，见上「本轮已补完」）

### P1-A 成交率真实标定（FILL_BASE 不再拍脑袋）
- **现状**：`FILL_BASE=0.30` 是假设；真实值只能小额真钱挂单测出（用户已明确"接真钱第三步搁置"）。
- **过渡方案**：做"影子标定"——在 inv 模式下记录每笔挂单的"理论应成交 vs 实际是否被 Gamma 成交"的偏差，反推各流动性档位真实成交率，输出标定报告（不接真钱也能逼近）。
- **价值**：归因瀑布与净锁利的可信度直接依赖它。

### P1-B 测试覆盖（回归保护）
- **现状**：仅 P2-4（Gamma 限流）有单测；`sim_rigor` 逆向选择、`compliance` 分类、归因恒等式、单实例锁均无单测。
- **建议**：加单测守护——① 归因恒等式 `gross = realized − settled + fees + slip + asel` 闭合；② `compliance.is_blocked` 正例/反例；③ 锁文件陈旧进程判活（Windows `ctypes` 路径）；④ `pnl_attribution` 边界。CI 跑 `pytest`。

### P1-C 配置外提 + 启动自检
- **现状**：环境变量散落代码；启动无自检（端口占用/锁残留/依赖缺失靠运行时崩）。
- **建议**：抽 `config.py`，启动打印配置摘要 + 预检（端口可用/锁状态/.env 提示），失败早退并给明确指引。

### P1-D 数据源冗余（CLOB 直连）
- **现状**：盘口只依赖 Gamma API，单点；限流时降级旧缓存（可能陈旧）。
- **建议**：加 CLOB（Polymarket CLOB API）直连作冗余源，Gamma 冷却时切 CLOB；结算价双源交叉校验。

---

## 🟢 P2（增强项，按需求排期）

### P2-A 可观测性升级 ✅（2026-09-01 完成）
- 暴露 Prometheus `/metrics`（round、equity、realized、cash、unrealized、合成/真实成交率、累计成交笔数、kill switch、盘口来源 gamma/clob/cache、合规过滤、实盘模式等），接 Grafana/告警。
- **已做**：`sim_server.py` 新增 `prometheus_metrics()` + `GET /metrics` 端点（text/plain 暴露格式，零依赖）；`USAGE_MANUAL.md` §6 + `DEPLOY_NB.md` 补监控说明。重启验证 `/metrics` 正常输出。
- 合规拦截样本人工复核闭环（误杀/漏杀回流词表）：仍为可选增强，未做。

### P2-B 旧文档清理 ✅（2026-09-01 完成）
- 现有 `DEPLOY.md` / `launch_dashboard.py` 讲的是旧 A 股 `webui.py`，已过时且与本项目无关，易混淆。
- **已做**：`DEPLOY.md` 顶部加重定向横幅（指向 DEPLOY_POLYMARKET.md / DEPLOY_NB.md）；`launch_dashboard.py` 顶部 docstring 注明"旧 A 股启动器、模拟盘用 sim_server.py"；看板 `c-cash` 卡片标签改「现金(含未平仓)」+ title 说明口径（避免伙伴像用户一样困惑 cash vs equity）。均未删文件，仅加警示，零破坏。

### P2-C 多策略并行（架构）
- 现状：单实例锁限制只能跑一个 `SIM_MODE`。
- 若未来要 inv / pairs 同跑对比，需重构为多策略进程或子账本隔离（保留单实例锁语义）。

### P2-D walk-forward 回测加厚
- 现状：P0-4 用 train vs oos 横截面 Spearman IC 证明确实 edge 可复现，但样本外市场数/区间有限。
- 建议：扩更多市场、更长 OOS 窗口、加成本/容量敏感性的稳定性区间报告。

### P2-E 部署加固（一键启动器 + 系统守护）✅（2026-09-01 完成）
- **已做**：新增 `start_nb.py`（跨平台启动器：venv 自动建 + `pip install -r requirements_nb.txt` + `.env` 缺失则复制 `.env.nb`→`.env` + env 校验 SHUTDOWN_TOKEN 必设/LIVE_MODE=1 必设 PM_BOT_PK + 崩溃自动重启 MAX_RETRIES=10 退避 3s）；`polymarket-sim.service`（Linux systemd，Restart=always）；`start_nb.bat`（Windows ASCII 入口）；`DEPLOY_NB.md` §5 补启动器与 systemd 安装说明。验证 `py_compile` OK、`--check` 退出 0、`.gitignore` 含 `.venv/`。
- **价值**：伙伴在 NB 机器 `cp .env.nb .env && python start_nb.py --setup` 即一键拉起，崩溃自愈，无需手动 export 环境变量；部署链路零隐式坑。

---

## ⛔ 明确搁置 / 已决策
- **接真钱第三步**：需境外部署 + $50–100，按边界搁置，不自行推进。
- **合规过滤（2026-08-31 更新）**：中国部署默认 `COMPLIANCE_FILTER=1` 保持开启、词表修改需评审；但用户已明确 **NB 省由合作伙伴部署、无合规风险**，`COMPLIANCE_FILTER=0` 可整体关闭（见 `DEPLOY_NB.md`）。即"不可关闭"仅限中国部署语境，NB 部署不受此约束。
- **Gitee 镜像**：用户 2026-09-01 明确本项目不推 Gitee，仅留 GitHub `landlord2003/Polymarket-Sim`。

---

## 优先级速查
| 项 | 优先级 | 工作量 | 价值 |
|----|--------|--------|------|
| P0-A 关停鉴权/绑本机 | 🔴 | 小 | 安全防误关 |
| P0-B 流水跨重启持久化 | 🔴 | 中 | 历史不断片 |
| P0-C 报告自动推钉钉 | 🔴 | 小 | 闭环 |
| P1-A 成交率影子标定 | 🟡 | 中 | 归因可信度 |
| P1-B 测试覆盖 | 🟡 | 中 | 防回归 |
| P1-C 配置外提+自检 | 🟡 | 小 | 易运维 |
| P1-D CLOB 冗余源 | 🟡 | 中 | 抗限流 |
| P2-A 可观测升级 ✅ | 🟢 | 中 | 监控 |
| P2-B 旧文档清理 ✅ | 🟢 | 小 | 防混淆 |
| P2-C 多策略并行 | 🟢 | 大 | 对比（评估非必需，跳过） |
| P2-D 回测加厚 | 🟢 | 中 | 稳健性（A股 edge，Polymarket 非急需，跳过） |
| P2-E 部署加固 ✅ | 🟢 | 小 | NB 一键启动+守护 |

---

## 🏁 项目最终交付态（2026-09-01）

- **P0/P1/P2(P2-A/P2-B/P2-E)/P3 全部落地并推 GitHub**（latest `b4e15e8`）。
- P2-C（多策略并行，架构大改）、P2-D（walk-forward 回测，A股 edge）经评估非必需，已明确跳过。
- **看板「行情来源」诚实标识（P2-E 收尾）✅**：header 徽章 + banner 首句随 `quotes_source` 动态（实时 Gamma/CLOB=绿、缓存快照=黄、失败=红），彻底消除「这是不是真实行情」困惑——北京实例显示最近成功抓取的盘口快照（非实时）。
- **做市标的智能筛选（P2-follow，2026-08-31）✅**：`select_mm` 综合分=流动性×(1+2×价差) + 每类上限 `MM_N_PER_CAT=5`（env 可配）+ 逐类均匀放宽补齐；`/api/state` 暴露 `mm_cats` 类别分布；重启后空集强制重建做市集。实测跨 6 类分散、单项≤5。单测覆盖（8类/退化2类/宽价差优先/政治词过滤）。
- 代码 + 文档 + 实盘能力（WS/L2 签名/风控/kill switch/只读探针/校准/可观测 /metrics/一键启动/系统守护）全齐，**已完全可交付 NB 伙伴部署实操**。
- **唯一真卡点**：NB 伙伴首跑反馈（真实成交率回填 `FILL_BASE` 才算实盘收益定论），北京无法推进 → 等项目交付 NB 后等伙伴实测。
- **Gitee**：按用户 2026-09-01 决策，本项目不推 Gitee，仅留 GitHub `landlord2003/Polymarket-Sim`。

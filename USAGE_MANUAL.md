# Polymarket 模拟交易系统 · 使用手册

> 适用版本：Quant-Trading v3 + P0/P1/P3 迭代（截至 2026-09-01，GitHub `landlord2003/Polymarket-Sim` commit `e54066b` 起）
> 维护方：老吴（北京研发）/ NB 合作伙伴（实盘部署）
> 端口：本机 `8787`（绑定 `0.0.0.0`，仅本机/同网关访问）

---

## 0. 一句话定位

| 环境 | 用途 | 合规 | 部署 |
|---|---|---|---|
| **北京（研发）** | `DRY_RUN` 模拟盘，验证工程链路、跑只读探针 | 合规过滤**开启**（默认） | 本机直接跑 |
| **NB（New Brunswick, 加拿大）** | 合作伙伴实盘部署 | 无合规风险，`COMPLIANCE_FILTER=0` | 境外机器，见 `DEPLOY_NB.md` |
| **接真钱第三步** | 仅 NB 实盘路径 | — | 需境外部署 + 小资金，按手册 §8 走 |

> ⚠️ 北京机器**只允许 DRY_RUN 与只读探针**，不得真发单（无 `PM_BOT_PK`、无 `LIVE_MODE`）。实盘只在 NB。

---

## 1. 系统架构

```
行情层   polymarket.py         Gamma REST + CLOB /markets 冗余源（降级链 gamma→clob→cache→error）
         ws_polymarket.py      CLOB WebSocket 实时盘口（实盘做市毫秒级必需，惰性导入）
策略层   sim_server.py         HTTP 服务 + 主循环 step()（信号 select_mm → 下单 → 结算 → 归因）
         sim_rigor.py          RigorVirtualBook：带真实摩擦的模拟账本（滑点/对冲漂移/逆向选择/时间衰减）
执行层   clob_exec.py          py-clob-client L2 凭证 + 钱包签名下单/撤单（默认 DRY_RUN 不真发单）
风控层   risk_control.py       单市场/总仓位上限 + 日亏损限额 + kill switch
合规层   compliance.py         政治/地缘/军事红线过滤（COMPLIANCE_FILTER=0 整体关闭）
报告层   sim_report.py         周期报告 HTML/MD
推送层   notify.py / notify_progress.py   钉钉 markdown 推送
情报层   probe_polymarket.py   只读拉真实概率 → 喂低空/宏观情报工作流
```

**数据流**：行情 → 信号(`select_mm`) → 下单(`book.market_make` / `live_dispatch`) → 结算 → PnL 归因 → 持久化(`run_meta` 等) → 看板 / 钉钉。

---

## 2. 快速开始（北京模拟盘）

```bash
# 仓库根目录
cd E:/Workbuddy/Quant-Trading
# 运行（默认 SIM_MODE=inv，DRY_RUN，合规开）
C:/Users/Lenovo/.workbuddy/binaries/python/versions/3.13.12/python.exe a_share/sim_server.py
```
- 浏览器打开 `http://127.0.0.1:8787`
- Windows 一键：`start_sim_dashboard.bat`（清理端口 + 起服务 + 开浏览器）
- 优雅停止：`http://127.0.0.1:8787/api/shutdown?token=sim-stop-8787`（token 见 §3）

---

## 3. 配置（环境变量）

复制 `.env.example` → `.env`（北京）或 `.env.nb` → `.env`（NB）。关键变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `SIM_MODE` | `inv` | 策略模式（`inv`=倒手做市 / `mm`=纯做市 / `arb`=套利） |
| `FILL_BASE` | `0.30` | 成交率地板（P1-A 标定显示意图成交率中位≈0.94，但保留保守地板） |
| `PRICE_REFRESH_SEC` | `90` | 真实盘口刷新周期 |
| `AUTO_REPORT_MIN` | `30` | 周期报告间隔（分钟），生成后自动推钉钉 |
| `SHUTDOWN_TOKEN` | `sim-stop-8787` | `/api/shutdown` 与 `/api/kill_switch` 鉴权 token |
| `COMPLIANCE_FILTER` | `1` | **1=开（北京） / 0=关（NB 无合规风险）** |
| `FILL_CALIBRATE_APPLY` | `0` | `1`=把标定 `recommended_base` 应用到 `FILL_BASE`（默认仅测量） |
| `LIVE_MODE` | `0` | **0=模拟不真发单 / 1=真发单（仅 NB，且需 `PM_BOT_PK`）** |
| `PM_BOT_PK` | 空 | 钱包私钥，**仅环境变量，绝不入 git/日志**（NB 实盘必填） |
| `DINGTALK_WEBHOOK` / `DINGTALK_SECRET` | 空 | 钉钉机器人（加签 HMAC-SHA256），空则静默跳过 |
| `MAX_POS_PER_MARKET` | `0` | 单市场最大仓位（USD，0=不限制；实盘建议设） |
| `MAX_TOTAL_POS` | `0` | 总仓位上限（USD，0=不限制） |
| `DAILY_LOSS_LIMIT` | `0` | 日亏损限额（USD，0=不限制；触发后禁止新单） |

> NB 实盘模板见 `.env.nb`，逐行注释 + 安全提醒。

---

## 4. 看板指标定义（重要，避免误读）

以 2026-09-01 实时快照为例（`round`=102,335）：

| 指标 | 值 | 含义 |
|---|---|---|
| 轮次 round | 102,335 | 主循环累计轮次 |
| **累计锁利 realized** | **258,993.10** | 已平仓回合净锁利（减建模费率 + 建模逆向选择损耗）。**模拟"真赚到"的利润** |
| 现金 cash | 284,472.16 | `book.cash`=实际发生现金流（含未平仓库存名义）。**不是账户总值** |
| **权益 equity** | **285,916.05** | `initial + realized + unrealized`=盯市总账户值（真权益） |
| 未实现 unrealized | 16,922.95 | 未平仓库存的盯市浮动盈亏 |
| 初始本金 initial | 10,000.00 | 起始权益 |

**三个关键恒等式（已代码校验，无重复计量）：**
1. `initial + realized + unrealized = equity`　→ 10000 + 258993.10 + 16922.95 = 285916.05 ✅
2. 分类锁利之和 ≈ 累计锁利　→ tag_pnl 各项 pnl 之和 259,244 ≈ checkpoint 259,230 ✅
3. 按日锁利之和 = 累计锁利（跨重启不丢）✅

**⚠️ 切勿这样读：`cash + unrealized ≠ equity`。**
`cash`（284,472）已内含未平仓库存名义 + 建模逆向选择损耗，若再加 `unrealized` 会**重复计算未平仓**。两者差值 ≈ 15,479 = 建模逆向选择损耗 + 净空头未平仓名义。看板请认 **equity** 作账户总值，**realized** 作已锁利。

---

## 5. 模拟 vs 实盘：方法论诚实说明（必读）

**当前 realized≈258,993（≈25.9 倍）是"工程链路正确"的证明，不是"策略有超额收益"的证明。** 它建立在以下模拟假设上：

| 维度 | 模拟盘假设 | 真实盘现实 | 影响 |
|---|---|---|---|
| 成交率 | `fill_prob`≈94%（标定中位） | 挂单等对手方主动来吃，真实成交率低得多 | 🔴 决定性高估 |
| 逆向选择 | 仅按 `adverse_selection_frac` 建模损耗 | 真实被知情单 pick-off，损耗更大 | 🔴 |
| 延迟 | 零延迟即时按报价成交 | WS+下单+链上确认有延迟，丢单/吃差 | 🔴 |
| 对手竞争 | 无对手盘，静态价差白捡 | 职业做市商抢单 | 🔴 |
| 费率 | 写死 0.5% | 做市挂单(maker) **0 费**，吃单方峰值 1.8% | 🟡 非主因 |

**所以：258K 不能当作实盘预期。** 唯一能验证实盘收益的办法，是在 NB 用 `LIVE_MODE=0` 先跑 ≥1 周，量出**真实成交率**，回填 `FILL_BASE`，确认真实净价差 > 费率再放大资金（见 §8）。这一步已内建（`DEPLOY_NB.md` + `FILL_CALIBRATE_APPLY`）。

**方法论是否需要迭代？** 代码逻辑已自洽（无重复计量、恒等式成立），无需重写；**真正要迭代的是"真实世界标定"**——fill 率 / 逆向选择 / 延迟只能靠 NB 实盘数据回填，不是北京能完成的。

---

## 6. HTTP API 端点

| 端点 | 鉴权 | 说明 |
|---|---|---|
| `GET /api/state` | 无 | 实时快照（round/cash/realized/equity/unrealized/fill/persistence） |
| `GET /api/stats` | 无 | 统计中心（胜率/回撤/分类锁利/按日锁利/归因） |
| `GET /api/compliance` | 无 | 合规过滤可观测（命中词/词表，`COMPLIANCE_FILTER=0` 时为空） |
| `GET /api/fill_calibration` | 无 | 成交率影子标定（intended 分布 + recommended_base） |
| `GET /api/risk` | 无 | 风控状态（仓位/日亏/kill switch） |
| `POST /api/kill_switch?token=` | SHUTDOWN_TOKEN | 触发/复位 kill switch（触发即钉钉告警） |
| `POST /api/shutdown?token=` | SHUTDOWN_TOKEN | 优雅停止并释放启动锁 |
| `GET /` | 无 | 看板 HTML |

---

## 7. 钉钉推送

```bash
# 单条进度通知
python a_share/notify_progress.py "任务号" "标题" "行1 @@ 行2 @@ 行3"
# 底层 markdown
python a_share/notify.py   # send_markdown(title, text)
```
- 需在 `.env` 配 `DINGTALK_WEBHOOK` + `DINGTALK_SECRET`（加签）。
- 周期报告（`AUTO_REPORT_MIN`）、kill switch 触发均自动推。
- 未配置则静默跳过，不影响主流程。

---

## 8. NB 省实盘部署（伙伴 SOP 摘要）

完整 SOP 见 **`DEPLOY_NB.md`**。核心步骤：

1. `git clone` + `python -m venv` + `pip install -r requirements_nb.txt`（含 `websockets` / `py-clob-client` / `web3`）
2. `cp .env.nb .env`，填 `PM_BOT_PK` / `SHUTDOWN_TOKEN` / 钉钉；`COMPLIANCE_FILTER=0`、`LIVE_MODE` 先设 `0`
3. **IP 自检**：`curl ipinfo.io` 确认落 CA/New Brunswick，**勿挂 VPN**（否则触发地域合规）
4. **先 `LIVE_MODE=0` 跑 ≥1 周**：验证真实成交率、WS 盘口、L2 凭证派生、风控/kill switch
5. 小资金（热钱包 $200–500）改 `LIVE_MODE=1` 实盘；确认真实净价差 > 费率再放大
6. 任何时候 `POST /api/kill_switch?token=...` 远程熔断（钉钉告警）

---

## 9. 风控与 kill switch

- `risk_control.py` 在每笔实盘下单前强制 `check_new_order()`（单市场/总仓位/日亏）。
- `GET /api/risk` 看当前状态；`POST /api/kill_switch?token=...` 置位后停止一切新单并钉钉告警；状态落盘 `data/risk_state.json` 跨重启。
- 北京模拟盘也可手动触发测试（不影响模拟逻辑，仅演示告警链路）。

---

## 10. 持久化与重启

| 文件 | 内容 | 重启 |
|---|---|---|
| `a_share/data/run_meta.json` | 累计锁利/轮次/峰值权益/分类/按日 | 重建 |
| `a_share/data/trades.jsonl` | 成交流水（最近样本） | 增量 |
| `a_share/data/equity.jsonl` | 权益曲线 | 重建 |
| `a_share/data/sim_book_poly.json` | 模拟账本（库存/均价/归因） | 重建 |
| `output/sim_server.pid` | 单实例锁 | 释放 |

- 重启不丢累计（检查点优先）。
- **`SIM_RESET=1` 清空重来**（谨慎，会丢历史）。

---

## 11. 只读探针（概率 → 情报）

```bash
python a_share/probe_polymarket.py --limit 60 --top 12          # 单次
python a_share/probe_polymarket.py --limit 60 --loop 3600        # 每小时刷新
```
- Gamma 公开 API 只读，北京零合规风险、立即可用。
- 聚焦经济/利率/加密/产业类，去地缘敏感噪声，输出 `a_share/data/probe_feed.json` 喂情报工作流。

---

## 12. 测试

```bash
python a_share/run_tests.py     # unittest discover，覆盖 PnL 归因恒等式/合规过滤/PID 锁
```
- 当前 7 passed。改动后请先跑（语法 + 逻辑回归）。

---

## 13. 故障排查 / FAQ

| 现象 | 原因 | 处理 |
|---|---|---|
| `/api/shutdown` 返回 403 | 缺/错 token | 加 `?token=SHUTDOWN_TOKEN` 或 `Authorization: Bearer` |
| 盘口来源 `error` | Gamma/CLOB 均不可达 | 检查网络；`quotes_source()` 可观测；本地有缓存兜底 |
| WS 未生效 | 未装 `websockets` | NB：`pip install websockets` |
| `clob_exec` 报 RuntimeError | `LIVE_MODE=1` 但无私钥 | 填 `PM_BOT_PK`；否则保持 `LIVE_MODE=0` |
| `cash` 比 `equity` 大很多 | 正常：`cash` 含未平仓名义，基数不同 | 认 `equity` 为账户总值，见 §4 |
| 累计锁利突然变大 | 模拟盘持续运行（隔夜也跑） | 正常，非 bug；看 `round` 判断是否新快照 |

---

## 14. 已知边界与路线图

- **已自洽**：PnL 归因、分类/按日/累计锁利恒等式、持久化跨重启、合规可关、WS/CLOB/L2/风控/kill switch 全就位。
- **待 NB 实测校准**：真实成交率 → 回填 `FILL_BASE`；真实逆向选择/延迟损耗 → 调 `adverse_selection_frac` / `adverse_frac`。
- **P2 增强（可选）**：多策略并行、回测加厚、旧文档重定向、可观测升级。
- **接真钱第三步**：搁置（需境外部署 + $50–100），按 §8 在 NB 走。

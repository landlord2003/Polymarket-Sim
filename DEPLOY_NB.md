# NB 省实盘部署 SOP（Polymarket-Sim）

> 面向**加拿大 New Brunswick 等开放省**的部署伙伴。物理 IP 落 CA 开放省、无 KYC、可实盘。
> 本文件只讲「怎么把项目拉下来、配好、先模拟跑通、再小资金实盘」。
> 模拟盘主线文档见 `DEPLOY_POLYMARKET.md`；本文件是其实盘增强版。

---

## 0. 上线前必做：IP 自检

Polymarket 按**当前物理出口 IP**判定地区（不看国籍/户籍）。上线前确认 IP 解析到 **CA / New Brunswick（或任一开放省）**：

```bash
curl -s ipinfo.io | python -m json.tool
# 关注 "country": "CA" 与 "region": "New Brunswick"
# 若 region 落在 ON/AB/BC/QC 或 country 非 CA，会被 close-only，先排查网络再继续
```

> ⚠️ **不要挂 VPN**（尤其勿挂到 ON/AB/BC/QC 或受限国，否则直接 close-only / 封锁）。本机直连即可。

---

## 1. 前置条件

| 条件 | 要求 |
|------|------|
| 出口 IP | CA 开放省（见 §0 自检） |
| Python | 3.11+（3.13 已验证） |
| Git | 任意较新版本 |
| 依赖 | `pip install -r requirements_nb.txt`（websockets / py-clob-client / web3） |
| 钱包 | 非托管钱包（MetaMask 等），往 Polymarket 充值地址打 **Polygon 上的 USDC**（最低 $3） |

---

## 2. 克隆 + 环境

```bash
git clone https://github.com/landlord2003/Polymarket-Sim.git
cd Polymarket-Sim
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements_nb.txt
cp .env.nb .env                                        # 编辑 .env 填真实值
```

---

## 3. 配置 `.env`（关键三项）

| 变量 | 填什么 | 说明 |
|------|--------|------|
| `SHUTDOWN_TOKEN` | 随机长串 | 关停 / kill switch 鉴权，务必改掉弱默认 |
| `PM_BOT_PK` | 你的钱包私钥 | **仅 env，绝不写代码 / 提交**；`LIVE_MODE=1` 时必须 |
| `COMPLIANCE_FILTER` | `0` | NB 无合规风险，关闭过滤，交易所有市场 |
| `LIVE_MODE` | 先 `0` | 先模拟跑通，再改 `1` 小资金实盘 |
| `LIVE_POLL_SEC` | `30` | `LIVE_MODE=1` 时后台每 N 秒轮询在途订单真实成交状态（用于看板真实成交率） |
| `DINGTALK_WEBHOOK` / `DINGTALK_SECRET` | （可选） | kill switch 触发 / 报告推送；建议配置以便手机告警 |

> **真实成交率看板**：`LIVE_MODE=1` 后，看板新增「真实成交率(LIVE)」卡片（显示 `命中/尝试` 与百分比），
> 数据来自后台对 `clob_exec.get_order_status` 的异步轮询；北京 `DRY_RUN` 下该卡片显示 `DRY_RUN`，不影响模拟盘。

> 完整变量见 `.env.nb` 注释；其余见 `DEPLOY_POLYMARKET.md` §6。

---

## 4. 启动（先模拟，再实盘）

### 阶段一：模拟观察（≥1 周，零风险）

```bash
# sim_server.py 启动时会自动加载仓库根 .env（stdlib 解析，无需 python-dotenv、不覆盖已设环境变量）
# 所以 cp .env.nb .env 并填好值后，直接运行即可，配置自动生效：
python a_share/sim_server.py
# 如需临时覆盖（不影响 .env 文件）：LIVE_MODE=0 python a_share/sim_server.py
```

- 浏览器开 `http://127.0.0.1:8787`，看 🛡️ 合规面板应为「已关闭」、`/api/state` 的 `compliance` 关。
- 看 `/api/risk`：确认仓位 / 日亏 / kill switch 状态正常。
- 重点观察：实际成交节奏、盘口来源 `quotes_source`（应为 `gamma`；失败降级 `clob`/`cache`）。

### 阶段一·五：真实成交率校准（回填 FILL_BASE，必经）

> 模拟盘 `fill_prob` 是假设值，不能直接当实盘成交率。实盘前必须用真实（极小）订单校准一次。
> 详细步骤见 **`CALIBRATE_FILL_BASE.md`**。

```bash
# 1) 预览将挂的价/量（不发单）
python a_share/calibrate_fill.py --preview

# 2) 真校准：30 个市场、单笔 $3、观察 10 分钟、跑 2 轮（须 LIVE_MODE=1 + PM_BOT_PK）
python a_share/calibrate_fill.py --live --markets 30 --size 3 --window 600 --rounds 2
#   → 输出 observed_fill_rate_pct + recommended_base，写入 a_share/data/fill_calibration_live.json
```

- 把 `recommended_base` 回填进 `.env` 的 `FILL_BASE`，重启生效。**若 observed_rate < 0.30，先调 `adverse_frac` / 换高流动市场，不要放大。**
- 校准闭环是"模拟 258K 能否在实盘兑现"的唯一实测手段（净价差闸门见 `CALIBRATE_FILL_BASE.md` §5）。

### 阶段二：小资金实盘（热钱包 $200–500）

1. 钱包只留小额热钱；主资金冷存。
2. 改 `.env`：`LIVE_MODE=1`，确认 `PM_BOT_PK` 已填。
3. 重启：`python a_share/sim_server.py`。
4. 前 24–48h 盯 `/api/risk` 与钉钉告警；确认实盘挂单出现在 Polymarket 账户。

> 🔴 实盘前务必确认：IP 自检通过（§0）、`COMPLIANCE_FILTER=0`、`SHUTDOWN_TOKEN` 已改、kill switch 钉钉可用。

---

## 5. 熔断与运维

- **kill switch（立即停止一切新单）**：
  ```bash
  curl "http://127.0.0.1:8787/api/kill_switch?token=<SHUTDOWN_TOKEN>"
  # 解除：curl "http://127.0.0.1:8787/api/kill_switch?token=<SHUTDOWN_TOKEN>&action=off"
  ```
  触发即钉钉告警，状态落盘 `a_share/data/risk_state.json`（重启仍生效）。
- **优雅停止**：`/api/shutdown?token=<SHUTDOWN_TOKEN>`（P0-A 鉴权）。
- **可观测性（P2-A）**：`GET /metrics` 暴露 Prometheus 文本格式指标（round/equity/realized/cash/合成与真实成交率/累计成交笔数/kill switch/盘口来源/合规过滤/实盘模式）。可自建 `prometheus.yml`  scrape `http://127.0.0.1:8787/metrics` + Grafana 看板/告警，零额外依赖（服务本身不装 Prometheus）。
- **金融风控**：单市场 / 总仓位 / 日亏超限自动拒单（`risk_control.py`，参数 `MAX_POS_PER_MARKET` / `MAX_TOTAL_POS` / `DAILY_LOSS_LIMIT`）。

---

## 6. 关键提醒

- ⚠️ **不要挂 VPN**；本机直连，出口 IP 即 NB。
- 🔴 **私钥只在 `.env`**，`.env` 已被 gitignore，永不提交。
- 🟡 24/7 在线需机器常开 / 加开放省 VPS（IP 必须落开放省，勿误落四省 close-only）。
- 🟡 税务：若构成加拿大税务居民，加密交易收益应税（CRA，报 T1）；保留全量交易记录。
- 🟡 平台属人义务不豁免：你仍需自行承担母国资本管制 / 税务等义务。

---

## 7. 模块对应（本次实盘化迭代）

| 模块 | 作用 |
|------|------|
| `sim_server.py` | 主服务；`COMPLIANCE_FILTER` 控制过滤开关；`/api/risk` `/api/kill_switch` 端点 |
| `risk_control.py` | 金融风控层（仓位 / 日亏 / kill switch） |
| `clob_exec.py` | CLOB 实盘下单（L2 凭证 + 钱包签名）；`LIVE_MODE=1` 真发单，下单前过风控 |
| `ws_polymarket.py` | CLOB WebSocket 实时盘口（实盘低延迟；`pip install websockets`） |
| `probe_polymarket.py` | 只读探针（概率 → 情报，北京也可用） |
| `calibrate_fill.py` | **真实成交率校准**（NB 真挂单+轮询，输出回填 FILL_BASE 的 recommended_base） |
| `compliance.py` | 合规过滤（NB 下由 `COMPLIANCE_FILTER=0` 关闭） |

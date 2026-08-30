# Polymarket 模拟交易看板 — 跨机部署说明

> 本文档只讲一件事：**在另一台电脑上把本项目的 Polymarket 模拟盘看板跑起来**。
> 仓库里还有 A 股 / 加密等其它模块，那些不在本指南范围；本指南只覆盖 `a_share/sim_server.py` 这条模拟盘主线。

---

## 0. 这是什么

- **Polymarket 模拟做市 / 锁利看板**，全程 **DRY_RUN（零真实资金）**，只是验证策略与工程链路。
- 入口：`a_share/sim_server.py`，本地起一个 HTTP 服务，浏览器打开 **http://127.0.0.1:8787** 即可看实时看板。
- **合规红线**：自动过滤政治 / 地缘 / 军事等敏感市场（`compliance.py` 为单一事实来源），中国部署**不可关闭**。
- 看板内置：实时行情榜、统计中心（含 🛡️ 合规过滤面板）、盈亏归因瀑布、敏感性分析、报告导出。

---

## 1. 前置条件

| 条件 | 要求 | 说明 |
|------|------|------|
| Python | **3.11+**（3.13 已验证可跑） | 模拟盘路径**只用标准库，无需 pip install** |
| Git | 任意较新版本 | 用于 clone 仓库 |
| 网络 | 能访问 Polymarket Gamma 盘口 API | 取真实盘口；取不到会降级用旧缓存 / 合成数据，看板仍可跑 |
| 钉钉机器人 | （可选）Webhook + 加签 Secret | 用于迭代进度 / 报告推送；不配也能本地跑，只是不推送 |
| 加密 / A 股模块 | （可选）`pip install -r requirements.txt` | **仅当你还想跑仓库内 crypto / a_share 信号模块时才需要**（pandas/numpy/akshare/ccxt） |

---

## 2. 克隆仓库

```bash
# GitHub（主）
git clone https://github.com/landlord2003/Polymarket-Sim.git
cd Polymarket-Sim

# 或 Gitee 镜像
git clone https://gitee.com/landlord2003/polymarket-sim.git
cd polymarket-sim
```

---

## 3. （可选）Python 虚拟环境

```bash
python -m venv .venv
.venv\Scripts\activate      # Windows
source .venv/bin/activate   # macOS / Linux
```

> 跑模拟盘**不用装任何依赖**。只有当你要顺带运行仓库里的 A 股 / 加密模块时才需要：
> `pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`（国内镜像加速）

---

## 4. 配置 `.env`（推送用，可选）

```bash
cp .env.example .env
# 编辑 .env，填入你的钉钉 Webhook 与 Secret
```

- `DINGTALK_WEBHOOK`：完整 URL（含 `access_token=...`）
- `DINGTALK_SECRET`：加签密钥（以 `SEC` 开头）
- 不填也能本地跑；`.env` 已被 `.gitignore` 排除，**永不入库**，放心填。

---

## 5. 启动

### 方式 A（推荐，Windows 一键）
**双击 `start_sim_dashboard.bat`** → 自动清理 8787 端口残留、用 `.venv` 启动 `sim_server`、5 秒后开浏览器。
关闭该窗口即停止服务。

### 方式 B（命令行，跨平台）
```bash
# 默认 inv 真实做市模式
python a_share/sim_server.py

# 或显式带环境变量
SIM_MODE=inv FILL_BASE=0.30 PRICE_REFRESH_SEC=150 AUTO_REPORT_MIN=30 python a_share/sim_server.py
```
启动后浏览器打开 **http://127.0.0.1:8787**

> 服务绑定 `0.0.0.0:8787`，同局域网内其它设备可用本机 IP 访问（如 `http://192.168.x.x:8787`）。

---

## 6. 环境变量速查

| 变量 | 默认 | 说明 |
|------|------|------|
| `SIM_MODE` | `pairs` | `inv`=真实做市（挂单按概率成交、未平敞口跨轮持有并承担波动，推荐）；`pairs`=乐观对照（同轮双边建平，收益会显著虚高，仅对照） |
| `FILL_BASE` | `0.30` | 挂在市场最优价时的基础成交率（0~1）；挂得越贪越难被打到 |
| `FILL_GAMMA` | `1.0` | Gamma 盘口流动性加权系数 |
| `APPLY_FILL` | `1` | `0`=关闭概率成交，退回 100% 成交（仅调试用） |
| `LIQ_REF` | `30000.0` | 流动性参考值，达到时基础成交率 → ~0.92 |
| `PRICE_REFRESH_SEC` | `150` | 真实盘口刷新间隔（秒）；一次全量拉取约 20s，**切勿设太小以免被 Gamma 限流** |
| `AUTO_REPORT_MIN` | `30` | 自动报告间隔（分钟）；`0`=关闭自动报告 |
| `SIM_RESET` | （不设） | 设为 `1` 启动时清空模拟账本重来 |

---

## 7. 看板与 HTTP 端点

| 地址 | 说明 |
|------|------|
| `http://127.0.0.1:8787/` | 主看板：行情榜 / 统计中心（含 🛡️ 合规过滤面板，每 2s 刷新）/ 盈亏归因瀑布 / 敏感性分析 |
| `/api/state` | 整体运行状态 JSON（round / 权益 / 成交率 / Gamma 限流冷却剩余等） |
| `/api/compliance` | 合规过滤可观测：扫描总数 / 已拦截 / 放行 / 拦截率 + 拦截样本（命中词 + 类别）+ 完整词表（P2-5） |
| `/api/attribution` | 盈亏归因瀑布：毛价差 / 滑点 / 手续费 / 逆向选择 / 结算 → 净锁利（P1-3） |
| `/api/export_report` | 导出 HTML + MD 报告（复用内存中在跑引擎数据） |
| `/api/shutdown` | 优雅停止：停服并同步释放启动锁（P2-2） |

---

## 8. 单实例锁 & 停止

- 启动时写 `output/sim_server.pid` 锁；**第二个实例会被拒绝**（提示用 `taskkill` 或删锁后重起）。
- 正常停止三选一：
  1. 浏览器访问 `/api/shutdown`
  2. 关掉启动窗口（`start_sim_dashboard.bat` 那个）
  3. `taskkill /PID <pid> /F`（Windows）/ `kill <pid>`（macOS/Linux）
- 锁文件会**自动释放**；若异常退出导致锁残留，删 `output/sim_server.pid` 即可重起。

---

## 9. 报告与数据落盘

- 报告 / 账本 / 日志均在 `output/`（已 `.gitignore`，**不入库**）。
- 自动报告每 `AUTO_REPORT_MIN` 分钟生成 `output/sim_report_*.html` / `.md`；启动即先出一份。
- 手动导出：看板「📤 导出报告」按钮，或调 `/api/export_report`。

---

## 10. 合规红线（重要）

- `compliance.py` 是过滤**单一事实来源**：扫描实时盘口、拦截政治 / 地缘 / 军事类市场（本机实测约 7% 拦截率）。
- 中国部署**不要关闭**此过滤；修改词表（`BLOCK_EXTRA` / `BLOCK_SPORTS`）需评审。
- 看板 🛡️ 合规过滤面板实时显示命中词与拦截数，红线从黑盒变可观测。

---

## 11. 常见问题

| 现象 | 处理 |
|------|------|
| 端口被占 | 先 `/api/shutdown` 或 `taskkill` 释放；或删 `output/sim_server.pid` 后重起 |
| 报「已有实例在运行」 | 同上，先停旧实例再起 |
| 盘口长时间不更新 | 查网络能否访问 Gamma；限流时会进 30s 冷却并降级旧缓存（看 `/api/state` 的 `gamma_cooldown`） |
| 启动即退出 | 看终端报错，多半端口占用或旧锁残留 |
| 不推送钉钉 | 检查 `.env` 的 `DINGTALK_WEBHOOK` / `DINGTALK_SECRET` 是否正确 |
| `ModuleNotFoundError` | 确认在仓库根目录运行，且 `a_share/` 下模块齐全（sim_server / polymarket / sim_rigor / sim_report / compliance / notify） |

---

## 12. 目录速览（模拟盘相关）

```
Polymarket-Sim/
├── a_share/
│   ├── sim_server.py     # 模拟盘 HTTP 服务入口（本项目核心）
│   ├── polymarket.py     # Gamma 盘口取数 + 429 限流冷却 / 指数退避（P2-4）
│   ├── sim_rigor.py      # 虚拟账本 + 逆向选择建模 + 敏感性分析（P0 / P1-2）
│   ├── sim_report.py     # HTML / MD 报告（含盈亏归因瀑布，P1-3）
│   ├── compliance.py     # 合规过滤（单一事实来源，P2-5）
│   └── notify.py         # 钉钉 / 企微推送
├── start_sim_dashboard.bat  # Windows 一键启动（清理端口 + 起服务 + 开浏览器）
├── requirements.txt          # 仅 A股 / 加密模块需要；模拟盘无需
├── .env.example
└── DEPLOY_POLYMARKET.md
```

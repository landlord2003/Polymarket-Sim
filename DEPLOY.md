# 在另一台电脑部署量化交易系统

本文档写给需要在第二台机器（Windows / macOS / Linux 均可）跑这套系统的你。
仓库只含代码与配置，**不含任何密钥**。钉钉/微信推送的凭证通过 `.env` 本地提供。

---

## 一、安装条件（必须满足）

| 条件 | 要求 | 说明 |
|------|------|------|
| 操作系统 | Windows 10/11、macOS 12+、Ubuntu 20.04+ | 全平台可用 |
| Python | **3.11 或 3.12（强烈推荐）**；3.13 已验证可跑但不保证所有依赖有轮子 | 不要用 3.8 以下 |
| pip | 随 Python 自带 | — |
| Git | 任意较新版本 | 用于 clone 仓库 |
| 网络 | 能访问 AkShare 数据源（东方财富等） | **国内网络最佳**；海外/受限网络需代理，否则取数失败会自动降级为「离线」，不出真实信号 |
| 钉钉/企微 | （可选）已建好的自定义机器人 Webhook + 加签密钥 | 用于接收信号推送；不配也能本地跑，只是不推送 |
| 加密（可选） | Binance Testnet 密钥（dry-run 不需） | 仅跑 `crypto/bot_dryrun.py` 需要；默认 dry-run 零资金，无需密钥 |

> ⚠️ A股**不自动下单**：本系统只产出「买/持/卖」信号推给你，**手动决策、手动下单**。这是合规 + 风控的硬约束。
> ⚠️ 加密线默认 `dry_run=true`（永不真实下单）；`testnet=true` 走 Binance 测试网，**仅用于技术验证，勿接真实资金**。

### 功能矩阵（部署后能直接做什么）

| 功能 | 命令 | 说明 |
|------|------|------|
| A股四维度信号 | `python a_share/run_daily.py` | 自选股打分 + 钉钉推送 |
| 个性化规则 | 编辑 `watchlist.json` 的 `rules` | 钢研布林下轨买点 / 元力建仓区间等已内置 |
| 五板块自动选股 | `python a_share/run_daily.py --screener` | 新能源/新材料/AI/机器人/军工 扫描 TopN |
| 加密模拟盘 | `python crypto/bot_dryrun.py --once` | CCXT 全自动闭环（dry-run 零资金） |
| 可视面板 | `python a_share/webui.py` | 浏览器开 http://127.0.0.1:8787，页面内按钮启动扫描 |
| Freqtrade 框架 | 见 `crypto/freqtrade/README.md` | 需另装 freqtrade + testnet 密钥 |

---

## 二、分步部署

### 第 1 步：克隆仓库
```bash
git clone https://gitee.com/landlord2003/quant-trading.git
cd quant-trading
```

### 第 2 步：创建隔离 Python 环境（强烈建议，避免污染系统）
```bash
# Windows
python -m venv .venv
.venv\Scripts\activate

# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
```

### 第 3 步：安装依赖
```bash
pip install -r requirements.txt
```
> 国内网络慢可加镜像：`pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`

### 第 4 步：配置推送凭证（.env）
```bash
cp .env.example .env
# 编辑 .env，填入你的钉钉 Webhook 与 SECRET
# 这两个值来你现有 D:\WorkBuddy\output\dingtalk_notify.py 里那两行
```
- `DINGTALK_WEBHOOK`：完整 URL（含 `access_token=...`）
- `DINGTALK_SECRET`：加签密钥（以 `SEC` 开头）
- `WECOM_WEBHOOK`：（可选）企业微信机器人

> 🔒 `.env` 已被 `.gitignore` 排除，**永远不会上传**，放心填。

### 第 5 步：离线自检（不联网、不推送，验证代码接线）
```bash
python a_share/run_daily.py --offline
python a_share/run_daily.py --screener --offline   # 五板块选股离线自检
python crypto/bot_dryrun.py --once                 # 加密模拟盘一轮（dry-run）
```
应看到一份 Markdown 格式信号日报，每只标的标注「离线」，且**不推送**；选股与加密也应有正常输出。

### 第 6 步：联网实跑（出真实信号 + 推送）
```bash
python a_share/run_daily.py --screener
```
联网正常时，会对 `a_share/watchlist.json` 里的每只股票跑四维度打分、对五板块跑选股初筛，汇总后推送钉钉（配了 `.env` 的话）。

### 第 6.5 步（推荐）：可视面板（webui，免敲命令行）
```bash
python a_share/webui.py
```
保持终端窗口开着（Ctrl+C 退出），浏览器打开 **http://127.0.0.1:8787**：点 **📊 日常盯盘** / **🔎 板块选股** / **🚀 全部运行**，勾 **离线验证** 先用合成数据跑顺、不联网不推送；勾 **推送钉钉** 且配好 `.env` 时推手机。扫描在后台线程跑（联网取数约 30–90 秒），页面每 2 秒自动轮询刷新。端口用 `QT_WEB_PORT` 环境变量改（默认 8787）。

### 第 7 步（可选）：加密模拟盘实跑
```bash
# 默认 dry_run=true 零资金，验证整条链路（用合成/实时数据）
python crypto/bot_dryrun.py --once --live        # 连 Binance/Testnet 实跑一轮
python crypto/bot_dryrun.py --loop --interval 60 # 每 60 秒一轮（Ctrl+C 退出）
```
若要接 testnet 真测试单：在 `.env` 填 `CRYPTO_API_KEY` / `CRYPTO_API_SECRET`，并把 `CRYPTO_DRYRUN` 改为 `false`（仍用测试资金，零真实风险）。

---

## 三、日常使用 & 定时

### 修改自选股
编辑 `a_share/watchlist.json`（JSON 格式，加代码/名称/标签即可）。低空板块标的已预置，按需增删。

### 定时每天自动跑（可选）
- **Windows**：任务计划程序 → 触发器「每日开盘后」→ 操作 `cmd /c ".venv\Scripts\python.exe a_share\run_daily.py"`
- **macOS/Linux**：`crontab -e` 加 `30 9 * * 1-5 cd /path/quant-trading && .venv/bin/python a_share/run_daily.py`

---

## 四、常见问题

| 现象 | 原因 | 处理 |
|------|------|------|
| 全部标的显示「离线」 | 网络不通 AkShare 数据源（海外/代理/防火墙） | 换国内网络或配置代理；离线模式仅验证不推送 |
| 推送提示「未配置 DINGTALK...」 | `.env` 没填或没激活环境 | 确认 `.env` 存在且值正确，`echo $DINGTALK_WEBHOOK` 能看到 |
| `ModuleNotFoundError: akshare` | 依赖没装进当前环境 | 确认已 `activate` 且 `pip install -r requirements.txt` |
| 信号全为「观望」 | 当前行情四维度综合分接近 0 | 正常；调高权重或等行情触发阈值 |
| 加密提示 `api.binance.com` 超时 | 网络/代理问题 | 默认离线合成验证链路；`--live` 才连网，需可达 Binance |
| `ccxt` 导入失败 | 依赖未装 | 确认已 `pip install -r requirements.txt` |

---

## 五、目录速览

```
quant-trading/
├── a_share/
│   ├── watchlist.json     # 自选股 + 个性化规则 rules（改这里）
│   ├── sectors.json       # 五板块扫描配置
│   ├── signal_engine.py   # 四维度打分 + 个性化规则叠加 + 风控
│   ├── screener.py        # 五板块自动选股初筛
│   ├── notify.py          # 钉钉/企微推送（读 .env）
│   ├── run_daily.py       # 每日编排入口（--screener 跑选股）
│   ├── webui.py           # 本地可视面板（按钮启动扫描）
│   └── backtest_skeleton.py  # backtrader 回测骨架
├── crypto/
│   ├── bot_dryrun.py      # CCXT 全自动闭环（零资金 dry-run/testnet）
│   ├── ccxt_demo.py       # CCXT 取数演示
│   └── freqtrade/         # Freqtrade 完整框架脚手架
├── risk/                  # 双线共用风控模块
├── requirements.txt
├── .env.example
├── DEPLOY.md
└── README.md
```

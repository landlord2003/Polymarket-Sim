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

> ⚠️ A股**不自动下单**：本系统只产出「买/持/卖」信号推给你，**手动决策、手动下单**。这是合规 + 风控的硬约束。

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
```
应看到一份 Markdown 格式信号日报，每只标的标注「离线」，且**不推送**。

### 第 6 步：联网实跑（出真实信号 + 推送）
```bash
python a_share/run_daily.py
```
联网正常时，会对 `a_share/watchlist.json` 里的每只股票跑四维度打分，汇总后推送钉钉（配了 `.env` 的话）。

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

---

## 五、目录速览

```
quant-trading/
├── a_share/
│   ├── watchlist.json     # 自选股 + 低空板块标的（改这里）
│   ├── signal_engine.py   # 四维度打分 + 风控闸门
│   ├── notify.py          # 钉钉/企微推送（读 .env）
│   ├── run_daily.py       # 每日编排入口
│   └── backtest_skeleton.py  # backtrader 回测骨架
├── crypto/                # 加密线（P3，CCXT testnet）
├── risk/                  # 双线共用风控模块
├── requirements.txt
├── .env.example
├── DEPLOY.md
└── README.md
```

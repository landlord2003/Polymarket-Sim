# 量化交易自研系统（股票 + 加密）

> 状态：P1 骨架 ✅ ｜ P2 A股信号跟踪 ✅ ｜ P2+ 个性化规则+五板块选股 ✅ ｜ P3 加密模拟盘 ✅
> 原则：**双线并行 · 全模拟起步 · 零资金 · A股暂不走自动下单**
> 主导：老吴（吴自强） ｜ 协办：Claw

## 它能做什么

- ✅ **自动跟踪**你关注的股票（watchlist：元力/钢研/悦安 + 低空板块标的）
- ✅ **个性化信号规则**：钢研"企稳布林下轨18.45买点"、元力"建仓区间25.5-26.5/止损24/阻力29.5"等阈值直接写进 `watchlist.json` → 引擎叠加到四维度信号
- ✅ **五板块自动选股**：新能源 / 新材料 / AI / 机器人 / 军工，循环扫描成分股 → 行情+量能初筛 → TopN 推荐（`screener.py`，`run_daily.py --screener`）
- ✅ **给出交易策略信号**：行情 / 资金 / 板块 / 消息 四维度综合 → 买 / 持 / 卖，经风控闸门后推送钉钉/微信
- ✅ **加密模拟盘全自动链路**：CCXT 取数→RSI/EMA信号→风控→testnet/dry-run 下单（`crypto/bot_dryrun.py`），零资金验证
- ✅ **可视面板 webui**：`python a_share/webui.py` 启动本地服务，浏览器开 `http://127.0.0.1:8787`，**页面内点按钮即可运行扫描**（日常盯盘/板块选股/全部运行），结果实时刷新；`run_daily.py` 仍可在命令行生成静态 `output/dashboard.html` 备用
- ❌ **A股不自动下单**：需 QMT/PTrade（50万+ 门槛），本阶段信号推给你**手动执行**

## 目录结构

```
quant-trading/
├── a_share/                # A股线
│   ├── watchlist.json      # 自选股 + 个性化规则 rules（建仓区间/止损/布林买点）
│   ├── sectors.json        # 五板块扫描配置（新能源/新材料/AI/机器人/军工）
│   ├── signal_engine.py    # 四维度打分 + 个性化规则叠加 + 风控闸门
│   ├── screener.py         # 五板块自动选股初筛
│   ├── notify.py           # 钉钉/企微推送（读 .env）
│   ├── run_daily.py        # 每日信号扫描入口（--screener 跑选股）
│   ├── webui.py            # 本地可视面板（页面内按钮启动扫描）
│   └── backtest_skeleton.py# backtrader 回测骨架
├── crypto/                 # 加密线：CCXT + Freqtrade testnet（P3）
│   ├── bot_dryrun.py       # CCXT 全自动闭环（零资金 dry-run/testnet）
│   ├── ccxt_demo.py        # CCXT 取数演示
│   └── freqtrade/          # Freqtrade 完整框架脚手架（config + 策略）
├── risk/                   # 双线共用风控模块
│   └── risk_control.py
├── data/                   # 本地数据缓存
├── output/                 # 回测报表 / 信号输出
├── requirements.txt
├── .env.example            # 推送凭证 + 加密变量模板（复制为 .env 填值）
├── .gitignore
├── DEPLOY.md               # 另一台电脑部署步骤
├── README.md
└── 策划文档.md             # 项目整体策划 + GitHub 仓库评测
```

## 快速开始（本机）

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # 填入钉钉 Webhook + SECRET（加密变量可选）
python a_share/run_daily.py --offline            # A股离线自检
python a_share/run_daily.py --screener --offline # 五板块选股离线自检
python a_share/run_daily.py --screener          # 联网实跑 + 选股 + 推送
python a_share/run_daily.py --no-html            # 只推送、不生成看板
# 看板生成于 output/dashboard.html，浏览器双击打开即可看（深色/自包含）
python crypto/bot_dryrun.py --once              # 加密模拟盘一轮（dry-run 零资金）
```

另一台电脑完整部署见 **[DEPLOY.md](./DEPLOY.md)**。

## 可视面板（webui）—— 怎么看面板

不想敲命令行？用面板：

```bash
python a_share/webui.py
```

启动后**保持这个终端窗口开着**（Ctrl+C 退出），浏览器打开 **http://127.0.0.1:8787**：

- 顶部按钮：**📊 日常盯盘**（自选股信号）/ **🔎 板块选股**（五板块挖新标的）/ **🚀 全部运行** / 勾 **离线验证**（合成数据跑顺、不联网不推送）/ 勾 **推送钉钉**（联网且配 .env 时推手机）
- 下方实时刷新信号看板；扫描在后台线程跑（联网取数约 30–90 秒），页面每 2 秒自动轮询
- 端口可改环境变量 `QT_WEB_PORT`（默认 8787）

> 全空仓时正好用：日常盯盘等你的买点（钢研回 18.45 布林买点、元力回 25.5 建仓区间），板块选股找新标的建仓。工具只给信号不替你买，手动在券商 App 下单。
>
> 想后台常驻（关终端不退）：`pythonw a_share/webui.py` 或 Windows 任务计划程序。

## 关键约束

- 零资金：默认 dry-run / 纯回测 / 信号推送，不落真实订单。
- A股不自动下单：信号推钉钉/微信，手动决策。
- 回测 ≠ 实盘：需样本外 + 模拟盘交叉验证后再谈小资金实盘（P4）。
- 密钥不入库：`.env` 被 `.gitignore` 排除，钉钉/企微凭证本地提供。
- GitHub 访问不稳：克隆第三方仓库用镜像兜底（kkgithub / ghproxy）。

## 四维度信号说明

| 维度 | 数据源 | 评分逻辑 |
|------|--------|----------|
| 行情 | AkShare 日线 | RSI 超买/超卖、MA20 趋势、布林带位置 |
| 资金 | 个股资金流 | 主力净流入额 + 连续净流入天数 |
| 板块 | 沪深300 对比 | 个股 20 日收益 vs 指数，相对强弱 |
| 消息 | 个股新闻 | 利好/利空关键词情绪打分 |

综合分 → 买入(≥0.5) / 偏多(≥0.15) / 观望 / 减仓 / 卖出。所有信号过 `risk_control.py` 闸门（最大回撤熔断等）。

## 信号调参（个性化规则 + 权重）

所有调参集中在 `a_share/watchlist.json`，改一处即可，无需动代码。

- **个股个性化规则 `rules`**（引擎强制信号优先级：止损 > 布林买点/建仓区间 > 阻力减仓）：
  - `buy_range: [lo, hi]` 价格落入区间 → 强制 🟢买入
  - `boll_lower_buy: 价格` 价格 ≤ 该值×1.03 → 🟢买入（如钢研 18.45、悦安 23.48、万丰 10.00）
  - `stop_loss: 价格` 价格 ≤ 该值 → 强制 🔴卖出（最高优先级）
  - `resistance: 价格` 价格 ≥ 该值 → 提示减仓/止盈
  - `cost_avg: 价格` 仅备注现价与筹码成本对比
- **四维度权重 `weights`**（文件顶层）：`{"market":0.35,"money":0.30,"sector":0.20,"news":0.15}`，一处调全局。

> ⚠️ 悦安/万丰 的 `rules` 为 **2026-08-19 技术参考值**（基于近期高低/布林带），非老吴真实成本，请在 `note` 中按你的建仓价/止损线替换后再用。

## 参考仓库（已在策划文档评测）

- A股轻量回测模板：PandOvo/quant-backtest-portfolio_akshare
- 加密机器人：Freqtrade（dry-run）、CCXT（统一 API + testnet）
- 回测之王：backtrader；实盘平台：vn.py（将来 QMT/PTrade 通道）

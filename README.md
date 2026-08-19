# 量化交易自研系统（股票 + 加密）

> 状态：P1 骨架 ✅ ｜ P2 A股信号跟踪 ✅ ｜ P2+ 个性化规则+五板块选股 ✅ ｜ P3 加密模拟盘 ✅
> 原则：**双线并行 · 全模拟起步 · 零资金 · A股暂不走自动下单**
> 主导：老吴（吴自强） ｜ 协办：Claw

## 它能做什么

- ✅ **自动跟踪**你关注的股票（watchlist：元力/钢研/悦安 + 低空板块标的）
- ✅ **个性化信号规则**：钢研"企稳动态布林下轨买点"、元力"建仓区间25.5-26.5/趋势破位线24/目标止盈29.5"等阈值直接写进 `watchlist.json` → 引擎叠加到四维度信号（当前全空仓，规则按"回补参考"解读）
- ✅ **五板块自动选股**：新能源 / 新材料 / AI / 机器人 / 军工，循环扫描成分股 → 行情+量能初筛 → TopN 推荐（`screener.py`，`run_daily.py --screener`）
- ✅ **给出交易策略信号**：行情 / 资金 / 板块 / 消息 四维度综合 → 买 / 持 / 卖，经风控闸门后推送钉钉/微信
- ✅ **加密模拟盘全自动链路**：CCXT 取数→RSI/EMA信号→风控→testnet/dry-run 下单（`crypto/bot_dryrun.py`），零资金验证
- ✅ **可视面板 webui**：**双击 `启动看板.bat`**（无需敲命令），浏览器自动打开 `http://127.0.0.1:8787`，**页面内点按钮即可运行扫描**（日常盯盘/板块选股/全部运行），结果实时刷新；`run_daily.py` 仍可在命令行生成静态 `output/dashboard.html` 备用
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

## 可视面板（webui）—— 怎么看面板（无需敲命令行）

**双击仓库里的 `启动看板.bat`**，浏览器会自动打开 **http://127.0.0.1:8787**（保持运行它的窗口开着，关掉就停）：

- 顶部按钮：**📊 日常盯盘**（自选股信号）/ **🔎 板块选股**（五板块挖新标的）/ **🚀 全部运行** / 勾 **离线验证**（合成数据跑顺、不联网不推送）/ 勾 **推送钉钉**（联网且配 .env 时推手机）
- **🔄 自动刷新**：勾上后按选定间隔（1/3/5/15 分钟）自动重跑最近一次的扫描模式，状态栏显示下次刷新倒计时。默认勾选 **仅盘中**，非交易时段不空跑；想强制刷新就取消该勾选
- **实时报价条**：面板第二行常驻显示全部自选股当前价与涨跌幅（**涨红跌绿**，A股惯例），每 10 秒自动刷新，走轻量快照接口不跑引擎
- **交易时段徽标**：显示集合竞价 / 盘中 / 午间休市 / 已收盘 / 休市（周末）+ 服务器时间
- 下方信号看板；联网扫描约 5–15 秒（多源直连，已不走 akshare），页面每 2 秒轮询状态
- 每只票都会标注 **数据源 + 数据日期**：`✅ 真实行情｜来源 腾讯财经(前复权)` 或 `⚠️ 合成数据…｜回退原因：…`，一眼分辨真假，不会再把合成价当真
- 端口可改环境变量 `QT_WEB_PORT`（默认 8787）

> 全空仓时正好用：日常盯盘等你的买点（钢研回**动态布林下轨企稳**、元力回 25.5 建仓区间），板块选股找新标的建仓。工具只给信号不替你买，手动在券商 App 下单。
>
> 想后台常驻（关终端不退）：`pythonw a_share/webui.py` 或 Windows 任务计划程序；`.bat` 已自动选 python（项目 `.venv` → 本机 WorkBuddy 托管环境 → 系统 python）。

## 关键约束

- 零资金：默认 dry-run / 纯回测 / 信号推送，不落真实订单。
- A股不自动下单：信号推钉钉/微信，手动决策。
- 回测 ≠ 实盘：需样本外 + 模拟盘交叉验证后再谈小资金实盘（P4）。
- 密钥不入库：`.env` 被 `.gitignore` 排除，钉钉/企微凭证本地提供。
- GitHub 访问不稳：克隆第三方仓库用镜像兜底（kkgithub / ghproxy）。

## 四维度信号说明

| 维度 | 数据源 | 评分逻辑 | 取不到数时 |
|------|--------|----------|-----------|
| 行情 35% | 多源日线（腾讯/东财/新浪） | RSI 超买超卖、MA20 趋势、布林带位置 | 三源全败才回退合成并显式告警 |
| 资金 30% | 东财个股资金流 | 主力净流入额 + 连续净流入天数 | **降级本地量价代理**（上涨日成交额 vs 下跌日 + MFI14），仍有分数并标注「代理」 |
| 板块 20% | 沪深300 指数（腾讯） | 个股 20 日收益 vs 指数，相对强弱 | 降级为个股自身 20 日动量并标注 |
| 消息 15% | 东财个股新闻搜索 | 利好/利空关键词情绪打分 | 置中性 0 分并注明原因 |

综合分 → 买入(≥0.5) / 偏多(≥0.15) / 观望 / 减仓 / 卖出。所有信号过 `risk_control.py` 闸门（最大回撤熔断等）。

### 数据层：为什么不用 akshare 取行情

2026-08-19 实测：`akshare.stock_zh_a_hist` 在本机被 `push2his.eastmoney.com` 以
`RemoteDisconnected` 掐断（服务端 WAF/限流），而腾讯 `web.ifzq` 与新浪
`getKLineData` 均秒回。原引擎 `except Exception: pass` 静默回退合成数据，
导致取消「离线验证」后看到的**仍是随机游走假数据且毫无提示**。

现由 `a_share/datasource.py` 统一直连（仅标准库 urllib，无 requests 依赖）：

- `fetch_kline`：腾讯前复权 → 东财前复权 → 新浪不复权，每源 3 次重试退避
- `fetch_realtime`：新浪批量快照，一次请求拿全部自选股当前价
- `fetch_money_flow` / `fetch_index_kline` / `fetch_news_titles`
- `market_phase`：交易时段判断（集合竞价/盘中/午休/收盘/周末）
- 真实来源写入 `df.attrs['source']`；全部失败时把原因写进 `fallback_reason` 并在看板显示 —— **绝不静默伪装成真实行情**

自检：`python a_share/datasource.py`（可带股票代码参数）。akshare 仍在
`requirements.txt` 里供回测等其他用途，行情主链路已不依赖它。

## 信号调参（个性化规则 + 权重）

所有调参集中在 `a_share/watchlist.json`，改一处即可，无需动代码。

- **持仓状态 `holding`**（文件顶层，默认 `false`=空仓）：决定规则语义——
  - `false`（空仓）：`stop_loss` 视为**趋势破位参考线**（跌破则偏弱、不接飞刀、不报卖出）；`buy_range`/布林买点视为**回补参考**（进入才亮买入）
  - `true`（持仓）：`stop_loss` 跌破报🔴卖出；`resistance` 触及建议减仓
- **个股个性化规则 `rules`**（引擎强制信号优先级：止损 > 动态布林/建仓区间 > 阻力减仓）：
  - `buy_range: [lo, hi]` 价格落入区间 → 强制 🟢买入（回补参考）
  - `use_dynamic_boll: true` 启用**动态布林下轨买点**（每次按实时 20日−2σ 计算），`boll_tol` 容差默认 0.03；价格贴近下轨且未创新低 → 🟢买入
  - `stop_loss: 价格` 跌破 →（空仓）偏弱不接飞刀 /（持仓）🔴卖出
  - `resistance: 价格` 价格 ≥ 该值 → 提示减仓/止盈（持仓时压低信号）
  - `cost_avg: 价格`（可选）仅备注现价与参考成本对比；**空仓无需填**
- **四维度权重 `weights`**（文件顶层）：`{"market":0.35,"money":0.30,"sector":0.20,"news":0.15}`，一处调全局。

> ⚠️ 钢研的"18.45"是早期价格较高时的布林下轨**快照**，现价 15.55 已随价格下移，故改用**动态布林下轨**（不再写死 18.45）。悦安/万丰 的 `rules` 为 **2026-08-19 技术参考值**，非老吴真实成本，可按你的回补价/破位线调整。

## 参考仓库（已在策划文档评测）

- A股轻量回测模板：PandOvo/quant-backtest-portfolio_akshare
- 加密机器人：Freqtrade（dry-run）、CCXT（统一 API + testnet）
- 回测之王：backtrader；实盘平台：vn.py（将来 QMT/PTrade 通道）

# 量化交易自研系统（股票 + 加密）

> 状态：P1 骨架 ✅ ｜ P2 A股信号跟踪 ✅ ｜ 原则：**双线并行 · 全模拟起步 · 零资金 · A股暂不走自动下单**
> 主导：老吴（吴自强） ｜ 协办：Claw

## 它能做什么

- ✅ **自动跟踪**你关注的股票（watchlist：元力/钢研/悦安 + 低空板块标的）
- ✅ **自动选股**雏形：四维度打分过滤器，可在全市场/A股池扩展（见 `signal_engine.py`）
- ✅ **给出交易策略信号**：行情 / 资金 / 板块 / 消息 四维度综合 → 买 / 持 / 卖，经风控闸门后推送钉钉/微信
- ❌ **不自动下单**：A股自动交易需 QMT/PTrade（50万+ 门槛），本阶段信号推给你**手动执行**

## 目录结构

```
quant-trading/
├── a_share/                # A股线
│   ├── watchlist.json      # 自选股 + 低空板块（改这里）
│   ├── signal_engine.py    # 四维度打分 + 风控闸门
│   ├── notify.py           # 钉钉/企微推送（读 .env）
│   ├── run_daily.py        # 每日信号扫描入口
│   └── backtest_skeleton.py# backtrader 回测骨架
├── crypto/                 # 加密线：CCXT + testnet（P3）
│   └── ccxt_demo.py
├── risk/                   # 双线共用风控模块
│   └── risk_control.py
├── data/                   # 本地数据缓存
├── output/                 # 回测报表 / 信号输出
├── requirements.txt
├── .env.example            # 推送凭证模板（复制为 .env 填值）
├── .gitignore
├── DEPLOY.md               # 另一台电脑部署步骤
├── README.md
└── 策划文档.md             # 项目整体策划 + GitHub 仓库评测
```

## 快速开始（本机）

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # 填入钉钉 Webhook + SECRET
python a_share/run_daily.py --offline   # 离线自检
python a_share/run_daily.py              # 联网实跑 + 推送
```

另一台电脑完整部署见 **[DEPLOY.md](./DEPLOY.md)**。

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

## 参考仓库（已在策划文档评测）

- A股轻量回测模板：PandOvo/quant-backtest-portfolio_akshare
- 加密机器人：Freqtrade（dry-run）、CCXT（统一 API + testnet）
- 回测之王：backtrader；实盘平台：vn.py（将来 QMT/PTrade 通道）

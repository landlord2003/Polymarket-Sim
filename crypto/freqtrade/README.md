# Freqtrade 自动化交易脚手架（testnet / dry-run）

本目录是「加密模拟盘全自动链路」的**完整框架版**，与 `../bot_dryrun.py`（轻量 CCXT 版）逻辑一致，
区别是 Freqtrade 自带回测、绘图、参数优化、Web UI，更适合长期实盘化演进。

## 零资金验证路径
1. 安装：`pip install freqtrade`
2. 配置密钥到 `.env`：
   ```
   CRYPTO_API_KEY=你的testnet_key
   CRYPTO_API_SECRET=你的testnet_secret
   ```
   Binance Testnet 申请：https://testnet.binance.vision/
3. 先 dry-run（不碰真钱）：
   ```bash
   freqtrade trade --config user_data/config.json --strategy CcxtDryRunStrategy
   ```
   默认 `dry_run: true`，模拟撮合，验证策略与下单逻辑。
4. 想接 testnet 真测试单：把 `config.json` 里 `dry_run` 改为 `false`（仍用测试资金，零真实风险）。

## 文件
- `user_data/config.json` — 交易所(testnet)、品种、仓位、dry_run 开关
- `user_data/strategies/CcxtDryRunStrategy.py` — EMA金叉+RSI 信号策略（与 bot_dryrun 同逻辑）

## 合规提示
境内访问交易所需合规自担；Testnet 仅用于技术验证，勿接真实资金账户。

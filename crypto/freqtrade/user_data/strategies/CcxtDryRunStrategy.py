"""Freqtrade 策略：与 crypto/bot_dryrun.py 同逻辑的 EMA+RSI 信号。

对应本项目"四维度 → 风控闸门"思路的加密版初版（这里只用行情维度做演示）。
部署（在你本机，需安装 freqtrade + 配置 testnet 密钥到 .env）：
  pip install freqtrade
  freqtrade trade --config user_data/config.json --strategy CcxtDryRunStrategy
默认 dry_run=true（零资金验证）。想接 testnet 真测试单：dry_run=false + 填密钥。
"""

from freqtrade.strategy import IStrategy, DecimalParameter
from pandas import DataFrame
import talib.abstract as ta


class CcxtDryRunStrategy(IStrategy):
    timeframe = "1m"
    can_short = False
    stoploss = -0.05
    minimal_roi = {"0": 0.02}

    ema_fast = 12
    ema_slow = 26

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=self.ema_fast)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=self.ema_slow)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe.loc[
            (dataframe["ema_fast"] > dataframe["ema_slow"]) &
            (dataframe["rsi"] < 70),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe.loc[
            (dataframe["ema_fast"] < dataframe["ema_slow"]) &
            (dataframe["rsi"] > 30),
            "exit_long",
        ] = 1
        return dataframe

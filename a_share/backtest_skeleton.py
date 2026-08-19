"""A股线骨架：AkShare 取数 + backtrader 回测 + 四维度信号占位

零资金原则：
  - 纯回测，不出实盘信号，不自动下单。
  - A股实盘自动下单有硬门槛（QMT/PTrade 需 50万+ 资产），本阶段只做信号 + 推送（手动）。

四维度信号（老吴体系，直接迁移）：
  行情(已落地: 均线交叉) / 资金(北向·主力净流入) / 板块(轮动强弱) / 消息(舆情利空过滤)
  后三者当前为占位，返回中性值，后续接入 AkShare 资金流/板块/新闻接口逐步激活。
"""

import akshare as ak
import backtrader as bt
import pandas as pd

SYMBOL = "300034"      # 钢研高纳（示例标的）
START = "20230101"
END = "20260819"


def _synthetic_data(symbol: str = SYMBOL) -> pd.DataFrame:
    """离线兜底：随机游走生成 OHLCV（仅用于本地验证回测引擎，非真实行情）。"""
    import numpy as np
    rng = np.random.default_rng(42)
    n = 500
    dates = pd.date_range(end=pd.Timestamp.today().normalize(), periods=n, freq="D")
    close = 25.0 + np.cumsum(rng.normal(0, 0.3, n))
    close = np.maximum(close, 1.0)
    open_ = close + rng.normal(0, 0.1, n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.1, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.1, n))
    vol = rng.integers(1_000_000, 5_000_000, n)
    df = pd.DataFrame({"datetime": dates, "open": open_, "high": high,
                      "low": low, "close": close, "volume": vol})
    df.set_index("datetime", inplace=True)
    return df


def load_data(symbol: str = SYMBOL, start: str = START, end: str = END) -> pd.DataFrame:
    try:
        df = ak.stock_zh_a_hist(symbol=symbol, period="daily",
                                start_date=start, end_date=end, adjust="qfq")
        df = df.rename(columns={"日期": "datetime", "开盘": "open", "收盘": "close",
                                "最高": "high", "最低": "low", "成交量": "volume"})
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df[["datetime", "open", "high", "low", "close", "volume"]]
        df.set_index("datetime", inplace=True)
        return df
    except Exception as e:  # 离线兜底：仅验证回测引擎接线
        print(f"[warn] AkShare 取数失败（{e}），使用离线合成数据验证回测引擎")
        return _synthetic_data(symbol)


def dim_money_flow(symbol: str) -> float:
    """资金维度：北向/主力净流入，返回 -1~1（占位）。"""
    return 0.0


def dim_sector_rotation(symbol: str) -> bool:
    """板块维度：是否处于强势板块（占位，默认通过）。"""
    return True


def dim_news(symbol: str) -> bool:
    """消息维度：有无重大利空，True=无利空（占位）。"""
    return True


class FourDimStrategy(bt.Strategy):
    params = dict(fast=12, slow=26)

    def __init__(self):
        self.ma_fast = bt.ind.SMA(period=self.p.fast)
        self.ma_slow = bt.ind.SMA(period=self.p.slow)
        self.cross = bt.ind.CrossOver(self.ma_fast, self.ma_slow)

    def next(self):
        if not self.position:
            if self.cross > 0 and dim_sector_rotation(SYMBOL) and dim_news(SYMBOL):
                self.buy()
        else:
            if self.cross < 0:
                self.close()


def run_backtest() -> None:
    data = load_data()
    feed = bt.feeds.PandasData(dataname=data)
    cerebro = bt.Cerebro()
    cerebro.adddata(feed)
    cerebro.addstrategy(FourDimStrategy)
    cerebro.broker.setcash(100000.0)
    cerebro.broker.setcommission(commission=0.0003)
    print("起始资金: 100,000.00")
    cerebro.run()
    print(f"终值: {cerebro.broker.getvalue():.2f}")


if __name__ == "__main__":
    run_backtest()

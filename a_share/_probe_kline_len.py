"""探测东方财富/腾讯K线接口一次能拉到的最大历史长度（决定扩池复核的 days 取值）。"""
from ml_model import _fetch_kline_timed

if __name__ == "__main__":
    for d in (600, 1200, 1800, 2400):
        df = _fetch_kline_timed("600519", days=d)
        if df is None:
            print(d, "-> None")
            continue
        print(d, "-> len=%d  first=%s  last=%s" % (len(df), str(df.index[0])[:10], str(df.index[-1])[:10]))

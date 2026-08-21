"""正交信息源因子构造。

把 data/alt/ 下的原始数据（龙虎榜/业绩/分析师/事件/北向）对齐成
按 (股票代码, 交易日) 的横截面因子矩阵，与现有价量因子完全独立。

因子清单（11维）：
  lhb_net_20d      龙虎榜净买额/流通市值，滚动20自然日累计（聪明钱异动）
  lhb_times_20d    近20自然日上榜次数
  np_yoy           净利润同比增长（业绩，ffill 到下一期公告）
  rev_yoy          营业总收入同比增长
  roe              净资产收益率
  analyst_rating   分析师投资评级（数值化，ffill）
  analyst_change   评级变化（上调/维持/下调，ffill）
  target_upside    目标价上限相对当前价的空间（ffill）
  event_unlock_20d 未来20日解禁占流通市值比例之和（解禁压力）
  event_repurchase_20d 近20日是否有回购公告（1/0）
  event_holdadd_20d 近20日净增持占流通股比例（增持-减持）
"""
import os
import numpy as np
import pandas as pd

FACTOR_NAMES = [
    "lhb_net_20d", "lhb_times_20d",
    "np_yoy", "rev_yoy", "roe",
    "analyst_rating", "analyst_change", "target_upside",
    "event_unlock_20d", "event_repurchase_20d", "event_holdadd_20d",
]

HERE = os.path.dirname(os.path.abspath(__file__))
ALT = os.path.join(HERE, "data", "alt")
_CACHE = {}

RATING_MAP = {
    "买入": 1.0, "强烈推荐": 1.0, "增持": 0.5, "推荐": 0.5, "谨慎推荐": 0.25,
    "中性": 0.0, "观望": -0.25, "减持": -0.5, "卖出": -1.0, "回避": -1.0,
}
CHANGE_MAP = {
    "维持": 0.0, "不变": 0.0, "上调": 1.0, "调高": 1.0, "下调": -1.0, "调低": -1.0,
    "首次": 0.5, "首次评级": 0.5,
}


def _load(name):
    if name in _CACHE:
        return _CACHE[name]
    p = os.path.join(ALT, name)
    df = pd.read_csv(p, encoding="utf-8-sig", dtype=str) if os.path.exists(p) else None
    _CACHE[name] = df
    return df


def _num(s):
    return pd.to_numeric(s, errors="coerce")


def _ffill_to(ser, td):
    """对(事件日->值)序列去重后，向前填充对齐到交易日 td，返回 Series(index=td)。"""
    ser = ser[~ser.index.duplicated(keep="last")].sort_index()
    return ser.reindex(td, method="ffill").fillna(0.0)


def build(symbol: str, trade_dates, closes=None) -> pd.DataFrame:
    """返回 DataFrame(index=trade_dates, columns=11因子)。均为对齐到交易日的横截面快照。"""
    td = pd.to_datetime(trade_dates)
    tmax = td.max()
    cols = {}

    # ---------- 龙虎榜（滚动20自然日） ----------
    lhb = _load("lhb_detail.csv")
    if lhb is not None and len(lhb):
        sub = lhb[lhb["代码"] == symbol]
        if len(sub):
            sub = sub.copy()
            sub["上榜日"] = pd.to_datetime(sub["上榜日"], errors="coerce")
            sub["val"] = _num(sub["龙虎榜净买额"]) / _num(sub["流通市值"])
            sub = sub.dropna(subset=["上榜日", "val"]).set_index("上榜日").sort_index()
            sub = sub[~sub.index.duplicated(keep="last")]
            if len(sub):
                full = pd.date_range(sub.index.min(), tmax, freq="D")
                s_val = sub["val"].reindex(full, fill_value=0.0).rolling("20D", min_periods=1).sum()
                s_cnt = pd.Series(1.0, index=sub.index).reindex(full, fill_value=0.0).rolling("20D", min_periods=1).sum()
                cols["lhb_net_20d"] = s_val.reindex(td, method="ffill").fillna(0.0).values
                cols["lhb_times_20d"] = s_cnt.reindex(td, method="ffill").fillna(0.0).values
            else:
                cols["lhb_net_20d"] = np.zeros(len(td)); cols["lhb_times_20d"] = np.zeros(len(td))
        else:
            cols["lhb_net_20d"] = np.zeros(len(td)); cols["lhb_times_20d"] = np.zeros(len(td))
    else:
        cols["lhb_net_20d"] = np.zeros(len(td)); cols["lhb_times_20d"] = np.zeros(len(td))

    # ---------- 业绩（ffill 到下一期公告） ----------
    yj = _load("yjbb.csv")
    if yj is not None and len(yj):
        sub = yj[yj["股票代码"] == symbol]
        if len(sub):
            sub = sub.copy()
            sub["公告日"] = pd.to_datetime(sub["最新公告日期"], errors="coerce")
            for col, out in [("净利润-同比增长", "np_yoy"),
                             ("营业总收入-同比增长", "rev_yoy"),
                             ("净资产收益率", "roe")]:
                sub[out] = _num(sub[col])
            sub = sub.dropna(subset=["公告日"]).sort_values("公告日")
            for out in ("np_yoy", "rev_yoy", "roe"):
                cols[out] = _ffill_to(sub.set_index("公告日")[out], td).values
        else:
            for out in ("np_yoy", "rev_yoy", "roe"):
                cols[out] = np.zeros(len(td))
    else:
        for out in ("np_yoy", "rev_yoy", "roe"):
            cols[out] = np.zeros(len(td))

    # ---------- 分析师（ffill） ----------
    an = _load("analyst.csv")
    if an is not None and len(an):
        sub = an[an["证券代码"] == symbol]
        if len(sub):
            sub = sub.copy()
            sub["发布日"] = pd.to_datetime(sub["发布日期"], errors="coerce")
            sub["rating"] = sub["投资评级"].map(RATING_MAP)
            sub["change"] = sub["评级变化"].map(CHANGE_MAP)
            sub["tgt_up"] = _num(sub["目标价格-上限"])
            sub = sub.dropna(subset=["发布日"]).sort_values("发布日")
            cols["analyst_rating"] = _ffill_to(sub.set_index("发布日")["rating"], td).values
            cols["analyst_change"] = _ffill_to(sub.set_index("发布日")["change"], td).values
            if closes is not None:
                cd = closes if isinstance(closes, pd.Series) else pd.Series(closes, index=trade_dates)
                cd = _num(cd.reindex(list(trade_dates)))
                tgt = _ffill_to(sub.set_index("发布日")["tgt_up"], td)
                upside = tgt.values / cd.values - 1.0
                cols["target_upside"] = np.where(np.isfinite(upside), upside, 0.0)
            else:
                cols["target_upside"] = np.zeros(len(td))
        else:
            for c in ("analyst_rating", "analyst_change", "target_upside"):
                cols[c] = np.zeros(len(td))
    else:
        for c in ("analyst_rating", "analyst_change", "target_upside"):
            cols[c] = np.zeros(len(td))

    # ---------- 事件：解禁（未来20日） ----------
    r = _load("restrict.csv")
    if r is not None and len(r):
        sub = r[r["代码"] == symbol]
        if len(sub):
            sub = sub.copy()
            sub["解禁时间"] = pd.to_datetime(sub["解禁时间"], errors="coerce")
            sub["占比"] = _num(sub["占流通市值比例"]).fillna(0)
            sub = sub.dropna(subset=["解禁时间"])
            out = [sub[(sub["解禁时间"] >= d) & (sub["解禁时间"] <= d + pd.Timedelta(days=20))]["占比"].sum() for d in td]
            cols["event_unlock_20d"] = np.array(out, dtype=float)
        else:
            cols["event_unlock_20d"] = np.zeros(len(td))
    else:
        cols["event_unlock_20d"] = np.zeros(len(td))

    # ---------- 事件：回购（近20日） ----------
    rp = _load("repurchase.csv")
    if rp is not None and len(rp):
        sub = rp[rp["股票代码"] == symbol]
        if len(sub):
            sub = sub.copy()
            sub["公告日"] = pd.to_datetime(sub["最新公告日期"], errors="coerce")
            sub = sub.dropna(subset=["公告日"])
            out = [1.0 if len(sub[(sub["公告日"] >= d - pd.Timedelta(days=20)) & (sub["公告日"] <= d)]) else 0.0 for d in td]
            cols["event_repurchase_20d"] = np.array(out, dtype=float)
        else:
            cols["event_repurchase_20d"] = np.zeros(len(td))
    else:
        cols["event_repurchase_20d"] = np.zeros(len(td))

    # ---------- 事件：增减持（近20日净增持占比） ----------
    gg = _load("ggcg.csv")
    if gg is not None and len(gg):
        sub = gg[gg["代码"] == symbol]
        if len(sub):
            sub = sub.copy()
            sub["公告日"] = pd.to_datetime(sub["公告日"], errors="coerce")
            sub["增减"] = sub["持股变动信息-增减"]
            sub["占比"] = _num(sub["持股变动信息-占流通股比例"]).fillna(0)
            sub = sub.dropna(subset=["公告日"])
            out = []
            for d in td:
                w = sub[(sub["公告日"] >= d - pd.Timedelta(days=20)) & (sub["公告日"] <= d)]
                net = w[w["增减"] == "增持"]["占比"].sum() - w[w["增减"] == "减持"]["占比"].sum()
                out.append(float(net))
            cols["event_holdadd_20d"] = np.array(out, dtype=float)
        else:
            cols["event_holdadd_20d"] = np.zeros(len(td))
    else:
        cols["event_holdadd_20d"] = np.zeros(len(td))

    return pd.DataFrame(cols, index=trade_dates)

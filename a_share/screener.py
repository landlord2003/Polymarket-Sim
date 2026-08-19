"""五板块自动选股扫描（新能源 / 新材料 / AI / 机器人 / 军工）

思路：对每个目标板块经 AkShare 取成分股 → 循环跑「行情维度(RSI/MA/布林) + 量能异动」
轻量初筛（不逐个打资金/新闻接口，避免海量 API 请求）→ 按综合分排序 → 输出每板块 TopN 推荐。

原则：
- 初筛只给「候选清单 + 强度分」，不替你下注；Top 标的若想深入，加进 watchlist.json 跑完整四维度。
- AkShare 不可达时走离线兜底（用内置样本成分股 + 合成分），仅验证链路，不产出真实信号。
"""

from __future__ import annotations

import json
import os
from datetime import datetime

import numpy as np
import pandas as pd

try:
    import akshare as ak
except Exception:  # pragma: no cover
    ak = None

HERE = os.path.dirname(os.path.abspath(__file__))
SECTORS_PATH = os.path.join(HERE, "sectors.json")

# 离线兜底用的各板块样本成分股（真实代码/名称，仅用于沙箱验证链路，分数合成）
OFFLINE_SAMPLES = {
    "新能源": [("300750", "宁德时代"), ("002594", "比亚迪"), ("601012", "隆基绿能"),
              ("300274", "阳光电源"), ("300014", "亿纬锂能"), ("600438", "通威股份"),
              ("002466", "天齐锂业"), ("002460", "赣锋锂业")],
    "新材料": [("300699", "光威复材"), ("600862", "中航高科"), ("300395", "菲利华"),
              ("603826", "坤彩科技"), ("688122", "西部超导"), ("600456", "宝钛股份"),
              ("300034", "钢研高纳"), ("688786", "悦安新材")],
    "AI": [("002230", "科大讯飞"), ("688256", "寒武纪"), ("002415", "海康威视"),
          ("603019", "中科曙光"), ("601360", "三六零"), ("002261", "拓维信息"),
          ("688111", "金山办公"), ("000977", "浪潮信息")],
    "机器人": [("002747", "埃斯顿"), ("300124", "汇川技术"), ("300024", "机器人"),
              ("300607", "拓斯达"), ("688017", "绿的谐波"), ("002472", "双环传动"),
              ("603728", "鸣志电器"), ("002896", "中大力德")],
    "军工": [("600760", "中航沈飞"), ("600893", "航发动力"), ("000768", "中航西飞"),
            ("300034", "钢研高纳"), ("600316", "洪都航空"), ("600038", "中直股份"),
            ("600150", "中国船舶"), ("600967", "内蒙一机")],
}


def load_sectors(path: str = SECTORS_PATH) -> list:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["scan_sectors"]


def get_constituents(label: str, candidates: list) -> tuple:
    """返回 (成分股列表[(code,name)...], offline:bool)。offline=True 表示用内置样本。"""
    if ak is None:
        return OFFLINE_SAMPLES.get(label, [("000001", "样本A")]), True

    for btype in ("concept", "industry"):
        name_getter = ak.stock_board_concept_name_em if btype == "concept" else ak.stock_board_industry_name_em
        cons_getter = ak.stock_board_concept_cons_em if btype == "concept" else ak.stock_board_industry_cons_em
        try:
            boards = name_getter()
            name_col = "板块名称" if "板块名称" in boards.columns else boards.columns[0]
            matched = []
            for cand in candidates:
                hits = boards[boards[name_col].astype(str).str.contains(cand, na=False)]
                matched += hits[name_col].tolist()
            seen = set()
            names = []
            for m in matched:
                if m not in seen:
                    seen.add(m)
                    names.append(m)
            allc = []
            for b in names:
                try:
                    df = cons_getter(symbol=b)
                    code_col = "代码" if "代码" in df.columns else df.columns[0]
                    name_col2 = "名称" if "名称" in df.columns else df.columns[1]
                    for _, row in df.iterrows():
                        allc.append((str(row[code_col]), str(row[name_col2])))
                except Exception:
                    continue
            seen2 = set()
            out = []
            for c, n in allc:
                if c not in seen2:
                    seen2.add(c)
                    out.append((c, n))
            if out:
                return out, False
        except Exception:
            continue
    # 都失败 → 离线样本兜底
    return OFFLINE_SAMPLES.get(label, [("000001", "样本A")]), True


def _volume_anomaly(df: pd.DataFrame) -> float:
    try:
        vol = df["volume"].astype(float)
        if len(vol) < 20:
            return 0.0
        recent = vol.tail(5).mean()
        base = vol.tail(60).mean()
        if base <= 0:
            return 0.0
        ratio = recent / base
        if ratio >= 1.8:
            return 0.4
        if ratio >= 1.3:
            return 0.2
        if ratio <= 0.6:
            return -0.2
        return 0.0
    except Exception:
        return 0.0


def _market_score(df: pd.DataFrame) -> tuple:
    """复用 signal_engine 的行情维度逻辑（RSI/MA/布林）。"""
    from signal_engine import dim_market
    try:
        return dim_market(df)
    except Exception as e:
        return 0.0, [f"行情数据缺失:{e}"]


def screen_sector(label: str, candidates: list, top_n: int = 8,
                  offline: bool = False) -> tuple:
    """扫描单板块，返回 (TopN 行列表, offline)。"""
    if offline:
        cons, off = OFFLINE_SAMPLES.get(label, [("000001", "样本A")]), True
    else:
        cons, off = get_constituents(label, candidates)

    rows = []
    for sym, nm in cons:
        from signal_engine import load_price
        df, doff = load_price(sym, force_offline=offline)
        if doff:
            off = True
        s_mkt, n_mkt = _market_score(df)
        s_vol = _volume_anomaly(df)
        score = max(-1.0, min(1.0, 0.6 * s_mkt + 0.4 * s_vol))
        try:
            last = float(df["close"].iloc[-1])
        except Exception:
            last = 0.0
        rows.append({
            "symbol": sym, "name": nm, "score": round(score, 3),
            "last": last, "mkt": round(s_mkt, 2), "vol": round(s_vol, 2),
            "note": "；".join(n_mkt),
        })
    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows[:top_n], off


def run_screener(top_n: int = 8, offline: bool = False) -> dict:
    """跑全部板块，返回 {label: {"rows":[...], "offline":bool}}。"""
    sectors = load_sectors()
    result = {}
    for s in sectors:
        rows, off = screen_sector(s["label"], s["candidates"], top_n=top_n, offline=offline)
        result[s["label"]] = {"rows": rows, "offline": off}
    return result


def build_screener_report(result: dict) -> str:
    today = datetime.today().strftime("%Y-%m-%d")
    lines = [f"# 🔎 五板块自动选股初筛 {today}\n"]
    for label, blk in result.items():
        off_tag = "（离线样本·非真实信号）" if blk["offline"] else ""
        lines.append(f"## 🧩 {label} 板块 Top 推荐 {off_tag}\n")
        lines.append("| 排名 | 代码 | 名称 | 强度分 | 最新价 | 行情 | 量能 | 备注 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(blk["rows"], 1):
            lines.append(
                f"| {i} | {r['symbol']} | {r['name']} | {r['score']:+.2f} | "
                f"{r['last']:.2f} | {r['mkt']:+.2f} | {r['vol']:+.2f} | {r['note']} |"
            )
        lines.append("")
    lines.append("> 初筛仅给候选清单，深入研判请把标的加进 watchlist.json 跑完整四维度。信号仅供研究，风险自担。")
    return "\n".join(lines)


if __name__ == "__main__":
    # 离线自测
    res = run_screener(offline=True)
    print(build_screener_report(res))

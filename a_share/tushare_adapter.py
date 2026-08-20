"""Tushare 适配器（可选，需 token）。

为什么有它：东财接口对单 IP 间歇限流，详情页的「财务」与「资金流」经常取不到。
Tushare 的 `fina_indicator`（财务）、`moneyflow`（个股资金流）更稳定，但需要
注册 token 且部分接口需积分（资金流接口约 200 积分，可签到/分享攒或购买）。

启用方式：在项目根目录 `.env` 加一行
    TUSHARE_TOKEN=你的token
或在环境变量导出。无 token / 未装 tushare 时，本模块所有函数返回 None，
上层自动降级回东财（再失败则用本地代理/回填），**绝不抛错阻断页面**。

注意：本文件未随主程序自动 import，避免无 token 环境因 import tushare 失败而崩。
由 webui._build_stock_detail 在需要时惰性调用。
"""

from __future__ import annotations

import os
from typing import Optional


def _load_token() -> Optional[str]:
    tok = os.environ.get("TUSHARE_TOKEN")
    if tok:
        return tok
    # 从项目根 .env 读取（与 notify.py 的 _load_dotenv 同源约定）
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = os.path.join(root, ".env")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("TUSHARE_TOKEN"):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001
        pass
    return None


def _ts_code(symbol: str) -> str:
    """A股代码转 tushare ts_code：深市(0/3)→.SZ，沪市(6/5/9)→.SH，北交所→.BJ。"""
    if symbol.startswith(("6", "5", "9")):
        return symbol + ".SH"
    if symbol.startswith(("4", "8")):
        return symbol + ".BJ"
    return symbol + ".SZ"


def fetch_financials_tushare(symbol: str) -> Optional[dict]:
    """Tushare 财务指标（最新一期）：营收/归母净利/ROE/毛利率/净利同比。

    返回与 datasource.fetch_financials 同形 dict；不可用返回 None。
    """
    tok = _load_token()
    if not tok:
        return None
    try:
        import tushare as ts  # 惰性导入，避免无 token 环境崩溃
    except Exception:  # noqa: BLE001
        return None
    try:
        ts.set_token(tok)
        pro = ts.pro_api()
        df = pro.fina_indicator(ts_code=_ts_code(symbol), periods="2")
        if df is None or len(df) == 0:
            return None
        row = df.iloc[0]
        roe = row.get("roe") or row.get("roe_dt") or row.get("roe_yearly")
        gm = row.get("grossprofit_margin")
        np_yoy = row.get("netprofit_yoy") or row.get("q_netprofit_yoy")
        end = row.get("end_date") or ""
        return {
            "report_date": str(end)[:10] if end else "",
            "revenue": None,            # fina_indicator 不含营收，营收另需 income 接口
            "net_profit": None,         # 同上
            "roe": float(roe) if roe not in (None, "") else None,
            "gross_margin": float(gm) if gm not in (None, "") else None,
            "profit_yoy": float(np_yoy) if np_yoy not in (None, "") else None,
            "source": "Tushare(fina_indicator)",
        }
    except Exception:  # noqa: BLE001
        return None


def fetch_money_flow_tushare(symbol: str) -> Optional[dict]:
    """Tushare 个股资金流（最新一日）：主力/超大单/大单/中单/小单 净流入(元)。

    返回与 datasource.fetch_fund_flow_breakdown 同形 dict；不可用返回 None。
    """
    tok = _load_token()
    if not tok:
        return None
    try:
        import tushare as ts
    except Exception:  # noqa: BLE001
        return None
    try:
        ts.set_token(tok)
        pro = ts.pro_api()
        df = pro.moneyflow(ts_code=_ts_code(symbol))
        if df is None or len(df) == 0:
            return None
        row = df.iloc[0]
        # 单位：Tushare 资金流为「千元」，换算为元
        k = 1_000.0
        return {
            "main": float(row.get("buy_sm_amount", 0) - row.get("sell_sm_amount", 0)) * k
                    if False else float(row.get("main_net_in", 0) or 0) * k,
            "huge": float(row.get("hg_net_in", 0) or 0) * k,
            "big": float(row.get("lg_net_in", 0) or 0) * k,
            "mid": float(row.get("md_net_in", 0) or 0) * k,
            "retail": float(row.get("sm_net_in", 0) or 0) * k,
            "source": "Tushare(moneyflow)",
        }
    except Exception:  # noqa: BLE001
        return None

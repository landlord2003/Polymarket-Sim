# -*- coding: utf-8 -*-
"""P3-4 金融风控层：单市场/总仓位上限 + 日亏损限额 + kill switch。

与合规红线(COMPLIANCE_FILTER)无关，专管「钱的风险」。实盘(clob_exec)下单前必须调
check_new_order()；kill switch 触发后停止一切新单并钉钉告警。

配置（环境变量）：
  MAX_POS_PER_MARKET  单市场最大仓位(USDC)   默认 200
  MAX_TOTAL_POS       总仓位上限(USDC)        默认 2000
  DAILY_LOSS_LIMIT    日亏损限额(USDC)        默认 100
  RISK_PERSIST=1      将 kill_switch / 日亏损落盘 data/risk_state.json（跨重启保留）
"""
import os
import json
import time
import threading
from datetime import date

try:
    from notify import send_markdown as _ding
except Exception:  # 离线/未配置时静默
    _ding = None

MAX_POS_PER_MARKET = float(os.environ.get("MAX_POS_PER_MARKET", "200"))
MAX_TOTAL_POS = float(os.environ.get("MAX_TOTAL_POS", "2000"))
DAILY_LOSS_LIMIT = float(os.environ.get("DAILY_LOSS_LIMIT", "100"))
RISK_PERSIST = os.environ.get("RISK_PERSIST", "1") == "1"
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "risk_state.json")

_lock = threading.Lock()
_positions = {}          # token_id -> 当前仓位 notional(USDC)
_day = None              # 当前日期字符串
_day_pnl = 0.0           # 当日已实现 PnL(USDC)
_kill = {"on": False, "at": None, "reason": None}


def _today():
    return date.today().isoformat()


def _load():
    global _day, _day_pnl
    if not RISK_PERSIST:
        _day = _today()
        return
    try:
        with open(STATE_PATH) as f:
            d = json.load(f)
        _kill.update(d.get("kill", {}))
        _day = d.get("day")
        if _day != _today():          # 跨日重置日亏损累计
            _day = _today()
            _day_pnl = 0.0
        else:
            _day_pnl = float(d.get("day_pnl", 0.0))
    except Exception:
        _day = _today()
        _day_pnl = 0.0


def _save():
    if not RISK_PERSIST:
        return
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump({"kill": _kill, "day": _day, "day_pnl": _day_pnl}, f)
    except Exception:
        pass


def total_pos():
    return sum(_positions.values())


def check_new_order(token_id, size_usd):
    """实盘下单前调用。返回 (allowed: bool, reason: str)。"""
    with _lock:
        if _kill["on"]:
            return False, "kill_switch_on"
        if size_usd <= 0:
            return False, "invalid_size"
        if size_usd > MAX_POS_PER_MARKET:
            return False, "exceed_max_pos_per_market"
        cur = _positions.get(token_id, 0.0)
        if cur + size_usd > MAX_POS_PER_MARKET:
            return False, "exceed_max_pos_per_market"
        if total_pos() + size_usd > MAX_TOTAL_POS:
            return False, "exceed_max_total_pos"
        # 预估该单最大可能亏损（全部 size）后是否击穿日亏限额
        if _day_pnl - size_usd < -DAILY_LOSS_LIMIT:
            return False, "exceed_daily_loss_limit"
        return True, "ok"


def record_fill(token_id, size_usd, realized_pnl=0.0):
    """成交后更新仓位与日盈亏。"""
    with _lock:
        global _day_pnl
        if _day != _today():
            _day = _today()
            _day_pnl = 0.0
        _positions[token_id] = _positions.get(token_id, 0.0) + max(size_usd, 0.0)
        _day_pnl += realized_pnl
        _save()


def trigger_kill_switch(reason="manual"):
    """触发 kill switch：停止一切新单 + 钉钉告警。"""
    with _lock:
        _kill["on"] = True
        _kill["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _kill["reason"] = reason
        _save()
    if _ding:
        try:
            _ding("🔴 风控 kill switch 触发",
                  "## 🔴 实盘已停止一切新单\n\n**原因**: %s\n\n**时间**: %s\n\n"
                  "> Quant-Trading 金融风控层自动告警" % (reason, _kill["at"]))
        except Exception:
            pass
    return dict(_kill)


def reset_kill_switch():
    """解除 kill switch（仅限人工确认后调用）。"""
    with _lock:
        _kill["on"] = False
        _kill["at"] = None
        _kill["reason"] = None
        _save()
    return dict(_kill)


def status():
    """供 /api/risk 返回当前风控状态。"""
    with _lock:
        return {
            "kill_switch": dict(_kill),
            "max_pos_per_market": MAX_POS_PER_MARKET,
            "max_total_pos": MAX_TOTAL_POS,
            "daily_loss_limit": DAILY_LOSS_LIMIT,
            "total_pos": total_pos(),
            "positions": dict(_positions),
            "day": _day,
            "day_pnl": round(_day_pnl, 2),
            "persist": RISK_PERSIST,
        }


_load()

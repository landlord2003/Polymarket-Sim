# -*- coding: utf-8 -*-
"""P3-4 金融风控层：单市场/总仓位上限 + 日亏损限额 + kill switch。

与合规红线(COMPLIANCE_FILTER)无关，专管「钱的风险」。实盘(clob_exec)下单前必须调
check_new_order()；kill switch 触发后停止一切新单并钉钉告警。

配置（环境变量）：
  MAX_POS_PER_MARKET  单市场最大仓位(USDC)   默认 200
  MAX_TOTAL_POS       总仓位上限(USDC)        默认 2000
  DAILY_LOSS_LIMIT    日亏损限额(USDC)        默认 100（当日已实现亏损≤-该值即自动熔断）
  DRAWDOWN_LIMIT      回撤熔断阈值(0~1)       默认 0.15（权益较峰值回撤≥15%即自动熔断）
  BANKROLL_FLOOR_FRAC 本金下限(占初始比例)    默认 0.70（权益<初始×70%即自动熔断）
  RISK_PERSIST=1      将 kill_switch / 日亏损落盘 data/risk_state.json（跨重启保留）

组合级熔断（evaluate_portfolio_guard）：日亏 / 回撤 / 本金下限任一触限即自动触发 kill switch
（停止一切新单 + 钉钉告警），需人工 /api/kill_switch?action=off 复位。DRY_RUN 模拟盘权益通常远高于
阈值，不会误触发；该护栏为实盘（NB 部署）硬前置。
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
DRAWDOWN_LIMIT = float(os.environ.get("DRAWDOWN_LIMIT", "0.15"))          # 权益较峰值回撤阈值(0~1)
BANKROLL_FLOOR_FRAC = float(os.environ.get("BANKROLL_FLOOR_FRAC", "0.70"))  # 权益低于初始×该比例即熔断
RISK_PERSIST = os.environ.get("RISK_PERSIST", "1") == "1"
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "risk_state.json")

_lock = threading.RLock()  # 可重入：evaluate_portfolio_guard 持锁调用 trigger_kill_switch 时不会死锁
_positions = {}          # token_id -> 当前仓位 notional(USDC)
_day = None              # 当前日期字符串
_day_pnl = 0.0           # 当日已实现 PnL(USDC)
_kill = {"on": False, "at": None, "reason": None}
_guards = {                       # 组合级熔断实时状态（供看板红灯 + /api/state）
    "guarded": False, "reason": None,
    "dd_pct": 0.0, "bankroll_pct": 100.0,
    "drawdown_breach": False, "bankroll_breach": False, "daily_loss_breach": False,
    "drawdown_limit_pct": round(DRAWDOWN_LIMIT * 100, 2),
    "bankroll_floor_pct": round(BANKROLL_FLOOR_FRAC * 100, 2),
    "daily_loss_limit": DAILY_LOSS_LIMIT,
}


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


def evaluate_portfolio_guard(equity, peak, cash, initial_capital):
    """组合级熔断评估：日亏 / 回撤 / 本金下限任一触限 → 自动 kill switch。

    由 sim_server 的周期守护线程调用（传入当前 equity / 峰值 / 现金 / 初始资金）。
    返回最新 guards 快照。已熔断时幂等（不再重复触发）。
    """
    with _lock:
        peak = peak or equity
        dd = (peak - equity) / peak if peak > 0 else 0.0
        dd = max(dd, 0.0)
        floor = (initial_capital or 0.0) * BANKROLL_FLOOR_FRAC
        bankroll_pct = (equity / initial_capital * 100.0) if initial_capital > 0 else 100.0
        daily_loss_breach = _day_pnl <= -DAILY_LOSS_LIMIT
        dd_breach = dd >= DRAWDOWN_LIMIT
        bankroll_breach = equity < floor
        _guards.update({
            "dd_pct": round(dd * 100, 2),
            "bankroll_pct": round(bankroll_pct, 2),
            "drawdown_breach": dd_breach,
            "bankroll_breach": bankroll_breach,
            "daily_loss_breach": daily_loss_breach,
            "drawdown_limit_pct": round(DRAWDOWN_LIMIT * 100, 2),
            "bankroll_floor_pct": round(BANKROLL_FLOOR_FRAC * 100, 2),
            "daily_loss_limit": DAILY_LOSS_LIMIT,
        })
        if _kill["on"]:
            _guards["guarded"] = True
            _guards["reason"] = _kill.get("reason")
            return dict(_guards)
        reason = None
        if daily_loss_breach:
            reason = "daily_loss"
        elif dd_breach:
            reason = "drawdown"
        elif bankroll_breach:
            reason = "bankroll_floor"
        if reason:
            trigger_kill_switch(reason=reason)
            _guards["guarded"] = True
            _guards["reason"] = reason
        else:
            _guards["guarded"] = False
            _guards["reason"] = None
        return dict(_guards)


def guard_view():
    """轻量 guards 快照，供 /api/state 与看板红灯使用。"""
    with _lock:
        return dict(_guards)


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
            "drawdown_limit": DRAWDOWN_LIMIT,
            "bankroll_floor_frac": BANKROLL_FLOOR_FRAC,
            "guards": dict(_guards),
            "total_pos": total_pos(),
            "positions": dict(_positions),
            "day": _day,
            "day_pnl": round(_day_pnl, 2),
            "persist": RISK_PERSIST,
        }


_load()

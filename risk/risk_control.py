"""风控模块（A股 / 加密双线共用）

核心闸门：单笔仓位上限、总仓位上限、止损、移动止损、最大回撤熔断、黑天鹅暂停。
设计原则：任何交易决策在落单前，必须先过 gate() 闸门。
策略不值钱，风控才值钱 —— 这是活到赚钱那天的前提。
"""

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass
class RiskConfig:
    max_position_pct: float = 0.30        # 单标的 / 单笔最大仓位（占总资金比例）
    max_total_position_pct: float = 0.80  # 总仓位上限
    stop_loss_pct: float = 0.08           # 单笔止损线（8%）
    trailing_stop_pct: float = 0.12       # 移动止损（12%）
    max_drawdown_pct: float = 0.20        # 整体最大回撤熔断（20%）
    black_swan_pause: bool = False         # 黑天鹅暂停开关（手动/消息触发）


class RiskController:
    def __init__(self, cfg: Optional[RiskConfig] = None):
        self.cfg = cfg or RiskConfig()
        self.peak_equity: Optional[float] = None
        self.equity: Optional[float] = None

    def init_equity(self, equity: float) -> None:
        self.equity = equity
        self.peak_equity = equity

    def update_equity(self, equity: float) -> None:
        self.equity = equity
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity

    def current_drawdown(self) -> float:
        if self.peak_equity in (None, 0):
            return 0.0
        return (self.peak_equity - self.equity) / self.peak_equity

    def check_drawdown_breaker(self) -> bool:
        """触发最大回撤熔断返回 True（应暂停所有交易）。"""
        if self.cfg.black_swan_pause:
            return True
        return self.current_drawdown() >= self.cfg.max_drawdown_pct

    def position_size(self, price: float, total_equity: float,
                      vol_target: Optional[float] = None) -> float:
        """计算可买入数量（股 / 币），已封顶仓位上限。"""
        if price <= 0:
            return 0.0
        budget = total_equity * self.cfg.max_position_pct
        if vol_target:
            budget = min(budget, total_equity * vol_target)
        return budget / price

    def should_stop_loss(self, entry_price: float, current_price: float) -> bool:
        if entry_price <= 0:
            return False
        return (entry_price - current_price) / entry_price >= self.cfg.stop_loss_pct

    def should_trailing_stop(self, peak_price: float, current_price: float) -> bool:
        if peak_price <= 0:
            return False
        return (peak_price - current_price) / peak_price >= self.cfg.trailing_stop_pct

    def gate(self, action: str = "trade") -> Tuple[bool, str]:
        """所有下单前必经。返回 (是否放行, 原因)。"""
        if self.check_drawdown_breaker():
            return False, (
                f"最大回撤熔断 {self.current_drawdown():.1%} "
                f">= {self.cfg.max_drawdown_pct:.1%}"
            )
        return True, "ok"


if __name__ == "__main__":
    rc = RiskController()
    rc.init_equity(100000.0)
    rc.update_equity(82000.0)
    print("当前回撤:", f"{rc.current_drawdown():.1%}")
    ok, reason = rc.gate()
    print("闸门:", ok, reason)
    qty = rc.position_size(price=25.5, total_equity=100000.0)
    print("建议买入数量(股):", int(qty))

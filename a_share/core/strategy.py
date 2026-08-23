# -*- coding: utf-8 -*-
"""统一撮合 / 盈亏原语（Phase 1 抽离，消除三模块重复实现）。

设计原则：
  - 纯 Python 标准库，无第三方依赖（与项目 urllib 约定一致）。
  - 只放「算术原语」，不放策略流程；三处仍各自保留其策略逻辑，
    但盈亏 / 费用 / 均价这些最易漂移的计算统一在此，且单测可覆盖。
  - 所有函数保持与旧实现**数值完全一致**（见仓库 scripts 验证）。

三模块原模型对照：
  - arb_backtest：历史 mid 构造双边盘口，成对做市；费用 = (建仓成交额 + 平仓成交额) × 费率。
  - arb_book    ：逐腿做市，单边腿费用 = 成交额 × 费率；库存归 0 时锁定 (平仓价 - 均价) × 份额 - 双腿费。
  - sim_engine  ：A股模拟，无费率；盈亏 = (现价 - 成本) × 股数；均价 = 加权。
"""
from __future__ import annotations


def leg_fee(price: float, size: float, fee_rate: float) -> float:
    """单边腿手续费：成交额 × 费率（arb_book / 通用做市腿）。"""
    return float(price) * float(size) * float(fee_rate)


def pair_fee(enter_notional: float, exit_notional: float, fee_rate: float) -> float:
    """成对做市双腿手续费：(建仓成交额 + 平仓成交额) × 费率（arb_backtest 模型）。"""
    return (float(enter_notional) + float(exit_notional)) * float(fee_rate)


def realized_pnl(avg_cost: float, exit_price: float, size: float) -> float:
    """已实现盈亏（多头平仓口径）：(平仓价 - 建仓均价) × 份额。"""
    return (float(exit_price) - float(avg_cost)) * float(size)


def unrealized_pnl(avg_cost: float, mid: float, net: float) -> float:
    """未实现盈亏：用**带符号库存**(+多 -空) 统一长/空两向 = (mid - avg_cost) × net。

    与 arb_book.view 的「多:(mid-avg)×net / 空:(avg-mid)×(-net)」、
    sim_engine.mark_to_market 的「(cur - cost_price) × qty」三式完全等价。
    """
    return (float(mid) - float(avg_cost)) * float(net)


def weighted_avg_cost(prev_avg: float, prev_qty: float,
                      price: float, qty: float) -> float:
    """通用加权建仓均价（sim_engine 口径）：(旧均×旧量 + 新价×新量) / (旧量 + 新量)。"""
    tot = float(prev_qty) + float(qty)
    if tot <= 0:
        return float(price)
    return (float(prev_avg) * float(prev_qty) + float(price) * float(qty)) / tot


def arb_avg_cost_on_buy(prev_avg: float, prev_qty: float,
                        price: float, qty: float) -> float:
    """做市买腿均价（arb_book 原口径，保留 max(prev_qty, 0) 的空头回补特性）：

    (旧均 × max(旧量, 0) + 新价 × 新量) / (旧量 + 新量)。

    旧量 > 0（净多建仓）时退化为加权均价；旧量 < 0（空头回补）时旧均贡献清零，
    与原实现 (prev_avg * max(inv, 0) + bid * size) / (inv + size) 逐字等价。
    """
    tot = float(prev_qty) + float(qty)
    if tot <= 0:
        return float(price)
    return (float(prev_avg) * max(float(prev_qty), 0.0)
            + float(price) * float(qty)) / tot


def portfolio_equity(cash: float, unrealized: float) -> float:
    """权益 = 现金 + 未实现盈亏（arb_book.view 口径）。"""
    return float(cash) + float(unrealized)

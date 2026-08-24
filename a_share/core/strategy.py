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


# ====================================================== 可插拔费用模型（Phase 5）
# 把「不同资产类别的撮合/费用规则」抽成可注册的 FillModel，
# 运行时按名取用（get_fill_model）。新增资产类别的费用规则只写一处并注册，
# 回测/模拟盘即可透明切换，消除散落的 if/else 与重复实现。
#
# FillModel 签名：fn(raw_price, qty, side, params) -> (fill_price, fee)
#   - fill_price：含滑点后的实际成交价（买多付、卖少收）
#   - fee：该笔总费用（含佣金/印花税/单边撮合费等，依规则而定）
#   - side：'buy' / 'sell'

_FILL_MODELS: dict = {}


def register_fill_model(name: str, fn) -> None:
    _FILL_MODELS[name] = fn


def get_fill_model(name: str):
    m = _FILL_MODELS.get(name)
    if m is None:
        raise KeyError(f"未知费用模型: {name}（可用: {list(_FILL_MODELS)}）")
    return m


def ashare_fill(raw_price: float, qty: float, side: str, params: dict):
    """A股费用模型：佣金万3(单边, 最低5元) + 卖出印花税千1 + 滑点0.1%。

    与 backtest_engine 原 buy/sell 内联公式逐字等价：
      buy : fill = price*(1+slip);  fee = max(fill*qty*commission, min_commission)
      sell: fill = price*(1-slip);  fee = max(fill*qty*commission, min_commission)
                               + fill*qty*stamp_duty
    """
    p = params or {}
    slip = p.get("slip", 0.001)
    commission = p.get("commission", 0.0003)
    min_commission = p.get("min_commission", 5.0)
    stamp = p.get("stamp_duty", 0.0005)
    if side == "buy":
        fill = float(raw_price) * (1.0 + slip)
        fee = max(fill * float(qty) * commission, min_commission)
    else:
        fill = float(raw_price) * (1.0 - slip)
        fee = (max(fill * float(qty) * commission, min_commission)
               + fill * float(qty) * stamp)
    return float(fill), float(fee)


def polymarket_fill(raw_price: float, qty: float, side: str, params: dict):
    """Polymarket 单腿撮合费：成交额 × 费率（arb_book 口径，与 leg_fee 等价）。"""
    p = params or {}
    fee_rate = p.get("fee_rate", 0.01)
    fill = float(raw_price)  # 预测市场按 mid 成交，无滑点
    fee = fill * float(qty) * fee_rate
    return fill, float(fee)


def crypto_fill(raw_price: float, qty: float, side: str, params: dict):
    """加密货币 maker/taker 费：默认 taker = 成交额 × 0.1%。"""
    p = params or {}
    taker = p.get("taker", 0.001)
    maker = p.get("maker", 0.0008)
    is_maker = bool(p.get("maker_only", False))
    rate = maker if is_maker else taker
    fill = float(raw_price)
    fee = fill * float(qty) * rate
    return fill, float(fee)


register_fill_model("ashare", ashare_fill)
register_fill_model("polymarket", polymarket_fill)
register_fill_model("crypto", crypto_fill)

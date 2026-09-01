"""P3-LP：LP 奖励半宽 δ 感知定价（#143）。

背景
----
Polymarket 对**挂在奖励区内**的限价单发放「持币时间加权流动性奖励」。
奖励区是围绕中间价 mid 的半宽 δ 区间 [mid-δ, mid+δ]；订单停留越久、名义本金越大，
奖励越多。当前 `scan_poly_marketmaking` 只按「吃现有 bid/ask 价差」算纯价差收益，
完全没吃这笔真实正向现金流——这是做市最被低估的收益源。

本模块把 δ 纳入报价**感知**（不碰真钱，纯模拟验证）：
- 给定市场 mid、观察价差 spread、奖励半宽 δ、奖励年化率 apr，
- 对比「纯价差」（吃现有流动性，edge = spread）与「奖励区内挂单」
  （edge = 2*min(half,δ) + apr*time_in_band_h/8760），
- 选出 blended edge 更大的策略，并给出建议 half_spread。

⚠️ 数据边界：δ 与 apr 是**假设值**（北京无外网，无法拉真实奖励参数）。
   NB 有网后用 `clob_exec` / `fetch_poly_quotes` 回填真实 δ 与真实奖励率，
   本模块逻辑不变，只换参数。
"""

import math

HOURS_PER_YEAR = 8760.0  # 365 * 24


def reward_band(mid, delta):
    """奖励区 [lo, hi] = [mid-δ, mid+δ]。"""
    return (round(mid - delta, 4), round(mid + delta, 4))


def in_band(price, mid, delta):
    """价格是否在奖励区内（含边界）。"""
    return (mid - delta) <= price <= (mid + delta)


def lp_reward_quote(mid, spread, delta, apr,
                    natural_half=None, time_in_band_h=24.0):
    """计算单市场「纯价差」vs「奖励区内挂单」的 blended edge，选出更优策略。

    参数
    ----
    mid            : 中间价 (bid+ask)/2
    spread         : 观察价差 ask-bid（纯价差模型的 edge）
    delta          : 奖励半宽 δ（假设值，NB 回填真实）
    apr           : 奖励年化率（假设值，NB 回填真实），如 0.20 = 20%/年
    natural_half   : 我们默认报价半宽；缺省取 spread/2（用市场一半价差做双边报价）
    time_in_band_h : 订单在奖励区内平均停留时长（小时），奖励按此时间加权

    返回 dict（供机会扫描层暴露 / 看板展示 / 回测对比）
    """
    if mid <= 0 or spread <= 0 or delta <= 0 or apr < 0:
        return {"ok": False, "msg": "参数非法(mid/spread/delta>0, apr>=0)"}
    if natural_half is None:
        natural_half = spread / 2.0
    natural_half = max(natural_half, 1e-6)

    # 纯价差：吃现有 bid/ask，edge = 锁定价差
    pure_edge = spread

    # 奖励区内挂单：把双边报价压到 half = min(自然半宽, δ)，保证在带内
    inband_half = min(natural_half, delta)
    inband_spread = 2.0 * inband_half                 # 区内挂单能锁定的价差
    inband_reward = apr * (time_in_band_h / HOURS_PER_YEAR)  # 区内停留奖励(名义本金占比)
    inband_edge = inband_spread + inband_reward

    chosen = "reward" if inband_edge > pure_edge else "spread"
    suggested_half = inband_half if chosen == "reward" else natural_half

    lo, hi = reward_band(mid, delta)
    return {
        "ok": True,
        "mid": round(mid, 4),
        "spread": round(spread, 4),
        "delta": round(delta, 4),
        "apr": round(apr, 4),
        "time_in_band_h": time_in_band_h,
        "reward_lo": lo,
        "reward_hi": hi,
        # 自然报价（不刻意进带）
        "natural_half": round(natural_half, 4),
        "natural_bid": round(mid - natural_half, 4),
        "natural_ask": round(mid + natural_half, 4),
        "natural_in_band": in_band(mid - natural_half, mid, delta)
                            and in_band(mid + natural_half, mid, delta),
        # 奖励区内报价
        "inband_half": round(inband_half, 4),
        "inband_bid": round(mid - inband_half, 4),
        "inband_ask": round(mid + inband_half, 4),
        "inband_in_band": True,
        # 收益对比
        "pure_edge": round(pure_edge, 6),
        "inband_spread_edge": round(inband_spread, 6),
        "inband_reward_edge": round(inband_reward, 6),
        "inband_edge": round(inband_edge, 6),
        "chosen": chosen,
        "suggested_half": round(suggested_half, 4),
        # 奖励对纯价差的增量（百分点，正=奖励区内更优）
        "lift": round((inband_edge - pure_edge) * 100.0, 4),
    }


def compare_over_quotes(quotes, delta, apr, top_n=None,
                        min_spread=0.002, time_in_band_h=24.0):
    """对一批市场汇总「纯价差」vs「价差+奖励」blended edge（模拟验证双收益）。

    返回 {n, pure_sum, reward_sum, lift_pct, per_market:[...]}
    per_market 仅含成功计算的（跳过无 bid/ask 的市场）。
    """
    per = []
    pure_sum = 0.0
    blended_sum = 0.0   # 感知择优后的 edge（reward 赢则用区内 edge，否则用纯价差）
    n = 0
    for q in quotes:
        if "error" in q:
            continue
        bid = q.get("yes_bid")
        ask = q.get("yes_ask")
        if not bid or not ask or bid <= 0 or ask <= 0 or ask <= bid:
            continue
        spread = round(ask - bid, 4)
        if spread < min_spread:
            continue
        mid = (bid + ask) / 2.0
        r = lp_reward_quote(mid, spread, delta, apr,
                            natural_half=spread / 2.0,
                            time_in_band_h=time_in_band_h)
        if not r.get("ok"):
            continue
        per.append(r)
        pure_sum += r["pure_edge"]
        # 感知择优：只在 reward 方案更优时计入奖励，否则退回纯价差
        edge_used = r["inband_edge"] if r["chosen"] == "reward" else r["pure_edge"]
        blended_sum += edge_used
        n += 1
        if top_n and len(per) >= top_n:
            break
    lift = (blended_sum - pure_sum) / pure_sum * 100.0 if pure_sum > 0 else 0.0
    return {
        "n": n,
        "pure_sum": round(pure_sum, 4),
        "blended_sum": round(blended_sum, 4),
        "reward_sum": round(blended_sum, 4),  # 兼容旧字段名
        "lift_pct": round(lift, 2),
        "per_market": per,
    }


def sweep(quotes, deltas, aprs, min_spread=0.002, time_in_band_h=24.0):
    """参数扫描：对 (δ, apr) 网格跑 compare_over_quotes，找最优组合。

    返回按 lift_pct 降序的列表 [ {delta, apr, n, pure_sum, reward_sum, lift_pct} ]
    """
    out = []
    for d in deltas:
        for a in aprs:
            c = compare_over_quotes(quotes, d, a,
                                    min_spread=min_spread,
                                    time_in_band_h=time_in_band_h)
            out.append({
                "delta": d, "apr": a,
                "n": c["n"],
                "pure_sum": c["pure_sum"],
                "reward_sum": c["reward_sum"],
                "lift_pct": c["lift_pct"],
            })
    out.sort(key=lambda x: x["lift_pct"], reverse=True)
    return out

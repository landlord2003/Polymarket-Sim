# -*- coding: utf-8 -*-
"""#143 LP 奖励半宽 δ 感知定价 —— 单元测试（8 项）。

运行：cd a_share && python -m unittest test_lp_reward -v
"""
import unittest

import lp_reward as LP


class TestLPReward(unittest.TestCase):
    def test_reward_band_basic(self):
        lo, hi = LP.reward_band(0.5, 0.01)
        self.assertAlmostEqual(lo, 0.49, places=4)
        self.assertAlmostEqual(hi, 0.51, places=4)

    def test_in_band(self):
        self.assertTrue(LP.in_band(0.50, 0.5, 0.01))
        self.assertTrue(LP.in_band(0.49, 0.5, 0.01))   # 边界在带内
        self.assertFalse(LP.in_band(0.52, 0.5, 0.01))  # 超出

    def test_pure_beats_reward_wide_spread(self):
        # 价差大到纯价差远胜区内奖励 → 选 spread
        r = LP.lp_reward_quote(mid=0.5, spread=0.10, delta=0.01,
                               apr=0.20, natural_half=0.05, time_in_band_h=24.0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["chosen"], "spread")
        self.assertLess(r["inband_edge"], r["pure_edge"])

    def test_reward_beats_pure_thin_spread(self):
        # 薄价差 + 区内挂单吃奖励 → 选 reward，lift 为正
        r = LP.lp_reward_quote(mid=0.5, spread=0.004, delta=0.01,
                               apr=0.20, natural_half=0.002, time_in_band_h=24.0)
        self.assertEqual(r["chosen"], "reward")
        self.assertGreater(r["lift"], 0.0)
        # 自然半宽已 <= δ，本就在带内，建议保持自然半宽
        self.assertAlmostEqual(r["suggested_half"], r["natural_half"], places=6)

    def test_high_apr_makes_reward_win(self):
        # 高奖励年化率把「区内」推赢纯价差
        r = LP.lp_reward_quote(mid=0.5, spread=0.01, delta=0.01,
                               apr=5.0, natural_half=0.005, time_in_band_h=24.0)
        self.assertEqual(r["chosen"], "reward")
        self.assertGreater(r["inband_reward_edge"], 0.0)

    def test_invalid_params(self):
        r = LP.lp_reward_quote(mid=0.0, spread=0.01, delta=0.01, apr=0.20)
        self.assertFalse(r["ok"])
        r2 = LP.lp_reward_quote(mid=0.5, spread=-0.01, delta=0.01, apr=0.20)
        self.assertFalse(r2["ok"])

    def test_compare_over_quotes(self):
        quotes = [
            {"yes_bid": 0.49, "yes_ask": 0.51, "id": "A",
             "question": "q1", "liquidity": 5000.0},
            {"yes_bid": 0.30, "yes_ask": 0.32, "id": "B",
             "question": "q2", "liquidity": 8000.0},
            {"yes_bid": 0.10, "yes_ask": 0.90, "id": "C",
             "question": "q3", "liquidity": 100.0},  # 价差过大，仍计入
        ]
        c = LP.compare_over_quotes(quotes, delta=0.01, apr=0.20,
                                   min_spread=0.002, time_in_band_h=24.0)
        self.assertEqual(c["n"], 3)
        self.assertGreater(c["reward_sum"], 0.0)
        self.assertIn("lift_pct", c)

    def test_sweep_returns_sorted(self):
        quotes = [
            {"yes_bid": 0.49, "yes_ask": 0.51, "id": "A",
             "question": "q1", "liquidity": 5000.0},
            {"yes_bid": 0.30, "yes_ask": 0.32, "id": "B",
             "question": "q2", "liquidity": 8000.0},
        ]
        s = LP.sweep(quotes, deltas=[0.005, 0.01, 0.02],
                     aprs=[0.10, 0.50, 2.0], min_spread=0.002)
        self.assertEqual(len(s), 9)
        # 降序：第一项 lift_pct >= 最后一项
        self.assertGreaterEqual(s[0]["lift_pct"], s[-1]["lift_pct"])


if __name__ == "__main__":
    unittest.main()

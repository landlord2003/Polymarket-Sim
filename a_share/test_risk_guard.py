# -*- coding: utf-8 -*-
"""组合级风控熔断回归测试：日亏 / 回撤 / 本金下限 自动 kill switch + 看板 guards 快照。

不依赖运行中服务；直接单测 risk_control.evaluate_portfolio_guard / guard_view / status。
跑法：cd a_share && python -m unittest test_risk_guard -v
"""
import unittest

import risk_control as RC


class TestRiskGuard(unittest.TestCase):
    def setUp(self):
        # 测试隔离：不落盘、用确定阈值、清 kill + 日亏累计、屏蔽钉钉推送（避免测试触发真实 webhook 卡网络）
        RC.RISK_PERSIST = False
        RC.DAILY_LOSS_LIMIT = 100.0
        RC.DRAWDOWN_LIMIT = 0.15
        RC.BANKROLL_FLOOR_FRAC = 0.70
        RC._ding = None
        RC.reset_kill_switch()
        RC._day_pnl = 0.0

    def test_all_clear(self):
        g = RC.evaluate_portfolio_guard(equity=285000, peak=285000, cash=5000, initial_capital=5000)
        self.assertFalse(g["guarded"])
        self.assertFalse(g["daily_loss_breach"])
        self.assertFalse(g["drawdown_breach"])
        self.assertFalse(g["bankroll_breach"])
        self.assertEqual(g["dd_pct"], 0.0)
        self.assertGreater(g["bankroll_pct"], 100.0)

    def test_daily_loss_breach(self):
        RC._day_pnl = -200.0  # 当日亏损超过 100 限额
        g = RC.evaluate_portfolio_guard(equity=285000, peak=285000, cash=5000, initial_capital=5000)
        self.assertTrue(g["guarded"])
        self.assertEqual(g["reason"], "daily_loss")
        self.assertTrue(RC.status()["kill_switch"]["on"])

    def test_drawdown_breach(self):
        # equity 200k vs peak 285k -> 回撤 ~29.8% > 15%
        g = RC.evaluate_portfolio_guard(equity=200000, peak=285000, cash=5000, initial_capital=5000)
        self.assertTrue(g["guarded"])
        self.assertEqual(g["reason"], "drawdown")
        self.assertGreaterEqual(g["dd_pct"], 15.0)

    def test_bankroll_breach(self):
        # 隔离本金下限：peak=3200 -> 回撤仅 6.25%（不触 15% 回撤线），
        # equity=3000 < 5000*0.70=3500 -> 单独触本金下限。
        g = RC.evaluate_portfolio_guard(equity=3000, peak=3200, cash=5000, initial_capital=5000)
        self.assertTrue(g["guarded"])
        self.assertEqual(g["reason"], "bankroll_floor")
        self.assertLess(g["bankroll_pct"], 70.0)
        self.assertFalse(g["drawdown_breach"])

    def test_reset_clears(self):
        RC._day_pnl = -200.0
        RC.evaluate_portfolio_guard(equity=285000, peak=285000, cash=5000, initial_capital=5000)
        self.assertTrue(RC.status()["kill_switch"]["on"])
        RC.reset_kill_switch()
        self.assertFalse(RC.status()["kill_switch"]["on"])

    def test_idempotent_and_recover(self):
        RC._day_pnl = -200.0
        g1 = RC.evaluate_portfolio_guard(equity=285000, peak=285000, cash=5000, initial_capital=5000)
        self.assertTrue(g1["guarded"])
        # 复位 kill 并把日亏归零后，再次评估应恢复安全
        RC.reset_kill_switch()
        RC._day_pnl = 0.0
        g2 = RC.evaluate_portfolio_guard(equity=285000, peak=285000, cash=5000, initial_capital=5000)
        self.assertFalse(g2["guarded"])

    def test_guard_view_exposes_limits(self):
        RC.evaluate_portfolio_guard(equity=285000, peak=285000, cash=5000, initial_capital=5000)
        v = RC.guard_view()
        self.assertEqual(v["drawdown_limit_pct"], 15.0)
        self.assertEqual(v["bankroll_floor_pct"], 70.0)
        self.assertEqual(v["daily_loss_limit"], 100.0)


if __name__ == "__main__":
    unittest.main()

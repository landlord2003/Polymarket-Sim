# -*- coding: utf-8 -*-
"""P1-B 测试：成交 CSV / 报告 范围筛选逻辑（filter_trades 时间区间 + limit，
parse_time_to_ts 解析，build_html/build_md 范围横幅渲染）。

覆盖 2026-09-01 新增的「导出范围」能力（全部/最近N笔/时间区间/日期/轮次）。
纯函数 + 渲染测试，不依赖运行中的服务、不触发 sim_server 启动。
"""
import datetime
import unittest

from sim_report import filter_trades, parse_time_to_ts, build_html, build_md


def _mk_trades(n, start_ts):
    """造 n 笔成交，ts 从 start_ts 起每笔 +60s，round 递增。"""
    out = []
    for i in range(n):
        out.append({
            "ts": start_ts + i * 60.0,
            "round": 1000 + i,
            "mkt": "market-%d" % i,
            "tag": "economy" if i % 2 == 0 else "crypto",
            "side": "buy" if i % 2 == 0 else "sell",
            "entry": 0.5,
            "size": 100,
            "pnl": 1.0 * i,
            "slip": 0.01,
            "cash_after": 1000.0 + i,
            "q": "market-%d" % i,
        })
    return out


class TestParseTimeToTs(unittest.TestCase):
    def test_datetime_local(self):
        ts = parse_time_to_ts("2026-09-01T21:00")
        self.assertIsNotNone(ts)
        self.assertAlmostEqual(ts, 1788267600.0, places=0)

    def test_date_only(self):
        ts = parse_time_to_ts("2026-09-01")
        self.assertIsNotNone(ts)
        # 该日期 00:00 本地
        self.assertAlmostEqual(ts, 1788192000.0, places=0)

    def test_with_seconds(self):
        ts = parse_time_to_ts("2026-09-01 21:05:30")
        self.assertIsNotNone(ts)

    def test_bad_input(self):
        self.assertIsNone(parse_time_to_ts(""))
        self.assertIsNone(parse_time_to_ts(None))
        self.assertIsNone(parse_time_to_ts("not-a-time"))


class TestFilterTrades(unittest.TestCase):
    def setUp(self):
        # 100 笔，覆盖 ~100 分钟；ts 范围 1788267600(21:00) .. +99*60
        self.trades = _mk_trades(100, 1788267600.0)

    def test_all_when_no_filter(self):
        self.assertEqual(len(filter_trades(self.trades)), 100)

    def test_time_range(self):
        # 21:00 ~ 21:10 = ts 1788267600 .. 1788268200
        out = filter_trades(self.trades, since_ts=1788267600.0, until_ts=1788268200.0)
        self.assertGreater(len(out), 0)
        for t in out:
            self.assertGreaterEqual(t["ts"], 1788267600.0)
            self.assertLessEqual(t["ts"], 1788268200.0)
        # 21:00 起每 60s 一笔，10 分钟窗口含 11 笔（含端点）
        self.assertEqual(len(out), 11)

    def test_limit_takes_last_n(self):
        out = filter_trades(self.trades, limit=5)
        self.assertEqual(len(out), 5)
        # 最近 N 笔 = 末尾 N 笔（ts 最大）
        self.assertEqual(out[-1]["ts"], self.trades[-1]["ts"])
        self.assertEqual(out[0]["ts"], self.trades[-5]["ts"])

    def test_time_range_then_limit(self):
        # 先时间区间（11 笔），再取最近 3 笔
        out = filter_trades(self.trades, since_ts=1788267600.0,
                            until_ts=1788268200.0, limit=3)
        self.assertEqual(len(out), 3)
        # 这 3 笔应是该窗口内最后 3 笔（ts 最大）
        self.assertEqual(out[-1]["ts"], 1788268200.0)

    def test_round_filter(self):
        out = filter_trades(self.trades, since_round=1050)
        self.assertEqual(len(out), 50)  # round 1050..1099

    def test_combined_round_and_date(self):
        # 用 _date_of 逻辑：date 过滤基于 ts 本地日期
        d = datetime.datetime.fromtimestamp(self.trades[0]["ts"]).strftime("%Y-%m-%d")
        out = filter_trades(self.trades, date=d)
        self.assertEqual(len(out), 100)  # 全部同一天


class TestScopeNoteRender(unittest.TestCase):
    def _st(self):
        return {
            "fill": {"on": False, "rate": 100.0, "attempts": 0,
                     "hits": 0, "base": 0.6},
            "mode": "inv", "equity": 130000.0, "cash": 128000.0,
            "unrealized": 2000.0, "quotes": {}, "positions": [],
            "n_markets": 20, "params": {"adverse": 0.15},
        }

    def _s(self):
        return {
            "realized": 100.0, "adverse_sel_loss": 2.0, "settled_pnl": 10.0,
            "settlement_exposure": 0.0, "unrealized": 2000.0,
            "peak_profit": 50.0, "max_drawdown_pct": 1.0, "win_rate": 60.0,
            "trades_total": 100, "buys": 50, "sells": 50, "win": 30,
            "avg_pnl": 1.0, "trades_per_hour": 30.0,
            "best_trade": {"pnl": 9.0, "mkt": "m"}, "worst_trade": {"pnl": -3.0, "mkt": "w"},
            "run_start": "2026-09-01 20:00", "duration_min": 90,
            "per_tag": {}, "per_day": {}, "attribution": {},
            "initial_equity": 5000.0, "drawdown_pct": 1.0, "peak": 131000.0,
            "n_markets": 20,
        }

    def test_html_scope_banner_embedded(self):
        trades = _mk_trades(10, 1788267600.0)
        html = build_html(self._st(), self._s(), {"markets": []}, 15,
                          "2026-09-01 21:30", trades=trades,
                          tag_trades=trades, scope_note="最近 5 笔（命中 5 / 全量 100 笔）")
        self.assertIn("最近 5 笔（命中 5 / 全量 100 笔）", html)
        # 明细表头应存在（最近成交明细）
        self.assertIn("最近成交明细", html)

    def test_md_scope_banner_embedded(self):
        trades = _mk_trades(10, 1788267600.0)
        md = build_md(self._st(), self._s(), {"markets": []}, 15,
                      "2026-09-01 21:30", trades=trades,
                      tag_trades=trades, scope_note="时间区间 21:00~21:10")
        self.assertIn("时间区间 21:00~21:10", md)

    def test_no_scope_note_when_empty(self):
        trades = _mk_trades(10, 1788267600.0)
        html = build_html(self._st(), self._s(), {"markets": []}, 15,
                          "2026-09-01 21:30", trades=trades, tag_trades=trades)
        self.assertNotIn("导出范围", html)


if __name__ == "__main__":
    unittest.main()

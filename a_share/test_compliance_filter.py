# -*- coding: utf-8 -*-
"""P1-B 测试：合规红线过滤（政治/地缘/军事敏感，体育对抗国家名放行）。"""
import unittest

from compliance import is_blocked, filter_markets


class TestComplianceFilter(unittest.TestCase):
    def test_blocked_political(self):
        self.assertTrue(is_blocked("Will Iran invade Ukraine?"))
        self.assertTrue(is_blocked("US Presidential election 2028 winner"))
        self.assertTrue(is_blocked("Will Russia use nuclear weapons?"))
        self.assertTrue(is_blocked("Hormuz strait closure impact"))

    def test_pass_benign(self):
        self.assertFalse(is_blocked("Will it rain in London tomorrow?"))
        self.assertFalse(is_blocked("Will Bitcoin exceed 100k?"))
        # 体育对抗赛（A vs B）：国家名不敏感，只屏蔽真正政治/军事词
        self.assertFalse(is_blocked("Lakers vs Celtics game winner"))

    def test_filter_markets(self):
        ms = [{"question": "A", "tag": "crypto"},
              {"question": "Iran war outcome", "tag": "geo"}]
        out = filter_markets(ms)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["question"], "A")


if __name__ == "__main__":
    unittest.main()

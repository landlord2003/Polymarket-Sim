# -*- coding: utf-8 -*-
"""P1-B 测试：盈亏归因瀑布恒等式（gross = net - settled + fees + slip + asel）。"""
import os
import tempfile
import unittest

from sim_rigor import RigorVirtualBook


class TestAttributionIdentity(unittest.TestCase):
    def test_identity_closure(self):
        tf = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        tf.close()
        os.remove(tf.name)
        try:
            book = RigorVirtualBook(path=tf.name, bankroll=10000.0)
            book.realized_pnl = 100.0
            book.fees_paid = 5.0
            book.slippage_paid = 3.0
            book.adverse_sel_loss = 2.0
            book.settled_pnl = 10.0
            a = book.pnl_attribution(100.0)
            gross = a["gross_spread"]
            slip = -a["walk_the_book"]
            fees = -a["fees"]
            asel = -a["adverse_selection"]
            settled = a["settlement"]
            net = a["net"]
            # 恒等式：gross = net - settled + fees + slip + asel
            self.assertAlmostEqual(gross, net - settled + fees + slip + asel, places=2)
            # 瀑布闭合：gross + walk + fees_attrib + adverse + settlement = net
            self.assertAlmostEqual(
                gross + a["walk_the_book"] + a["fees"] + a["adverse_selection"]
                + a["settlement"], net, places=2)
            self.assertAlmostEqual(net, 100.0, places=2)
        finally:
            if os.path.exists(tf.name):
                os.remove(tf.name)


if __name__ == "__main__":
    unittest.main()

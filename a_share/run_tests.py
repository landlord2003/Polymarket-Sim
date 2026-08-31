# -*- coding: utf-8 -*-
"""P1-B 统一测试入口：发现并运行 a_share/ 下所有 test_*.py。

用法：
  cd a_share && python run_tests.py
或
  cd a_share && python -m unittest discover -p "test_*.py" -v
"""
import os
import sys
import unittest

if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    loader = unittest.TestLoader()
    suite = loader.discover(here, pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)

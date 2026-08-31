# -*- coding: utf-8 -*-
"""P1-B 测试：单实例锁进程判活 + 加锁/释放回合（P2-1）。"""
import os
import tempfile
import unittest

import sim_server as S


class TestLockAlive(unittest.TestCase):
    def test_pid_alive_self(self):
        self.assertTrue(S._pid_alive(os.getpid()))

    def test_pid_alive_bogus(self):
        # 一个极不可能存活的 pid
        self.assertFalse(S._pid_alive(2 ** 30))

    def test_acquire_release_roundtrip(self):
        tf = tempfile.NamedTemporaryFile(delete=False, suffix=".pid")
        tf.close()
        prev = S.PID_FILE
        try:
            S.PID_FILE = tf.name
            if os.path.exists(tf.name):
                os.remove(tf.name)
            S._acquire_lock()
            self.assertTrue(os.path.exists(tf.name))
            S._release_lock()
            self.assertFalse(os.path.exists(tf.name))
        finally:
            S.PID_FILE = prev
            if os.path.exists(tf.name):
                os.remove(tf.name)


if __name__ == "__main__":
    unittest.main()

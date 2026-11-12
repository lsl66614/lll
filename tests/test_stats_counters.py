"""Stats 计数器：漏声明的字段不能让一次答题直接 500。"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="ocs-stats-test-")
os.environ.setdefault("OCS_DATA_DIR", _TEST_DATA_DIR)
os.environ.setdefault("BANK_DB_PATH", "")
atexit.register(shutil.rmtree, _TEST_DATA_DIR, True)

from app.main import Stats  # noqa: E402


class StatsCounterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "stats.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_title_filled_is_declared(self) -> None:
        # 题干补全命中会 bump 这个计数器；未声明时曾整题 AttributeError。
        stats = Stats(self.path)
        stats.bump("title_filled")
        self.assertEqual(stats.title_filled, 1)

    def test_unknown_counter_does_not_raise(self) -> None:
        stats = Stats(self.path)
        stats.bump("never_declared_counter")
        self.assertEqual(stats.never_declared_counter, 1)
        stats.bump("never_declared_counter", 2)
        self.assertEqual(stats.never_declared_counter, 3)

    def test_title_filled_persists(self) -> None:
        stats = Stats(self.path)
        stats.bump("title_filled")
        stats.bump("title_filled")
        self.assertEqual(Stats(self.path).title_filled, 2)


if __name__ == "__main__":
    unittest.main()

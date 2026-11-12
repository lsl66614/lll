"""慢模型冷却：连续超时的模型要暂时退出视觉题池。"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="ocs-cooldown-test-")
os.environ.setdefault("OCS_DATA_DIR", _TEST_DATA_DIR)
os.environ.setdefault("BANK_DB_PATH", "")
atexit.register(shutil.rmtree, _TEST_DATA_DIR, True)

from app import main  # noqa: E402


def _vision(pid: str) -> dict:
    return {"id": pid, "name": pid.upper(), "vision": "yes"}


class TimeoutCooldownTests(unittest.TestCase):
    def setUp(self) -> None:
        main._recent_timeouts.clear()

    def tearDown(self) -> None:
        main._recent_timeouts.clear()

    def test_single_timeout_keeps_provider(self) -> None:
        main._mark_timeout("a")
        self.assertFalse(main._in_timeout_cooldown("a"))
        self.assertEqual([p["id"] for p in main._pick_vision([_vision("a")])], ["a"])

    def test_two_timeouts_drop_from_vision_pool(self) -> None:
        main._mark_timeout("a")
        main._mark_timeout("a")
        picked = main._pick_vision([_vision("a"), _vision("b")])
        self.assertEqual([p["id"] for p in picked], ["b"])

    def test_all_cooling_falls_back_to_pool(self) -> None:
        main._mark_timeout("a")
        main._mark_timeout("a")
        picked = main._pick_vision([_vision("a")])
        self.assertEqual([p["id"] for p in picked], ["a"])

    def test_cooldown_expires_after_window(self) -> None:
        with mock.patch.object(main.time, "time", return_value=1000.0):
            main._mark_timeout("a")
            main._mark_timeout("a")
            self.assertTrue(main._in_timeout_cooldown("a"))
        with mock.patch.object(main.time, "time", return_value=1000.0 + 601):
            self.assertFalse(main._in_timeout_cooldown("a"))


if __name__ == "__main__":
    unittest.main()

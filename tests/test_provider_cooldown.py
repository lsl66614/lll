"""慢模型冷却：连续超时的模型要暂时退出视觉题池。"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="ocs-cooldown-test-")
os.environ.setdefault("OCS_DATA_DIR", _TEST_DATA_DIR)
os.environ.setdefault("BANK_DB_PATH", "")
atexit.register(shutil.rmtree, _TEST_DATA_DIR, True)

from app import config, main  # noqa: E402


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


class SamplingBudgetTests(unittest.TestCase):
    def test_fresh_request_allows_sampling(self) -> None:
        self.assertTrue(main._sampling_allowed(time.perf_counter(), {}))

    def test_over_budget_skips_sampling(self) -> None:
        self.assertFalse(main._sampling_allowed(0.0, {}))

    def test_custom_budget_is_respected(self) -> None:
        now = time.perf_counter()
        self.assertTrue(main._sampling_allowed(now - 5, {"sample_budget_seconds": 10}))
        self.assertFalse(main._sampling_allowed(now - 15, {"sample_budget_seconds": 10}))


class CallBudgetTests(unittest.TestCase):
    def _provider(self, timeout: int = 60) -> dict:
        return {"id": "p", "name": "P", "timeout": timeout}

    def test_text_question_uses_request_budget(self) -> None:
        messages = [{"role": "user", "content": "题目"}]
        self.assertEqual(main._call_budget(self._provider(), messages),
                         float(config.REQUEST_TIMEOUT_SECONDS))

    def test_vision_question_uses_shorter_budget(self) -> None:
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "题目"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        ]}]
        self.assertEqual(main._call_budget(self._provider(), messages),
                         float(config.VISION_TIMEOUT_SECONDS))
        self.assertLess(config.VISION_TIMEOUT_SECONDS, config.REQUEST_TIMEOUT_SECONDS)

    def test_provider_timeout_lower_wins(self) -> None:
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        ]}]
        self.assertEqual(main._call_budget(self._provider(timeout=12), messages), 12.0)


if __name__ == "__main__":
    unittest.main()

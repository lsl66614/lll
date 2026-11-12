"""后台自动重做失败题：退避、计数上限、成功销账。"""

from __future__ import annotations

import atexit
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="ocs-auto-retry-test-")
os.environ.setdefault("OCS_DATA_DIR", _TEST_DATA_DIR)
os.environ.setdefault("BANK_DB_PATH", "")
atexit.register(shutil.rmtree, _TEST_DATA_DIR, True)

from app import config, main  # noqa: E402
from app.failures import FailureLog  # noqa: E402


def _failure(title: str = "求极限 lim x->0 sin(x)/x", **extra) -> dict:
    item = {
        "at": 1000.0,
        "code": 50001,
        "type": "single",
        "title": title,
        "options": ["A. 0", "B. 1", "C. 2", "D. 不存在"],
        "message": "模型都失败了",
    }
    item.update(extra)
    return item


class AutoRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._old_failures = main.failures
        main.failures = FailureLog(os.path.join(self.tmp.name, "failures.json"))

    def tearDown(self) -> None:
        main.failures = self._old_failures
        self.tmp.cleanup()

    def test_success_resolves_record(self) -> None:
        main.failures.add(_failure())

        async def fake(item_dict):
            main.failures.resolve(item_dict["title"], item_dict["options"])
            return True

        with mock.patch.object(main, "_retry_item_once", side_effect=fake):
            result = asyncio.run(main._auto_retry_once(now=1200.0))
        self.assertEqual(result, {"tried": 1, "ok": 1, "failed": 0})
        self.assertEqual(main.failures.all(), [])

    def test_failure_counts_and_backs_off(self) -> None:
        main.failures.add(_failure())
        with mock.patch.object(main, "_retry_item_once", return_value=False):
            with mock.patch.object(main.time, "time", return_value=1200.0):
                result = asyncio.run(main._auto_retry_once(now=1200.0))
        self.assertEqual(result, {"tried": 1, "ok": 0, "failed": 1})
        item = main.failures.all()[0]
        self.assertEqual(item["retry_count"], 1)
        self.assertEqual(item["retry_at"], 1200.0 + 60)

    def test_retry_count_survives_at_rewrite(self) -> None:
        """重试失败会重新 add 同一条记录（at 变新），计数不能被冲掉。"""
        main.failures.add(_failure())
        item = main.failures.all()[0]
        main.failures.update_by_key(item, retry_count=1, retry_at=2000.0)
        # 模拟 _record_failure 在重试失败后又写了一次同题记录。
        main.failures.add(_failure(at=1300.0))
        merged = main.failures.all()[0]
        self.assertEqual(merged["retry_count"], 1)
        self.assertEqual(merged["retry_at"], 2000.0)
        self.assertEqual(merged["at"], 1300.0)

    def test_max_attempts_stops_retrying(self) -> None:
        main.failures.add(_failure(retry_count=3, retry_at=1000.0))
        with mock.patch.object(main, "_retry_item_once", return_value=True) as fake:
            result = asyncio.run(main._auto_retry_once(now=99999.0))
        self.assertEqual(result["tried"], 0)
        fake.assert_not_called()

    def test_non_retryable_is_skipped(self) -> None:
        main.failures.add({
            "at": 1000.0, "code": 40002, "title": "判断题", "options": [],
            "message": "暂不支持该题型：judge",
        })
        with mock.patch.object(main, "_retry_item_once", return_value=True) as fake:
            result = asyncio.run(main._auto_retry_once(now=99999.0))
        self.assertEqual(result["tried"], 0)
        fake.assert_not_called()

    def test_fresh_failure_waits_one_backoff(self) -> None:
        main.failures.add(_failure(at=1000.0))
        with mock.patch.object(main, "_retry_item_once", return_value=True) as fake:
            self.assertEqual(asyncio.run(main._auto_retry_once(now=1010.0))["tried"], 0)
            self.assertEqual(asyncio.run(main._auto_retry_once(now=1060.0))["tried"], 1)
        fake.assert_called_once()

    def test_interval_and_enabled_config(self) -> None:
        self.assertGreaterEqual(config.AUTO_RETRY_INTERVAL_SECONDS, 30)
        self.assertGreaterEqual(config.AUTO_RETRY_MAX_ATTEMPTS, 1)
        self.assertEqual(len(config.AUTO_RETRY_BACKOFFS), 3)


if __name__ == "__main__":
    unittest.main()

"""失败记录补存图片地址 + 指定模型重做。"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="ocs-retry-test-")
os.environ["OCS_DATA_DIR"] = _TEST_DATA_DIR
os.environ["BANK_DB_PATH"] = ""
atexit.register(shutil.rmtree, _TEST_DATA_DIR, True)

from fastapi.testclient import TestClient  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from app import main  # noqa: E402
from app.failures import FailureLog  # noqa: E402


def _provider(pid: str, name: str) -> dict:
    return {
        "id": pid,
        "name": name,
        "base_url": "https://example.com/v1",
        "api_key": "sk-test",
        "model": "test-model",
        "enabled": True,
        "timeout": 30,
        "api_type": "chat_completions",
        "temperature": 0.2,
        "vision": "auto",
    }


class FailureLogImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log = FailureLog(os.path.join(self.tmp.name, "failures.json"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_image_question_retryable_only_with_urls(self) -> None:
        self.log.add({
            "code": 50001,
            "title": "设随机变量 X 服从正态分布",
            "options": ["A", "B"],
            "images": 1,
        })
        self.assertFalse(self.log.all()[0]["retryable"])

        self.log.add({
            "code": 50001,
            "title": "设随机变量 X 服从正态分布",
            "options": ["A", "B"],
            "images": 1,
            "image_urls": ["http://mooc-image.nosdn.127.net/a.png"],
        })
        item = self.log.all()[0]
        self.assertTrue(item["retryable"])
        self.assertEqual(item["image_urls"],
                         ["http://mooc-image.nosdn.127.net/a.png"])

    def test_text_question_keeps_retryable(self) -> None:
        self.log.add({"code": 50001, "title": "纯文字题", "options": ["A", "B"]})
        self.assertTrue(self.log.all()[0]["retryable"])


class RetryEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._old_failures = main.failures
        main.failures = FailureLog(os.path.join(self.tmp.name, "failures.json"))
        self.providers = [_provider("p1", "模型一"), _provider("p2", "模型二")]
        self.seen: list[dict] = []

        async def fake_resolve(title, qtype, options, images=None, subject=None, **kwargs):
            self.seen.append({
                "title": title,
                "images": list(images or []),
                "forced": main._forced_provider_ids.get(),
            })
            return JSONResponse({"code": 0, "data": {"answer": "A", "match": "ai"}})

        self._patches = [
            mock.patch.object(main.store, "enabled", return_value=self.providers),
            mock.patch.object(main, "_resolve_core", mock.AsyncMock(side_effect=fake_resolve)),
        ]
        for patch in self._patches:
            patch.start()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        for patch in self._patches:
            patch.stop()
        main.failures = self._old_failures
        self.tmp.cleanup()

    def _retry(self, **body):
        return self.client.post("/api/failures/retry", json=body).json()

    def test_retry_respects_selected_provider(self) -> None:
        result = self._retry(
            title="指定模型重做题一", type="single", options=["甲", "乙"],
            providers=["p2"],
        )
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["answer"], "A")
        self.assertEqual(self.seen[-1]["forced"], ("p2",))

    def test_retry_without_choice_leaves_pool_open(self) -> None:
        result = self._retry(title="不限定模型题", type="single", options=["甲", "乙"])
        self.assertEqual(result["code"], 0)
        self.assertIsNone(self.seen[-1]["forced"])

    def test_retry_rejects_unknown_providers(self) -> None:
        result = self._retry(
            title="不存在的模型题", type="single", options=["甲", "乙"],
            providers=["missing"],
        )
        self.assertEqual(result["code"], 50002)
        self.assertEqual(self.seen, [])

    def test_retry_resolves_failure_on_success(self) -> None:
        main.failures.add({
            "code": 50001, "title": "失败过的题", "options": ["甲", "乙"],
            "type": "single",
        })
        result = self._retry(title="失败过的题", type="single", options=["甲", "乙"])
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["resolved"], 1)
        self.assertEqual(main.failures.all(), [])

    def test_retry_reuses_stored_image_urls(self) -> None:
        main.failures.add({
            "code": 50001, "title": "带图失败题", "options": ["甲", "乙"],
            "type": "single", "images": 1,
            "image_urls": ["http://mooc-image.nosdn.127.net/x.png"],
        })
        with mock.patch.object(main, "fetch_inline_images",
                               mock.AsyncMock(return_value=["data:image/png;base64,AAAA"])) as fetch:
            result = self._retry(title="带图失败题", type="single", options=["甲", "乙"])
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["images"], 1)
        fetch.assert_awaited_once_with(["http://mooc-image.nosdn.127.net/x.png"])
        self.assertEqual(self.seen[-1]["images"], ["data:image/png;base64,AAAA"])


class ProviderWhitelistTests(unittest.TestCase):
    def test_forced_ids_filter_after_routing(self) -> None:
        providers = [_provider("p1", "模型一"), _provider("p2", "模型二")]
        with mock.patch.object(
            main, "route_providers",
            side_effect=lambda enabled, routes, qtype, images: list(enabled),
        ):
            token = main._forced_provider_ids.set(("p2",))
            try:
                picked = main._route_providers(providers, {}, "single", False)
            finally:
                main._forced_provider_ids.reset(token)
        self.assertEqual([p["id"] for p in picked], ["p2"])

    def test_no_forced_ids_keeps_routing_result(self) -> None:
        providers = [_provider("p1", "模型一"), _provider("p2", "模型二")]
        with mock.patch.object(
            main, "route_providers",
            side_effect=lambda enabled, routes, qtype, images: list(enabled),
        ):
            picked = main._route_providers(providers, {}, "single", False)
        self.assertEqual([p["id"] for p in picked], ["p1", "p2"])


if __name__ == "__main__":
    unittest.main()

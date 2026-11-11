"""`POST /api/batch/query` 的接口测试。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.answer_memory import AnswerMemory  # noqa: E402
from app.bank import Bank  # noqa: E402
from app.cache import AnswerCache  # noqa: E402
from app.failures import FailureLog  # noqa: E402
from app.scene_state import SceneState  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.store import ProviderStore  # noqa: E402
from app.subjects import SubjectRules  # noqa: E402


def _item(qid="q1", qtype="single", options=None, title="2+2=?"):
    return {
        "id": qid,
        "type": qtype,
        "title": title,
        "options": options if options is not None else ["3", "4"],
    }


class BatchApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        main.store = ProviderStore(os.path.join(self.tmp.name, "providers.json"))
        main.cache = AnswerCache(os.path.join(self.tmp.name, "answers.db"), 168)
        main.bank = Bank(os.path.join(self.tmp.name, "bank.json"))
        main.answer_memory = AnswerMemory(os.path.join(self.tmp.name, "answer_memory.db"))
        main.settings = Settings(os.path.join(self.tmp.name, "settings.json"))
        main.stats = main.Stats(os.path.join(self.tmp.name, "stats.json"))
        main.failures = FailureLog(os.path.join(self.tmp.name, "failures.json"))
        main.scene_state = SceneState(os.path.join(self.tmp.name, "scene.json"))
        main.subject_rules = SubjectRules(os.path.join(self.tmp.name, "subjects.json"))
        main._recent_failures.clear()
        main._batch_hits.clear()
        main.store.add({
            "name": "主 AI",
            "base_url": "https://example.com/v1",
            "api_key": "sk-test-1234",
            "model": "test-model",
        })
        self.old_enabled = main.config.BATCH_ENABLED
        main.config.BATCH_ENABLED = True
        self.client = TestClient(main.app)

    def tearDown(self):
        main.config.BATCH_ENABLED = self.old_enabled
        main._batch_hits.clear()
        main.cache.close()
        try:
            main.answer_memory.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            main.bank.close()
        except Exception:  # noqa: BLE001
            pass
        self.tmp.cleanup()

    def _cheap_miss(self):
        """题库/记忆/缓存全部未命中。"""
        return (
            mock.patch.object(main.bank, "find_exact", return_value=None),
            mock.patch.object(main.answer_memory, "recall", return_value=None),
            mock.patch.object(
                main.cache, "lookup_for_answer", return_value=(None, False)
            ),
        )

    def test_disabled_returns_404(self):
        main.config.BATCH_ENABLED = False
        main.settings.update({"batch_enabled": False})
        response = self.client.post("/api/batch/query", json={"items": [_item()]})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], 40401)

    def test_token_required(self):
        old = main.config.GATEWAY_TOKEN
        main.config.GATEWAY_TOKEN = "secret-test"
        try:
            with mock.patch.object(main, "_batch_cheap_hit", return_value={
                "id": "q1", "answer": "A", "confidence": None, "source": "cache",
                "provider": "m", "cached": True, "elapsed_ms": 0, "error": "",
            }):
                denied = self.client.post("/api/batch/query", json={"items": [_item()]})
                allowed = self.client.post(
                    "/api/batch/query",
                    json={"items": [_item()]},
                    headers={"X-Gateway-Token": "secret-test"},
                )
        finally:
            main.config.GATEWAY_TOKEN = old
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(allowed.status_code, 200)

    def test_empty_items(self):
        response = self.client.post("/api/batch/query", json={"items": []})
        self.assertEqual(response.status_code, 400)
        self.assertIn("items", response.json()["msg"])

    def test_duplicate_id(self):
        response = self.client.post(
            "/api/batch/query", json={"items": [_item("q1"), _item("q1")]}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("重复", response.json()["msg"])

    def test_unknown_type(self):
        response = self.client.post(
            "/api/batch/query", json={"items": [_item(qtype="essay")]}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("题型", response.json()["msg"])

    def test_too_many_items(self):
        items = [_item(f"q{index}") for index in range(31)]
        response = self.client.post("/api/batch/query", json={"items": items})
        self.assertEqual(response.status_code, 400)
        self.assertIn("30", response.json()["msg"])

    def test_cache_hit(self):
        with mock.patch.object(main.bank, "find_exact", return_value=None), \
                mock.patch.object(main.answer_memory, "recall", return_value=None), \
                mock.patch.object(
                    main.cache, "lookup_for_answer",
                    return_value=({"answer": "A", "source": "ai",
                                   "provider": "m", "confidence": 0.9}, False),
                ), \
                mock.patch.object(main, "_call_batch_one") as call:
            response = self.client.post("/api/batch/query", json={"items": [_item()]})
        data = response.json()["data"]
        self.assertEqual(data["results"][0]["answer"], "A")
        self.assertTrue(data["results"][0]["cached"])
        self.assertEqual(data["results"][0]["source"], "ai")
        self.assertEqual(data["stats"]["cached"], 1)
        self.assertEqual(data["stats"]["ai_calls"], 0)
        call.assert_not_called()

    def test_prefetch_hides_answer(self):
        with mock.patch.object(main.bank, "find_exact", return_value=None), \
                mock.patch.object(main.answer_memory, "recall", return_value=None), \
                mock.patch.object(
                    main.cache, "lookup_for_answer",
                    return_value=({"answer": "A", "source": "ai",
                                   "provider": "m", "confidence": 0.9}, False),
                ):
            response = self.client.post(
                "/api/batch/query", json={"mode": "prefetch", "items": [_item()]}
            )
        row = response.json()["data"]["results"][0]
        self.assertNotIn("answer", row)
        self.assertTrue(row["ok"])

    def test_bank_hit_beats_cache(self):
        with mock.patch.object(
            main.bank, "find_exact",
            return_value={"answer": "B", "status": "ok", "source": "official"},
        ), mock.patch.object(main.cache, "lookup_for_answer") as lookup:
            response = self.client.post("/api/batch/query", json={"items": [_item()]})
        row = response.json()["data"]["results"][0]
        self.assertEqual(row["answer"], "B")
        self.assertEqual(row["source"], "bank")
        lookup.assert_not_called()

    def test_ai_path_writes_cache(self):
        async def fake_batch(provider, items):
            return {"q1": {"answer": "B", "confidence": 0.8}}, [], 12, ""

        patches = self._cheap_miss()
        with patches[0], patches[1], patches[2], \
                mock.patch.object(main, "_call_batch_one", fake_batch), \
                mock.patch.object(main, "_record_answer") as record, \
                mock.patch.object(main.cache, "set") as cache_set:
            response = self.client.post("/api/batch/query", json={"items": [_item()]})
        data = response.json()["data"]
        self.assertEqual(data["results"][0]["answer"], "B")
        self.assertEqual(data["results"][0]["source"], "ai")
        self.assertEqual(data["stats"]["answered"], 1)
        self.assertEqual(data["stats"]["ai_calls"], 1)
        self.assertEqual(cache_set.call_args.kwargs["source"], "ai")
        record.assert_called_once()

    def test_ai_failure_marks_no_answer(self):
        async def fake_batch(provider, items):
            return {}, ["q1"], 0, "上游 500"

        patches = self._cheap_miss()
        with patches[0], patches[1], patches[2], \
                mock.patch.object(main, "_call_batch_one", fake_batch):
            response = self.client.post("/api/batch/query", json={"items": [_item()]})
        data = response.json()["data"]
        self.assertEqual(data["results"][0]["answer"], "")
        self.assertEqual(data["results"][0]["error"], "no_answer")
        self.assertEqual(data["stats"]["failed"], 1)

    def test_rate_limit(self):
        old = main.config.BATCH_RATE_LIMIT
        main.config.BATCH_RATE_LIMIT = 1
        main._batch_hits.clear()
        try:
            hit = {
                "id": "q1", "answer": "A", "confidence": None, "source": "cache",
                "provider": "m", "cached": True, "elapsed_ms": 0, "error": "",
            }
            with mock.patch.object(main, "_batch_cheap_hit", return_value=hit):
                first = self.client.post("/api/batch/query", json={"items": [_item()]})
                second = self.client.post("/api/batch/query", json={"items": [_item()]})
        finally:
            main.config.BATCH_RATE_LIMIT = old
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)


if __name__ == "__main__":
    unittest.main()

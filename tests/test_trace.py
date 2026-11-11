"""答题 trace 存储的测试：记录、环形淘汰、响应解析与异常隔离。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.responses import JSONResponse  # noqa: E402

from app.trace import MAX_MODELS, TraceStore  # noqa: E402


class TraceStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = TraceStore(max_traces=3)

    def tearDown(self):
        self.store.reset()

    def test_begin_binds_current_and_increments_seq(self):
        first = self.store.begin(title="题一", qtype="single", options=["A", "B"])
        self.assertIs(self.store.current(), first)
        self.assertEqual(first.seq, 1)
        second = self.store.begin(title="题二")
        self.assertEqual(second.seq, 2)
        self.assertIs(self.store.current(), second)

    def test_stage_and_update(self):
        trace = self.store.begin(title="题")
        self.store.update(scene="unit", subject="概率论")
        self.store.stage("bank", "hit", answer="A", item_id=7)
        self.store.stage("cache", "miss")
        data = self.store.recent(limit=1)["traces"][0]
        self.assertEqual(data["scene"], "unit")
        self.assertEqual(data["subject"], "概率论")
        self.assertEqual([item["name"] for item in data["stages"]], ["bank", "cache"])
        self.assertEqual(data["stages"][0]["answer"], "A")
        self.assertEqual(data["stages"][0]["status"], "hit")

    def test_non_jsonable_extra_is_dropped(self):
        self.store.begin(title="题")
        self.store.stage("x", "done", bad=object(), good="ok")
        stage = self.store.recent(limit=1)["traces"][0]["stages"][0]
        self.assertNotIn("bad", stage)
        self.assertEqual(stage["good"], "ok")

    def test_model_lifecycle(self):
        self.store.begin(title="题")
        entry = self.store.model_start({"id": "1", "name": "模型甲", "model": "m"})
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "running")
        self.store.model_end(entry, answer="B", confidence=0.9, elapsed_ms=321)
        models = self.store.recent(limit=1)["traces"][0]["models"]
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["status"], "ok")
        self.assertEqual(models[0]["answer"], "B")
        self.assertEqual(models[0]["elapsed_ms"], 321)
        self.assertIsNotNone(models[0]["t_end"])

    def test_model_cap(self):
        self.store.begin(title="题")
        for index in range(MAX_MODELS + 3):
            self.store.model_start({"id": str(index), "name": f"m{index}"})
        models = self.store.recent(limit=1)["traces"][0]["models"]
        self.assertEqual(len(models), MAX_MODELS)

    def test_model_usage_and_totals(self):
        self.store.begin(title="题")
        entry = self.store.model_start({"id": "1", "name": "模型甲"})
        self.store.model_end(
            entry, answer="A", elapsed_ms=100,
            usage={"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
        )
        data = self.store.recent(limit=1)["traces"][0]
        self.assertEqual(data["models"][0]["total_tokens"], 15)
        self.assertEqual(data["total_tokens"], 15)

    def test_stage_span_covers_until_next_stage(self):
        self.store.begin(title="题")
        self.store.stage("a", "done")
        self.store.stage("b", "done")
        data = self.store.recent(limit=1)["traces"][0]
        self.assertIn("span_ms", data["stages"][0])
        self.assertGreaterEqual(data["max_t"], data["stages"][-1]["t"])

    def test_stage_can_use_earlier_start(self):
        import time

        self.store.begin(title="题")
        started = time.perf_counter()
        time.sleep(0.05)
        self.store.stage("prompt", "done", started_at=started)
        self.store.stage("after", "done")
        data = self.store.recent(limit=1)["traces"][0]
        self.assertLess(data["stages"][0]["t"], 20)
        self.assertLess(data["stages"][0]["t"], data["stages"][1]["t"])

    def test_vote(self):
        self.store.begin(title="题")
        self.store.set_vote({"kind": "unit", "votes": {"A": 2, "B": 1}, "share": 0.67})
        vote = self.store.recent(limit=1)["traces"][0]["vote"]
        self.assertEqual(vote["votes"], {"A": 2, "B": 1})
        self.assertAlmostEqual(vote["share"], 0.67)

    def test_finish_parses_success_response(self):
        trace = self.store.begin(title="题")
        response = JSONResponse({
            "code": 0,
            "data": {"answer": "A", "source": "ai", "provider": "模型甲", "confidence": 0.8},
        })
        self.store.finish(trace, response=response)
        self.assertIsNone(self.store.current())
        data = self.store.recent(limit=1)["traces"][0]
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["result"]["answer"], "A")
        self.assertEqual(data["result"]["provider"], "模型甲")
        self.assertIsNotNone(data["elapsed_ms"])
        self.assertEqual(data["stages"][-1]["name"], "finish")

    def test_finish_parses_failure_response(self):
        trace = self.store.begin(title="题")
        response = JSONResponse({"code": 50001, "message": "没有启用的 AI"}, status_code=200)
        self.store.finish(trace, response=response)
        data = self.store.recent(limit=1)["traces"][0]
        self.assertEqual(data["status"], "error")
        self.assertEqual(data["error"], "没有启用的 AI")

    def test_finish_marks_running_models_cancelled(self):
        trace = self.store.begin(title="题")
        self.store.model_start({"id": "1", "name": "慢模型"})
        self.store.finish(trace, error="")
        model = self.store.recent(limit=1)["traces"][0]["models"][0]
        self.assertEqual(model["status"], "cancelled")
        self.assertIn("取消", model["error"])
        self.assertIsNotNone(model["t_end"])

    def test_finish_twice_is_idempotent(self):
        trace = self.store.begin(title="题")
        self.store.finish(trace, error="第一次")
        self.store.finish(trace, error="第二次")
        data = self.store.recent(limit=1)["traces"][0]
        self.assertEqual(data["error"], "第一次")

    def test_recent_newest_first_and_ring_buffer(self):
        for index in range(5):
            trace = self.store.begin(title=f"题{index}")
            self.store.finish(trace, error=f"e{index}")
        payload = self.store.recent(limit=10)
        self.assertEqual(payload["latest"], 5)
        seqs = [item["seq"] for item in payload["traces"]]
        self.assertEqual(seqs, [5, 4, 3])

    def test_recent_after_seq(self):
        for index in range(4):
            trace = self.store.begin(title=f"题{index}")
            self.store.finish(trace, error="x")
        payload = self.store.recent(limit=10, after_seq=2)
        self.assertEqual([item["seq"] for item in payload["traces"]], [4, 3])

    def test_broken_payload_never_raises(self):
        self.store.begin(title="题")
        self.store.model_end({"status": "running"}, answer=object())
        self.store.stage("x", None, value=None)
        self.store.set_vote(object())
        self.assertIsNotNone(self.store.recent())

    def test_batch_fields_and_filter(self):
        batch = self.store.begin(
            title="批量 2 题",
            qtype="batch",
            batch_id="b1",
            batch_items=[{"id": "q1", "answer": "A"}],
        )
        batch.finish(result={"items": 2, "resolved": 1})
        single = self.store.begin(title="单题")
        single.finish(result={"answer": "B"})

        payload = self.store.recent(limit=5, batch_id="b1")
        self.assertEqual(len(payload["traces"]), 1)
        row = payload["traces"][0]
        self.assertEqual(row["batch_id"], "b1")
        self.assertEqual(row["batch_items"][0]["id"], "q1")
        self.assertEqual(row["qtype"], "batch")

        self.assertEqual(self.store.recent(limit=5, batch_id="nope")["traces"], [])
        self.assertEqual(len(self.store.recent(limit=5)["traces"]), 2)


if __name__ == "__main__":
    unittest.main()

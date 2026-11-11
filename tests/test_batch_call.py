"""批量提示词、分片与 `_call_batch_one` 的测试。"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import main  # noqa: E402
from app.batch_parse import plan_batches  # noqa: E402
from app.parsing import build_batch_messages  # noqa: E402


def _item(qid: str, qtype: str = "single", options=None, title: str = "题干"):
    return {
        "id": qid,
        "type": qtype,
        "title": title,
        "options": options if options is not None else ["甲", "乙"],
    }


class BatchPromptTests(unittest.TestCase):
    def test_messages_contain_ids_types_and_options(self):
        items = [
            _item("q1", "single", ["甲", "乙"]),
            _item("q2", "multiple", ["甲", "乙", "丙"]),
        ]
        messages = build_batch_messages(items)
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn('"answers"', messages[0]["content"])
        self.assertIn("不得自增", messages[0]["content"])
        body = messages[1]["content"]
        self.assertIn("【q1】", body)
        self.assertIn("题型：单选题", body)
        self.assertIn("A. 甲", body)
        self.assertIn("【q2】", body)
        self.assertIn("题型：多选题", body)
        self.assertLess(body.index("【q1】"), body.index("【q2】"))

    def test_context_is_prepended(self):
        messages = build_batch_messages([_item("q1")], context="第三章 函数")
        self.assertTrue(messages[1]["content"].startswith("课程/作业上下文：第三章 函数"))

    def test_extra_text_included(self):
        item = _item("q1")
        item["extra"] = "图片转写：求导"
        body = build_batch_messages([item])[1]["content"]
        self.assertIn("图片转写：求导", body)


class PlanBatchesTests(unittest.TestCase):
    def test_item_limit(self):
        items = [_item(f"q{i}") for i in range(10)]
        batches = plan_batches(items, max_items=4, max_chars=10 ** 6)
        self.assertEqual([len(batch) for batch in batches], [4, 4, 2])

    def test_char_limit(self):
        items = [_item(f"q{i}", title="x" * 80) for i in range(5)]
        batches = plan_batches(items, max_items=99, max_chars=250)
        self.assertEqual([len(batch) for batch in batches], [2, 2, 1])

    def test_oversized_item_gets_its_own_batch(self):
        items = [_item("big", title="x" * 500), _item("small", title="x")]
        batches = plan_batches(items, max_items=99, max_chars=100)
        self.assertEqual([len(batch) for batch in batches], [1, 1])
        self.assertEqual(batches[0][0]["id"], "big")

    def test_empty(self):
        self.assertEqual(plan_batches([]), [])


class CallBatchOneTests(unittest.TestCase):
    PROVIDER = {"id": "p1", "name": "甲", "timeout": 10}

    def _run(self, items, fake):
        with mock.patch.object(main, "call_provider", fake), \
                mock.patch.object(main, "stats") as fake_stats:
            result = asyncio.run(main._call_batch_one(self.PROVIDER, items))
        return result, fake_stats

    def test_success_mixed_types(self):
        items = [
            _item("q1", "single", ["甲", "乙"]),
            _item("q2", "multiple", ["甲", "乙", "丙"]),
            _item("q3", "judge", ["正确", "错误"]),
        ]

        async def fake(provider, messages):
            return (
                '{"answers":['
                '{"id":"q1","answer":"a","confidence":0.9},'
                '{"id":"q2","answer":["A","C"]},'
                '{"id":"q3","answer":true}'
                "]}"
            )

        (resolved, missing, elapsed, error), fake_stats = self._run(items, fake)
        self.assertEqual(error, "")
        self.assertEqual(missing, [])
        self.assertEqual(resolved["q1"]["answer"], "A")
        self.assertEqual(resolved["q1"]["confidence"], 0.9)
        self.assertEqual(resolved["q2"]["answer"], "A#C")
        self.assertEqual(resolved["q3"]["answer"], "正确")
        self.assertGreaterEqual(elapsed, 0)
        fake_stats.success.assert_called_once()

    def test_partial_result_marks_missing(self):
        items = [_item("q1"), _item("q2")]

        async def fake(provider, messages):
            return '{"answers":[{"id":"q1","answer":"A"}]}'

        (resolved, missing, _elapsed, error), _stats = self._run(items, fake)
        self.assertEqual(error, "")
        self.assertEqual(set(resolved), {"q1"})
        self.assertEqual(missing, ["q2"])

    def test_unparseable_text_falls_back_to_all_missing(self):
        items = [_item("q1"), _item("q2")]

        async def fake(provider, messages):
            return "我今天不想答题。"

        (resolved, missing, _elapsed, error), _stats = self._run(items, fake)
        self.assertEqual(resolved, {})
        self.assertEqual(missing, ["q1", "q2"])
        self.assertEqual(error, "")

    def test_answer_outside_options_is_dropped(self):
        items = [_item("q1", "single", ["甲", "乙"])]

        async def fake(provider, messages):
            return '{"answers":[{"id":"q1","answer":"Z"}]}'

        (resolved, missing, _elapsed, _error), _stats = self._run(items, fake)
        self.assertEqual(resolved, {})
        self.assertEqual(missing, ["q1"])

    def test_provider_error_returns_all_missing(self):
        items = [_item("q1")]

        async def fake(provider, messages):
            raise main.ProviderError("上游 500")

        (resolved, missing, _elapsed, error), fake_stats = self._run(items, fake)
        self.assertEqual(resolved, {})
        self.assertEqual(missing, ["q1"])
        self.assertIn("上游 500", error)
        fake_stats.failure.assert_called_once()

    def test_timeout_returns_all_missing(self):
        items = [_item("q1")]
        provider = {"id": "p1", "name": "甲", "timeout": 0.01}

        async def fake(_provider, _messages):
            await asyncio.sleep(0.5)
            return "{}"

        with mock.patch.object(main, "call_provider", fake), \
                mock.patch.object(main, "stats"):
            resolved, missing, _elapsed, error = asyncio.run(
                main._call_batch_one(provider, items)
            )
        self.assertEqual(resolved, {})
        self.assertEqual(missing, ["q1"])
        self.assertIn("超时", error)

    def test_empty_items(self):
        resolved, missing, elapsed, error = asyncio.run(
            main._call_batch_one(self.PROVIDER, [])
        )
        self.assertEqual((resolved, missing, elapsed, error), ({}, [], 0, ""))


if __name__ == "__main__":
    unittest.main()

"""缓存低置信随机重问的单元测试。

规则：只有 AI 来源、且置信度低于阈值的命中才可能被重问；
题库/官方答案命中永不重问，概率为 0 时行为与旧版完全一致。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.cache import AnswerCache  # noqa: E402


class CacheRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = AnswerCache(
            os.path.join(self.tmp.name, "answers.db"),
            ttl_hours=168,
            completion_ttl_hours=24,
        )
        self.addCleanup(self.cache.close)

    def _set(self, **kwargs):
        params = {
            "key": "k1",
            "question": "题干",
            "answer": "A",
            "provider": "m",
            "qtype": "single",
        }
        params.update(kwargs)
        self.cache.set(**params)

    def test_low_confidence_ai_hit_is_retried(self):
        self._set(source="ai", confidence=0.4)
        row, retried = self.cache.lookup_for_answer(
            "k1", retry_prob=1.0, min_confidence=0.75
        )
        self.assertIsNone(row)
        self.assertTrue(retried)

    def test_high_confidence_ai_hit_stays(self):
        self._set(source="ai", confidence=0.9)
        row, retried = self.cache.lookup_for_answer(
            "k1", retry_prob=1.0, min_confidence=0.75
        )
        self.assertIsNotNone(row)
        self.assertFalse(retried)
        self.assertEqual(row["confidence"], 0.9)
        self.assertEqual(row["source"], "ai")

    def test_authoritative_source_never_retried(self):
        self._set(source="bank", confidence=0.1)
        row, retried = self.cache.lookup_for_answer(
            "k1", retry_prob=1.0, min_confidence=0.75
        )
        self.assertIsNotNone(row)
        self.assertFalse(retried)

    def test_legacy_row_without_source_not_retried(self):
        self._set()
        row, retried = self.cache.lookup_for_answer(
            "k1", retry_prob=1.0, min_confidence=0.75
        )
        self.assertIsNotNone(row)
        self.assertFalse(retried)
        self.assertEqual(row["source"], "")
        self.assertIsNone(row["confidence"])

    def test_probability_zero_disables_retry(self):
        self._set(source="ai", confidence=0.1)
        row, retried = self.cache.lookup_for_answer(
            "k1", retry_prob=0.0, min_confidence=0.75
        )
        self.assertIsNotNone(row)
        self.assertFalse(retried)

    def test_miss_returns_none(self):
        row, retried = self.cache.lookup_for_answer("nope")
        self.assertIsNone(row)
        self.assertFalse(retried)

    def test_get_for_answer_returns_row(self):
        self._set(source="ai", confidence=0.95)
        row = self.cache.get_for_answer("k1", retry_prob=1.0, min_confidence=0.75)
        self.assertIsNotNone(row)
        self.assertEqual(row["answer"], "A")

    def test_retry_result_overwrites_old_row(self):
        self._set(source="ai", confidence=0.2)
        row, retried = self.cache.lookup_for_answer(
            "k1", retry_prob=1.0, min_confidence=0.75
        )
        self.assertTrue(retried)
        self.cache.set("k1", "题干", "B", "m", qtype="single",
                       source="ai", confidence=0.95)
        fresh = self.cache.get("k1")
        self.assertIsNotNone(fresh)
        self.assertEqual(fresh["answer"], "B")
        self.assertEqual(fresh["confidence"], 0.95)


if __name__ == "__main__":
    unittest.main()

"""Math-Verify 等价判定、填空题判分与缓存 TTL 的测试。"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import evaluation as ev  # noqa: E402
from app import mathcheck  # noqa: E402
from app.cache import AnswerCache  # noqa: E402
from app.quality import check_official  # noqa: E402


class MathEquivalentTests(unittest.TestCase):
    def setUp(self):
        mathcheck.cache_clear()

    def test_same_value_different_notation(self):
        self.assertTrue(mathcheck.math_equivalent("1/2", "0.5"))
        self.assertTrue(mathcheck.math_equivalent("0.50", "1/2"))
        self.assertTrue(mathcheck.math_equivalent("50%", "0.5"))
        self.assertTrue(mathcheck.math_equivalent(r"\frac{1}{2}", "0.5"))
        self.assertTrue(mathcheck.math_equivalent("15", "15.0"))

    def test_approximation_is_not_equivalence(self):
        self.assertFalse(mathcheck.math_equivalent("0.0833", "1/12"))
        self.assertFalse(mathcheck.math_equivalent("1/3", "3/32"))

    def test_chinese_units_are_not_stripped(self):
        self.assertFalse(mathcheck.math_equivalent("3.5米", "3.5秒"))
        self.assertTrue(mathcheck.math_equivalent("3.5米", "3.5米"))
        self.assertTrue(mathcheck.math_equivalent("正确", "正确"))
        self.assertFalse(mathcheck.math_equivalent("正确", "错误"))

    def test_empty_and_text(self):
        self.assertFalse(mathcheck.math_equivalent("", "0"))
        self.assertFalse(mathcheck.math_equivalent("随机", "随机事件"))
        self.assertFalse(mathcheck.math_equivalent("A", "B"))

    def test_degrades_when_library_missing(self):
        mathcheck.cache_clear()
        with mock.patch.object(mathcheck, "_parser", None), \
                mock.patch.object(mathcheck, "_verifier", None):
            self.assertFalse(mathcheck.available())
            self.assertFalse(mathcheck.math_equivalent("1/2", "0.5"))
        # 退出 mock 后恢复可用（本机装了 math-verify 时）
        if mathcheck.available():
            self.assertTrue(mathcheck.math_equivalent("1/2", "0.5"))


class JudgeCompletionTests(unittest.TestCase):
    def test_symbolic_equivalence(self):
        self.assertEqual(ev.judge_completion("0.5", "1/2"), (True, "symbolic"))
        self.assertEqual(ev.judge_completion(r"\frac{1}{2}", "0.5"), (True, "symbolic"))
        self.assertEqual(ev.judge_completion("15", "15.0"), (True, "symbolic"))

    def test_fraction_truth_no_longer_rounds_to_zero(self):
        # 旧 numeric 档把 1/3 和 3/32 都四舍五入到 0 位，误判为相等
        self.assertEqual(ev.judge_completion("1/3", "3/32"), (False, "mismatch"))
        self.assertEqual(ev.judge_completion("0.0833", "1/12"), (False, "mismatch"))

    def test_numeric_tolerance_only_for_decimal_truth(self):
        self.assertEqual(ev.judge_completion("0.0833", "0.08"), (True, "numeric"))
        self.assertEqual(ev.judge_completion("0.3", "0.08"), (False, "mismatch"))
        # 整数真值不接受 14.9
        self.assertEqual(ev.judge_completion("14.9", "15"), (False, "mismatch"))

    def test_existing_reasons_unchanged(self):
        self.assertEqual(ev.judge_completion("0.08", "0.08"), (True, "exact"))
        self.assertEqual(ev.judge_completion("随机事件", "随机"), (True, "prefix"))
        self.assertEqual(ev.judge_completion("", "0.08"), (False, "empty"))


class OfficialCheckMathTests(unittest.TestCase):
    def test_completion_equivalent_keeps_official(self):
        result = check_official("0.5", "1/2", 0, 0.0, qtype="completion")
        self.assertEqual(result.decision, "agree")
        self.assertEqual(result.answer, "0.5")

    def test_completion_different_allows_ai_consensus(self):
        result = check_official("0.5", "0.6", 2, 1.0, qtype="completion")
        self.assertEqual(result.decision, "override")
        self.assertEqual(result.answer, "0.6")

    def test_judge_not_affected(self):
        self.assertEqual(check_official("正确", "正确", 0, 0.0, qtype="judge").decision, "agree")


class UnitVoteMergeTests(unittest.TestCase):
    def _provider(self, pid: str) -> dict:
        return {"id": pid, "name": f"m{pid}"}

    def test_equivalent_completion_answers_merge_into_one_vote(self):
        from app import main

        providers = [self._provider("1"), self._provider("2"), self._provider("3")]
        results = [
            (providers[0], "0.5", 0.9, None),
            (providers[1], r"\frac{1}{2}", 0.9, None),
            (providers[2], "1/2", 0.9, None),
        ]
        with mock.patch.object(main.stats, "quality_snapshot", return_value={"providers": {}}), \
                mock.patch.object(main.settings, "all", return_value={"confidence_weight": False}):
            choice, canon_first, canon_models = main._unit_vote(results, "completion")
        self.assertIsNotNone(choice.winner)
        self.assertEqual(choice.counts[choice.winner], 3)
        self.assertEqual(len(canon_models[choice.winner]), 3)
        self.assertEqual(canon_first[choice.winner], "0.5")

    def test_approximate_answers_do_not_merge(self):
        from app import main

        providers = [self._provider("1"), self._provider("2")]
        results = [
            (providers[0], "0.0833", 0.9, None),
            (providers[1], "1/12", 0.9, None),
        ]
        with mock.patch.object(main.stats, "quality_snapshot", return_value={"providers": {}}), \
                mock.patch.object(main.settings, "all", return_value={"confidence_weight": False}):
            choice, _canon_first, canon_models = main._unit_vote(results, "completion")
        self.assertEqual(len(choice.counts), 2)
        self.assertTrue(all(len(models) == 1 for models in canon_models.values()))


class CacheCompletionTtlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = AnswerCache(
            os.path.join(self.tmp.name, "answers.db"),
            ttl_hours=168,
            completion_ttl_hours=24,
        )
        self.addCleanup(self.cache.close)

    def test_completion_expires_sooner(self):
        self.cache.set("k1", "题", "1/12", "m", qtype="completion")
        self.cache.set("k2", "题", "A", "m", qtype="single")
        old = time.time() - 25 * 3600
        self.cache._conn.execute(
            "UPDATE answers SET created = ? WHERE key IN ('k1', 'k2')", (old,)
        )
        self.cache._conn.commit()
        self.assertIsNone(self.cache.get("k1"))
        self.assertIsNotNone(self.cache.get("k2"))

    def test_invalidate_by_question(self):
        self.cache.set("k1", "同一道题", "A", "m", qtype="single")
        self.cache.set("k2", "同一道题", "A", "m", qtype="single")
        self.cache.set("k3", "另一道题", "B", "m", qtype="single")
        removed = self.cache.invalidate("single", "同一道题")
        self.assertEqual(removed, 2)
        self.assertIsNone(self.cache.get("k1"))
        self.assertIsNone(self.cache.get("k2"))
        self.assertIsNotNone(self.cache.get("k3"))

    def test_zero_completion_ttl_falls_back_to_global(self):
        cache = AnswerCache(
            os.path.join(self.tmp.name, "answers2.db"),
            ttl_hours=168,
            completion_ttl_hours=0,
        )
        self.addCleanup(cache.close)
        cache.set("k1", "题", "1/12", "m", qtype="completion")
        old = time.time() - 25 * 3600
        cache._conn.execute("UPDATE answers SET created = ? WHERE key = 'k1'", (old,))
        cache._conn.commit()
        self.assertIsNotNone(cache.get("k1"))


if __name__ == "__main__":
    unittest.main()

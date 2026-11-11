"""结果页反馈写库：先算好、校验过、一次写入。

口径：
- 旧实现先 ``mark_wrong`` 清空答案、再 ``update`` 写新答案。两次落盘之间
  进程退出 / update 被来源优先级拦下，题库就只剩一条 pending 空记录，
  下次答题退化成 AI（用户看到的就是「同一个题一直错」）。
- 现在旧答案由 ``update`` 自己进 history，用户错选的字母进 ``wrong_answers``，
  记录消失时旧答案原样保留。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bank import Bank  # noqa: E402
from app.mooc_result import apply_feedback  # noqa: E402
from app.quiz_status import QuizStatus  # noqa: E402

TITLE = "行列式 X 的充分必要条件是（ ）."
OPTIONS = ["某两列元素成比例", "某一行元素为 0", "转置行列式为 0"]


class FeedbackAtomicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = Bank(
            os.path.join(self.tmp.name, "bank.json"),
            os.path.join(self.tmp.name, "bank.db"),
        )
        self.addCleanup(self.bank.db.close)

    def seed(self, answer="A", **extra):
        payload = {
            "type": "single",
            "title": TITLE,
            "options": list(OPTIONS),
            "answer": answer,
            "source": "official",
            "status": "adopted",
        }
        payload.update(extra)
        return self.bank.add(payload)

    def feedback(self, letters, selected, **extra):
        item = {
            "type": "single",
            "title": TITLE,
            "options": list(OPTIONS),
            "correct_letters": list(letters),
            "selected_letters": list(selected),
            "is_correct": False,
            "analysis_visible": True,
        }
        item.update(extra)
        return apply_feedback(self.bank, [item], dry_run=False)

    def test_wrong_answer_is_replaced_in_one_write(self):
        record = self.seed(answer="A")
        result = self.feedback(["C"], ["A"])

        got = self.bank.get(record["id"])
        self.assertEqual(got["answer"], "C")
        self.assertEqual(got["options"], OPTIONS)
        self.assertEqual(got["status"], "adopted")
        self.assertEqual(got["source"], "official")
        self.assertEqual(result["marked_wrong"], 1)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["details"][0]["action"], "fix")
        # 旧答案进 history，同时进 wrong_answers；用户错选也记下来
        self.assertTrue(any(h.get("answer") == "A" for h in got["history"]))
        self.assertIn("A", got["wrong_answers"])

    def test_failed_update_keeps_old_answer(self):
        record = self.seed(answer="A")
        with mock.patch.object(self.bank, "update", return_value=None):
            result = self.feedback(["C"], ["A"])

        got = self.bank.get(record["id"])
        # 关键：不能被清成空答案
        self.assertEqual(got["answer"], "A")
        self.assertEqual(got["status"], "adopted")
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["details"][0]["action"], "update-failed")

    def test_out_of_range_answer_does_not_touch_existing(self):
        record = self.seed(answer="A")
        result = self.feedback(["D"], ["B"])

        got = self.bank.get(record["id"])
        self.assertEqual(got["answer"], "A")  # 非法答案不覆盖
        self.assertIn("B", got["wrong_answers"])  # 但错选要留下
        self.assertEqual(result["details"][0]["action"], "wrong")

    def test_wrong_pick_recorded_without_official_answer(self):
        record = self.seed(answer="A")
        self.feedback([], ["B"])
        got = self.bank.get(record["id"])
        self.assertEqual(got["answer"], "A")
        self.assertIn("B", got["wrong_answers"])

    def test_missing_record_and_no_answer_is_skipped(self):
        result = self.feedback([], ["A"])
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.bank.count(), 0)


class QuizStatusPendingTests(unittest.TestCase):
    def test_pending_follows_latest_report_but_published_is_sticky(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = QuizStatus(os.path.join(tmp, "quiz_status.json"))
            status.upsert({"tid": "1", "aid": "2", "published": False, "pending": True})
            self.assertTrue(status.all()[0]["pending"])
            self.assertFalse(status.all()[0]["published"])

            status.upsert({"tid": "1", "aid": "3", "published": True, "pending": False})
            row = status.all()[0]
            self.assertTrue(row["published"])
            self.assertFalse(row["pending"])

            # 官方答案拿到过就记着；后一次空上报不该把 pending 又翻回 True
            status.upsert({"tid": "1", "aid": "4", "published": False, "pending": False})
            row = status.all()[0]
            self.assertTrue(row["published"])
            self.assertFalse(row["pending"])


if __name__ == "__main__":
    unittest.main()

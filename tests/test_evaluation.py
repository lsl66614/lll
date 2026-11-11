"""评估内核（app/evaluation.py）的单元测试：比对口径与官方候选。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import evaluation as ev  # noqa: E402
from app.bank import Bank  # noqa: E402


def _case(qtype: str, options: list[str], truth: tuple[str, ...]) -> dict:
    return {"type": qtype, "title": "题干", "options": options, "truth": truth}


class AnswerMatchesTests(unittest.TestCase):
    def test_single_choice_letter_maps_to_option_content(self):
        case = _case("single", ["北京", "上海"], (ev.norm_option("上海"),))
        self.assertTrue(ev.answer_matches("B", case))
        self.assertFalse(ev.answer_matches("A", case))

    def test_judge_accepts_semantic_and_letter(self):
        case = _case("judge", ["正确", "错误"], ("正确",))
        self.assertTrue(ev.answer_matches("正确", case))
        self.assertTrue(ev.answer_matches("A", case))
        self.assertFalse(ev.answer_matches("错误", case))

    def test_completion_numeric_keeps_decimals(self):
        case = _case("completion", [], ("0.08",))
        self.assertTrue(ev.answer_matches("1/12", case))
        self.assertFalse(ev.answer_matches("0.3", case))
        self.assertFalse(ev.answer_matches("", case))

    def test_judge_completion_reports_reason(self):
        self.assertEqual(ev.judge_completion("0.08", "0.08"), (True, "exact"))
        self.assertEqual(ev.judge_completion("1/12", "0.08")[0], True)
        self.assertEqual(ev.judge_completion("0.3", "0.08"), (False, "mismatch"))


class OfficialCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = Bank(os.path.join(self.tmp.name, "bank.json"))
        self.addCleanup(self.bank.close)

    def test_only_adopted_with_same_options(self):
        case = _case("single", ["北京", "上海"], ())
        hit = self.bank.add({"type": "single", "title": "题干", "options": ["北京", "上海"],
                             "answer": "B", "source": "official", "status": "adopted"})
        pending = self.bank.add({"type": "single", "title": "题干", "options": ["北京", "上海"],
                                 "answer": "A", "source": "official", "status": "pending"})
        other = self.bank.add({"type": "single", "title": "另一题", "options": ["北京", "上海"],
                               "answer": "A", "source": "official", "status": "adopted"})
        ids = [i["id"] for i in ev.official_candidates(self.bank, case)]
        self.assertIn(hit["id"], ids)
        self.assertNotIn(pending["id"], ids)
        self.assertNotIn(other["id"], ids)


if __name__ == "__main__":
    unittest.main()

"""quizscore 分数页隐藏答案（analysisInfo.f-dn）的解析与放行规则。"""

from __future__ import annotations

import unittest

from app.mooc_result import apply_feedback, parse_result_html


def _completion_block(answer: str = "0.4", *, hidden: bool = True,
                      exam_mode: bool = False) -> str:
    cls = "m-FillBlank u-questionItem f-pr analysisMode"
    if exam_mode:
        cls += " examMode"
    analysis_cls = "analysisInfo j-analysis answrong" + (" f-dn" if hidden else "")
    return (
        '<div class="%s">'
        '<div class="j-title title f-cb">'
        '<div class="f-richEditorText j-richTxt">设联合概率密度为f(x,y)，则常数k=</div>'
        '<div class="qaMark j-qamark"></div></div>'
        '<div class="j-textarea"><textarea disabled></textarea></div>'
        '<div class="%s"><span class="f-f0 tt1">正确答案：</span>'
        '<span class="f-f0 tt2">%s</span></div>'
        '</div>'
    ) % (cls, analysis_cls, answer)


class HiddenAnalysisParseTest(unittest.TestCase):
    def test_hidden_completion_answer_is_marked(self):
        items = parse_result_html(_completion_block("0.4"))
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["type"], "completion")
        self.assertEqual(item["correct_text"], "0.4")
        self.assertFalse(item["analysis_visible"])
        self.assertTrue(item["analysis_hidden"])

    def test_exam_mode_hidden_answer_is_not_marked(self):
        """答题页的隐藏答案必须是占位/错位的，不能放行。"""
        items = parse_result_html(_completion_block("0.4", exam_mode=True))
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertFalse(item["analysis_visible"])
        self.assertFalse(item["analysis_hidden"])
        self.assertEqual(item["correct_text"], "")

    def test_visible_analysis_is_not_hidden(self):
        items = parse_result_html(_completion_block("0.4", hidden=False))
        item = items[0]
        self.assertTrue(item["analysis_visible"])
        self.assertFalse(item["analysis_hidden"])


class _FakeBank:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def find_exact(self, *args, **kwargs):
        return None

    def match(self, *args, **kwargs):
        return None

    def subjects(self):
        return []

    def upsert_calibrated(self, qtype, title, options, answer, subject,
                          adopted=True, source="official"):
        item = {"id": str(len(self.items) + 1), "type": qtype, "title": title,
                "options": options, "answer": answer, "subject": subject,
                "source": source}
        self.items.append(item)
        return item

    def update(self, item_id, fields):
        self.items[0].update(fields)
        return self.items[0]

    def align_official(self, *args, **kwargs):
        return 0


class HiddenAnalysisApplyTest(unittest.TestCase):
    def _item(self, answer: str = "0.4") -> dict:
        return {
            "type": "completion",
            "title": "设联合概率密度为f(x,y)，则常数k=",
            "options": [],
            "selected_letters": [],
            "correct_letters": [],
            "correct_text": answer,
            "is_correct": False,
            "blank": True,
            "status_text": "",
            "analysis_visible": False,
            "analysis_hidden": True,
        }

    def test_apply_feedback_skips_hidden_by_default(self):
        result = apply_feedback(object(), [self._item()])
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["applied"], 0)

    def test_apply_feedback_accepts_hidden_when_enabled(self):
        bank = _FakeBank()
        result = apply_feedback(bank, [self._item("0.4")], via="page",
                                allow_hidden=True)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["created"], 1)
        self.assertEqual(bank.items[0]["answer"], "0.4")
        self.assertEqual(bank.items[0]["source"], "official")


class SnapshotFeedbackItemsTest(unittest.TestCase):
    def setUp(self):
        from app.main import _snapshot_feedback_items
        self.pick = _snapshot_feedback_items

    def test_hidden_items_only_on_quizscore(self):
        parsed = [
            {"analysis_visible": True, "correct_text": "A"},
            {"analysis_visible": False, "analysis_hidden": True, "correct_text": "0.4"},
            {"analysis_visible": False, "analysis_hidden": True, "correct_text": ""},
            {"analysis_visible": False, "analysis_hidden": False, "correct_text": ""},
        ]
        quiz = self.pick(parsed, "quizscore")
        self.assertEqual(len(quiz), 2)
        self.assertEqual([x.get("correct_text") for x in quiz], ["A", "0.4"])

        other = self.pick(parsed, "inline-result")
        self.assertEqual(len(other), 1)
        self.assertTrue(other[0]["analysis_visible"])


if __name__ == "__main__":
    unittest.main()

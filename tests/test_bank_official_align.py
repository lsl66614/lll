"""同题多条官方记录的答案对齐。

反馈写入只命中一条记录，查询却可能挑另一条；本用例锁定「一次官方反馈
把同题所有官方记录统一成同一份答案+选项顺序」的行为与安全闸门。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bank import Bank  # noqa: E402
from app.mooc_result import apply_feedback  # noqa: E402

TITLE = "同题多条官方记录"
OPTIONS = ["甲", "乙", "丙", "丁"]


class OfficialAlignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = Bank(os.path.join(self.tmp.name, "bank.json"))
        self.addCleanup(self.bank.close)

    def add(self, **extra):
        payload = {
            "type": "single",
            "title": TITLE,
            "options": list(OPTIONS),
            "answer": "A",
            "source": "official",
            "status": "adopted",
        }
        payload.update(extra)
        return self.bank.add(payload)

    def test_apply_feedback_aligns_all_official_duplicates(self):
        first = self.add(options=["甲", "乙", "丙", "丁"], answer="D")
        second = self.add(options=["乙", "甲", "丙", "丁"], answer="C",
                          subject="线性代数")
        ai = self.add(options=["甲", "乙", "丙", "丁"], answer="A",
                      source="ai", status="pending")

        # 本次反馈顺序里 B = 甲
        result = apply_feedback(
            self.bank,
            [{"type": "single", "title": TITLE, "options": ["丙", "甲", "丁", "乙"],
              "correct_letters": ["B"], "is_correct": True}],
            None, False, via="page",
        )
        self.assertEqual(result["applied"], 1)
        self.assertGreaterEqual(result["aligned"], 1)

        for item in (self.bank.get(first["id"]), self.bank.get(second["id"])):
            self.assertEqual(item["answer"], "B")
            self.assertEqual(item["options"], ["丙", "甲", "丁", "乙"])
            self.assertEqual(item["source"], "official")
            self.assertEqual(item["status"], "adopted")

        # 非官方记录保持原样，不被「统一」成官方题。
        untouched = self.bank.get(ai["id"])
        self.assertEqual(untouched["source"], "ai")
        self.assertEqual(untouched["status"], "pending")
        self.assertEqual(untouched["answer"], "A")

        # 第三种选项顺序查询时，返回的字母仍指向「甲」
        hit = self.bank.find_official("single", TITLE, ["乙", "丁", "甲", "丙"])
        self.assertIsNotNone(hit)
        self.assertEqual(hit["answer"], "C")

    def test_align_skips_different_image_identities(self):
        first = self.add(image_hash="h1", answer="A")
        first_twin = self.add(image_hash="h1", answer="A")
        second = self.add(image_hash="h2", answer="A")

        # 带 h1 的反馈只改 h1 这一组，h2 的图片变体不动
        changed = self.bank.align_official(
            "single", TITLE, OPTIONS, "B", via="page", image_hash="h1")
        self.assertEqual(changed, 2)
        self.assertEqual(self.bank.get(first["id"])["answer"], "B")
        self.assertEqual(self.bank.get(first_twin["id"])["answer"], "B")
        self.assertEqual(self.bank.get(second["id"])["answer"], "A")

        # 反馈没带图片指纹且同题有两个不同指纹：宁可不改
        changed = self.bank.align_official("single", TITLE, OPTIONS, "C", via="page")
        self.assertEqual(changed, 0)
        self.assertEqual(self.bank.get(first["id"])["answer"], "B")
        self.assertEqual(self.bank.get(second["id"])["answer"], "A")

    def test_align_skips_multi_subject_group(self):
        first = self.add(subject="线性代数", answer="A")
        second = self.add(subject="概率论", answer="A")
        changed = self.bank.align_official("single", TITLE, OPTIONS, "B", via="page")
        self.assertEqual(changed, 0)
        self.assertEqual(self.bank.get(first["id"])["answer"], "A")
        self.assertEqual(self.bank.get(second["id"])["answer"], "A")

    def test_align_respects_page_over_paper(self):
        page = self.add(official_via="page", answer="A")
        paper = self.add(official_via="paper", answer="B")
        # paper 说 D（丁），page 说 A（甲）：保留 page，整组不动
        changed = self.bank.align_official(
            "single", TITLE, OPTIONS, "D", via="paper")
        self.assertEqual(changed, 0)
        self.assertEqual(self.bank.get(page["id"])["answer"], "A")
        self.assertEqual(self.bank.get(paper["id"])["answer"], "B")

    def test_find_official_prefers_recently_updated_duplicate(self):
        # created 更晚但 updated 更早的那条，不应再压过刚被反馈修正的那条。
        self.add(answer="A", created=300.0, updated=500.0)
        self.add(answer="B", created=400.0, updated=400.0)
        hit = self.bank.find_official("single", TITLE, OPTIONS)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["answer"], "A")


if __name__ == "__main__":
    unittest.main()

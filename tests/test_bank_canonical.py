"""P2：canonical_id —— 同一道题的多条记录只挂不删。

口径与 dedupe 的合并闸门一致：跨专业、答案冲突且无唯一官方答案、
图片选项指纹不一致的组不挂。规范记录按来源优先级选，**不动任何记录**。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bank import Bank  # noqa: E402

TITLE = "设 http://x/a.png 为三阶方阵，求行列式的值"
OPTIONS = ["正确", "错误"]


class CanonicalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "bank.db")
        self.bank = Bank(os.path.join(self.tmp.name, "bank.json"), self.db_path)
        self.addCleanup(self.bank.db.close)

    def add(self, **extra):
        payload = {
            "type": "judge",
            "title": TITLE,
            "options": list(OPTIONS),
            "answer": "正确",
            "source": "ai",
            "status": "pending",
        }
        payload.update(extra)
        return self.bank.add(payload)

    def test_ai_record_points_to_official(self):
        ai = self.add(source="ai", status="pending")
        official = self.add(source="official", status="adopted")
        self.assertEqual(official["canonical_id"], official["id"])
        self.assertEqual(self.bank.get(ai["id"])["canonical_id"], official["id"])
        # 只挂不删
        self.assertEqual(len(self.bank._items), 2)

    def test_better_record_added_later_becomes_canonical(self):
        official = self.add(source="official", status="adopted")
        self.assertEqual(self.bank.get(official["id"])["canonical_id"], official["id"])
        # 再来一条人工记录：官方仍然是规范记录
        manual = self.add(source="manual", status="adopted")
        self.assertEqual(self.bank.get(manual["id"])["canonical_id"], official["id"])
        self.assertEqual(self.bank.get(official["id"])["canonical_id"], official["id"])

    def test_answer_conflict_without_official_is_not_linked(self):
        a = self.add(source="ai", answer="正确", status="pending")
        b = self.add(source="ai", answer="错误", status="pending")
        self.assertEqual(self.bank.get(a["id"])["canonical_id"], a["id"])
        self.assertEqual(self.bank.get(b["id"])["canonical_id"], b["id"])

    def test_answer_conflict_resolved_by_official_is_linked(self):
        ai = self.add(source="ai", answer="错误", status="pending")
        official = self.add(source="official", answer="正确", status="adopted")
        self.assertEqual(self.bank.get(ai["id"])["canonical_id"], official["id"])

    def test_different_image_urls_are_different_questions(self):
        a = self.add(title="设 http://x/a.png 为三阶方阵，求行列式的值")
        b = self.add(title="设 http://x/zzz.png 为三阶方阵，求行列式的值")
        self.assertEqual(self.bank.get(a["id"])["canonical_id"], a["id"])
        self.assertEqual(self.bank.get(b["id"])["canonical_id"], b["id"])

    def test_different_image_hash_is_not_linked(self):
        a = self.add(image_hash="hash-a")
        b = self.add(image_hash="hash-b")
        self.assertEqual(self.bank.get(a["id"])["canonical_id"], a["id"])
        self.assertEqual(self.bank.get(b["id"])["canonical_id"], b["id"])

    def test_multi_subject_is_not_linked(self):
        a = self.add(subject="高数", source="official", status="adopted")
        b = self.add(subject="线代", source="official", status="adopted")
        self.assertEqual(self.bank.get(a["id"])["canonical_id"], a["id"])
        self.assertEqual(self.bank.get(b["id"])["canonical_id"], b["id"])

    def test_survives_db_round_trip(self):
        ai = self.add(source="ai", status="pending")
        official = self.add(source="official", status="adopted")
        self.bank.db.close()
        reloaded = Bank(os.path.join(self.tmp.name, "bank.json"), self.db_path)
        try:
            self.assertEqual(reloaded.get(ai["id"])["canonical_id"], official["id"])
            self.assertEqual(reloaded.get(official["id"])["canonical_id"], official["id"])
        finally:
            reloaded.db.close()


if __name__ == "__main__":
    unittest.main()

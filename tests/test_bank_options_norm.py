"""P3：`options_norm`（选项顺序归一化）与 `answer_norm`（答案语义）。

口径：
- `options_norm` —— 与 ``options`` **同序**的规范化内容（派生列）；
- `answer_norm`  —— 与选项顺序**无关**的答案语义（派生列）：
  单选/多选是所选选项的规范化内容（排序后用 ``#`` 连接），判断题是 正确/错误，
  填空是答案原文；字母越界/指向空选项时为空串（表示不可解释）；
- 对外返回的答案**仍是字母**（OCS 契约不变）—— 语义只用于比较、修复和排查。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bank import Bank, translate_answer  # noqa: E402
from app.bankdb import BankDB  # noqa: E402

TITLE = "设矩阵 A 为三阶方阵，下列说法正确的是"


class OptionsNormTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "bank.db")
        self.bank = Bank(os.path.join(self.tmp.name, "bank.json"), self.db_path)
        self.addCleanup(self.bank.db.close)

    def add(self, **extra):
        payload = {
            "type": "single",
            "title": TITLE,
            "options": ["A. 甲", "B、乙", "C．丙"],
            "answer": "B",
            "source": "manual",
            "status": "adopted",
        }
        payload.update(extra)
        return self.bank.add(payload)

    def test_options_norm_normalizes_and_keeps_order(self):
        item = self.add()
        self.assertEqual(item["options_norm"], ["甲", "乙", "丙"])
        self.assertEqual(item["answer_norm"], "乙")

    def test_multiple_answers_are_sorted_semantically(self):
        item = self.add(type="multiple", options=["x=1", "x=2", "x=3"], answer="C#A")
        self.assertEqual(item["answer_norm"], "x=1#x=3")

    def test_judge_letter_answer_maps_to_semantics_by_option_order(self):
        a = self.add(type="judge", options=["对", "错"], answer="A")
        b = self.add(type="judge", options=["错", "对"], answer="A")
        self.assertEqual(a["answer_norm"], "正确")
        # 同一字母在换序后的选项里是另一个意思 —— 这正是「按字母存答案」的危险
        self.assertEqual(b["answer_norm"], "错误")

    def test_completion_answer_is_its_own_semantics(self):
        item = self.add(type="completion", options=[], answer="3.14")
        self.assertEqual(item["options_norm"], [])
        self.assertEqual(item["answer_norm"], "3.14")

    def test_out_of_range_letter_is_unexplainable(self):
        item = self.add(options=["甲", "乙"], answer="D")
        self.assertEqual(item["answer_norm"], "")

    def test_content_form_answer_is_preserved(self):
        item = self.add(options=["1", "2", "3"], answer="3")
        self.assertEqual(item["answer_norm"], "3")

    def test_match_still_returns_letters_for_reordered_options(self):
        self.add(options=["甲", "乙", "丙"], answer="B")
        hit = self.bank.match("single", TITLE, ["丙", "乙", "甲"])
        self.assertIsNotNone(hit)
        self.assertEqual(hit["answer"], "B")  # 乙 在新顺序里是第二个

    def test_translate_content_answer_to_letters(self):
        self.assertEqual(
            translate_answer("乙", ["甲", "乙"], ["乙", "甲"], "single"), "A")
        self.assertIsNone(
            translate_answer("丁", ["甲", "乙"], ["乙", "甲"], "single"))

    def test_update_reorders_options_and_translates_answer(self):
        item = self.add(options=["甲", "乙", "丙"], answer="B")
        updated = self.bank.update(item["id"], {"options": ["乙", "甲", "丙"]})
        self.assertEqual(updated["answer"], "A")   # 乙 挪到了第一位
        self.assertEqual(updated["answer_norm"], "乙")  # 语义不变

    def test_update_drops_answer_when_option_disappears(self):
        item = self.add(options=["甲", "乙"], answer="B")
        updated = self.bank.update(item["id"], {"options": ["甲", "丙"]})
        self.assertEqual(updated["answer"], "")
        self.assertEqual(updated["answer_norm"], "")
        self.assertEqual(updated["status"], "pending")
        self.assertTrue(updated["conflict"])

    def test_update_with_explicit_answer_is_respected(self):
        item = self.add(options=["甲", "乙"], answer="A")
        updated = self.bank.update(item["id"], {"options": ["乙", "甲"], "answer": "A"})
        self.assertEqual(updated["answer"], "A")  # 调用方明确给了答案，不做翻译

    def test_canonical_links_records_with_same_semantics_different_letters(self):
        left = self.add(options=["甲", "乙"], answer="A")
        right = self.add(options=["乙", "甲"], answer="B")
        # 原始字母不同（A vs B），但都是「甲」—— 语义键让它们挂到一起
        self.assertEqual(left["answer_norm"], right["answer_norm"])
        self.assertEqual(self.bank.get(left["id"])["canonical_id"],
                         self.bank.get(right["id"])["canonical_id"])

    def test_dedupe_does_not_report_answer_conflict_for_same_semantics(self):
        self.add(options=["甲", "乙"], answer="A")
        self.add(options=["乙", "甲"], answer="B")
        result = self.bank.dedupe(dry_run=True)
        self.assertEqual(result["conflict_count"], 0)
        self.assertEqual(result["groups"], 1)

    def test_survives_db_round_trip(self):
        item = self.add()
        self.bank.db.close()
        reloaded = Bank(os.path.join(self.tmp.name, "bank.json"), self.db_path)
        try:
            got = reloaded.get(item["id"])
            self.assertEqual(got["options_norm"], ["甲", "乙", "丙"])
            self.assertEqual(got["answer_norm"], "乙")
        finally:
            reloaded.db.close()


class MigrationTests(unittest.TestCase):
    def test_old_db_gets_columns_and_bank_derives_them(self):
        old_schema = (
            "CREATE TABLE questions ("
            "id TEXT PRIMARY KEY, type TEXT, title TEXT, options TEXT, answer TEXT, subject TEXT,"
            "image_hash TEXT, sh TEXT, source TEXT, level INTEGER, via TEXT, status TEXT,"
            "history TEXT, wrong_answers TEXT, agree INTEGER, conflict INTEGER, created REAL)"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            conn = sqlite3.connect(path)
            conn.execute(old_schema)
            conn.execute(
                "INSERT INTO questions (id, type, title, options, answer, subject, image_hash, sh,"
                " source, level, via, status, history, wrong_answers, agree, conflict, created)"
                " VALUES ('q1','single','题干','[\"甲\",\"乙\"]','B','默认','','','manual',3,'',"
                "'adopted','[]','[]',0,0,123.0)"
            )
            conn.commit()
            conn.close()

            db = BankDB(path)
            try:
                rows = db.load_all()
            finally:
                db.close()
            self.assertEqual(rows[0]["options_norm"], [])  # 列已加，旧行是默认空值
            self.assertEqual(rows[0]["answer_norm"], "")

            bank = Bank(os.path.join(tmp, "bank.json"), path)
            try:
                got = bank.get("q1")
                self.assertEqual(got["options_norm"], ["甲", "乙"])
                self.assertEqual(got["answer_norm"], "乙")
            finally:
                bank.db.close()


if __name__ == "__main__":
    unittest.main()

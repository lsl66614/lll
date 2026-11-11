"""P1 题库溯源两列：`updated`（最后修改时间）与 `origin`（原始出处）。

约定：
- `updated` 只在**真有变化**的写入时刷新（add / update / record_attempt /
  mark_wrong / remember_answer）；重复作答同一答案不算修改。
- `origin` 只在入库时定一次，之后不能通过更新接口改写；老记录用当时的
  `via` 兜底（原始出处已经丢失，这是唯一可得的近似）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bank import Bank  # noqa: E402
from app.bankdb import BankDB  # noqa: E402

OLD_SCHEMA = (
    "CREATE TABLE questions ("
    "id TEXT PRIMARY KEY, type TEXT, title TEXT, options TEXT, answer TEXT, subject TEXT,"
    "image_hash TEXT, sh TEXT, source TEXT, level INTEGER, via TEXT, status TEXT,"
    "history TEXT, wrong_answers TEXT, agree INTEGER, conflict INTEGER, created REAL)"
)


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "bank.db")
        self.bank = Bank(os.path.join(self.tmp.name, "bank.json"), self.db_path)
        self.addCleanup(self.bank.db.close)

    def add_one(self, **extra):
        payload = {
            "type": "judge",
            "title": "设 http://x/a.png 为三阶方阵",
            "answer": "A",
            "source": "official",
            "status": "adopted",
        }
        payload.update(extra)
        return self.bank.add(payload)

    def test_add_sets_updated_now_and_origin_from_via(self):
        before = time.time()
        item = self.add_one(via="paper")
        self.assertGreaterEqual(item["updated"], before)
        self.assertEqual(item["origin"], "paper")

    def test_update_bumps_updated_and_keeps_origin(self):
        item = self.add_one(via="paper")
        self.bank._items[0]["updated"] = 1.0  # 便于观察是否被刷新
        self.bank._items[0]["origin"] = "paper"
        self.bank._items[0]["via"] = "agnes"  # 后续作答把 via 改掉了
        updated = self.bank.update(item["id"], {"answer": "B"})
        self.assertGreater(updated["updated"], 1.0)
        self.assertEqual(updated["origin"], "paper")   # 原始出处不跟着 via 变
        self.assertEqual(updated["via"], "agnes")

    def test_update_cannot_rewrite_origin(self):
        item = self.add_one(via="paper")
        updated = self.bank.update(item["id"], {"origin": "hacked"})
        self.assertEqual(updated["origin"], "paper")

    def test_record_attempt_bumps_updated_on_change(self):
        # 官方答案不允许被 AI 覆盖（那条路径直接返回、不算修改），
        # 所以这里用一条 AI 待确认记录来观察 updated。
        item = self.add_one(source="ai", status="pending")
        self.bank._items[0]["updated"] = 1.0
        self.bank.record_attempt(item["id"], "B", source="ai", allow_adopt=False)
        self.assertGreater(self.bank._items[0]["updated"], 1.0)

    def test_record_attempt_same_answer_does_not_bump(self):
        self.add_one(source="ai", status="adopted")
        stored = self.bank._items[0]["answer"]  # 判断题的 "A" 会被归一成语义答案
        self.bank._items[0]["updated"] = 1.0
        self.bank.record_attempt(self.bank._items[0]["id"], stored, source="ai")
        self.assertEqual(self.bank._items[0]["updated"], 1.0)

    def test_mark_wrong_bumps_updated(self):
        item = self.add_one()
        self.bank._items[0]["updated"] = 1.0
        self.bank.mark_wrong(item["id"])
        self.assertGreater(self.bank._items[0]["updated"], 1.0)

    def test_survives_db_round_trip(self):
        item = self.add_one(via="snapshot-restore")
        self.bank.db.close()
        reloaded = Bank(os.path.join(self.tmp.name, "bank.json"), self.db_path)
        try:
            got = reloaded.get(item["id"])
            self.assertEqual(got["origin"], "snapshot-restore")
            self.assertAlmostEqual(got["updated"], item["updated"], places=3)
        finally:
            reloaded.db.close()


class MigrationTests(unittest.TestCase):
    def test_old_db_gets_columns_and_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            conn = sqlite3.connect(path)
            conn.execute(OLD_SCHEMA)
            conn.execute(
                "INSERT INTO questions (id, type, title, options, answer, subject, image_hash, sh,"
                " source, level, via, status, history, wrong_answers, agree, conflict, created)"
                " VALUES ('q1','judge','题干','[]','A','默认','','','official',4,'paper','adopted',"
                " '[]','[]',0,0,123.0)"
            )
            conn.commit()
            conn.close()

            db = BankDB(path)
            try:
                rows = db.load_all()
            finally:
                db.close()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["updated"], 123.0)  # 用 created 当下界
            self.assertEqual(rows[0]["origin"], "paper")  # 老记录用 via 兜底
            self.assertEqual(rows[0]["img_key"], "")      # 上一轮迁移也照常执行


if __name__ == "__main__":
    unittest.main()

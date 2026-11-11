"""题库主库与 JSON 快照一致性检查的单元测试。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bank import Bank  # noqa: E402
from app.bank_consistency import compare, rewrite_json  # noqa: E402


class BankConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "bank.db")
        self.json_path = os.path.join(self.tmp.name, "bank.json")
        self.bank = Bank(self.json_path, self.db_path)
        self.bank.add({
            "type": "single", "title": "中国首都是？",
            "options": ["北京", "上海", "广州"], "answer": "A",
            "subject": "地理", "source": "manual", "status": "adopted",
        })
        self.bank.add({
            "type": "judge", "title": "地球是圆的。",
            "options": ["正确", "错误"], "answer": "正确",
            "subject": "地理", "source": "official", "status": "adopted",
        })
        self.bank.flush()
        self._write_snapshot()

    def tearDown(self):
        try:
            self.bank.close()
        finally:
            self.tmp.cleanup()

    def _items(self):
        return json.loads(self.bank.export_json())["items"]

    def _write_snapshot(self, items=None):
        with open(self.json_path, "w", encoding="utf-8") as fp:
            json.dump({"items": items if items is not None else self._items()},
                      fp, ensure_ascii=False, indent=2)

    def test_consistent_snapshot(self):
        report = compare(self.db_path, self.json_path)
        self.assertTrue(report["ok"])
        self.assertEqual(report["db_count"], 2)
        self.assertEqual(report["json_count"], 2)
        self.assertEqual(report["field_diffs_total"], 0)

    def test_detects_missing_and_field_drift(self):
        items = self._items()
        items[0]["answer"] = "B"
        dropped = items.pop(1)
        items.append({
            "id": "only-in-json", "type": "single", "title": "额外题",
            "options": ["对", "错"], "answer": "A", "subject": "地理",
            "source": "manual", "status": "adopted",
        })
        self._write_snapshot(items)

        report = compare(self.db_path, self.json_path)
        self.assertFalse(report["ok"])
        self.assertEqual(report["field_diffs_total"], 1)
        self.assertEqual(report["field_diffs"][0]["field"], "answer")
        self.assertEqual(report["missing_in_json"], [dropped["id"]])
        self.assertEqual(report["missing_in_db"], ["only-in-json"])

    def test_rewrite_json_uses_db_as_source(self):
        items = self._items()
        items[0]["answer"] = "B"
        self._write_snapshot(items)
        self.assertFalse(compare(self.db_path, self.json_path)["ok"])

        result = rewrite_json(self.db_path, self.json_path)
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"], 2)
        report = compare(self.db_path, self.json_path)
        self.assertTrue(report["ok"])
        # 主库没有被 JSON 反向修改
        with open(self.json_path, encoding="utf-8") as fp:
            fixed = {item["id"]: item for item in json.load(fp)["items"]}
        self.assertEqual(fixed[items[0]["id"]]["answer"], "A")
        backups = [name for name in os.listdir(self.tmp.name)
                   if name.startswith("bank.json.bak-consistency-")]
        self.assertEqual(len(backups), 1)

    def test_duplicate_ids_reported(self):
        items = self._items()
        items.append(dict(items[0]))
        self._write_snapshot(items)
        report = compare(self.db_path, self.json_path)
        self.assertFalse(report["ok"])
        self.assertEqual(report["duplicate_ids"], [items[0]["id"]])

    def test_invalid_json_returns_error(self):
        with open(self.json_path, "w", encoding="utf-8") as fp:
            fp.write("{not json")
        report = compare(self.db_path, self.json_path)
        self.assertFalse(report["ok"])
        self.assertIn("读取 JSON 失败", report["error"])


if __name__ == "__main__":
    unittest.main()

"""只读巡检器的单元测试。

巡检器不碰网络、不依赖全局 config：每个用例用临时目录建自己的 Bank 与存储。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bank import Bank  # noqa: E402
from app.inspector import Inspector, diff_findings  # noqa: E402


def _completion(bank: Bank, title: str, answer: str = "答案", source: str = "ai") -> None:
    bank.add({
        "type": "completion", "title": title, "options": [],
        "answer": answer, "source": source, "status": "adopted",
    })


class InspectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = Bank(os.path.join(self.tmp.name, "bank.json"))
        self.addCleanup(self.bank.close)
        self.inspector = Inspector(os.path.join(self.tmp.name, "inspections.json"))

    def test_empty_official_answer_is_a_finding(self):
        item = self.bank.add({
            "type": "completion", "title": "空答案官方填空题", "options": [],
            "answer": "", "source": "official", "status": "adopted",
        })
        run = self.inspector.run(self.bank, include_snapshot=False)
        ids = [f["id"] for f in run["findings"]]
        self.assertIn("official-empty", ids)
        finding = next(f for f in run["findings"] if f["id"] == "official-empty")
        self.assertEqual(finding["severity"], "P1")
        self.assertEqual(finding["metric"], 1)
        self.assertIn("空答案官方填空题", " ".join(finding["evidence"]))
        # 证据链：finding 带上题库条目 id，管理台据此直达
        self.assertEqual(finding["items"], [item["id"]])
        self.assertFalse(run["snapshot"]["included"])

    def test_conflict_and_pending_backlog(self):
        item = self.bank.add({
            "type": "completion", "title": "冲突题", "options": [],
            "answer": "甲", "source": "ai", "status": "adopted",
        })
        self.bank.update(item["id"], {"conflict": True})
        self.bank.add({
            "type": "completion", "title": "待定题", "options": [],
            "answer": "", "source": "ai", "status": "pending",
        })
        run = self.inspector.run(self.bank, include_snapshot=False)
        ids = {f["id"]: f for f in run["findings"]}
        self.assertEqual(ids["bank-conflicts"]["metric"], 1)
        self.assertEqual(ids["pending-backlog"]["severity"], "P2")

    def test_run_marks_resolved_and_persists_history(self):
        first = Bank(os.path.join(self.tmp.name, "bank1.json"))
        self.addCleanup(first.close)
        _completion(first, "先有空答案", answer="", source="official")
        run1 = self.inspector.run(first, include_snapshot=False)
        self.assertIn("official-empty", [f["id"] for f in run1["findings"]])

        second = Bank(os.path.join(self.tmp.name, "bank2.json"))
        self.addCleanup(second.close)
        _completion(second, "已经修好了")
        run2 = self.inspector.run(second, include_snapshot=False)
        self.assertEqual(run2["findings"], [])
        self.assertEqual([r["id"] for r in run2["diff"]["resolved"]], ["official-empty"])
        self.assertEqual(len(self.inspector.runs(20)), 2)
        self.assertEqual(self.inspector.latest()["at"], run2["at"])

    def test_bank_drop_is_p0(self):
        big = Bank(os.path.join(self.tmp.name, "big.json"))
        self.addCleanup(big.close)
        for i in range(20):
            _completion(big, "题 %d" % i)
        self.inspector.run(big, include_snapshot=False)

        small = Bank(os.path.join(self.tmp.name, "small.json"))
        self.addCleanup(small.close)
        for i in range(5):
            _completion(small, "题 %d" % i)
        run = self.inspector.run(small, include_snapshot=False)
        drop = next(f for f in run["findings"] if f["id"] == "bank-drop")
        self.assertEqual(drop["severity"], "P0")
        self.assertEqual(drop["metric"], 75)
        self.assertEqual(run["counts"]["P0"], 1)

    def test_diff_statuses(self):
        previous = [
            {"id": "a", "title": "A", "severity": "P1", "metric": 1},
            {"id": "b", "title": "B", "severity": "P2", "metric": 5},
            {"id": "c", "title": "C", "severity": "P2", "metric": 2},
        ]
        current = [
            {"id": "a", "title": "A", "severity": "P1", "metric": 3},
            {"id": "b", "title": "B", "severity": "P2", "metric": 1},
            {"id": "d", "title": "D", "severity": "P1", "metric": 1},
        ]
        diff = diff_findings(previous, current)
        self.assertEqual(diff["new"], ["d"])
        self.assertEqual(diff["worse"], ["a"])
        self.assertEqual(diff["better"], ["b"])
        self.assertEqual([r["id"] for r in diff["resolved"]], ["c"])
        status = {f["id"]: f["status"] for f in current}
        self.assertEqual(status, {"a": "worse", "b": "better", "d": "new"})

    def test_config_roundtrip_ignores_unknown_keys(self):
        cfg = self.inspector.set_config({"interval_hours": 12, "enabled": False, "oops": 1})
        self.assertEqual(cfg, {"enabled": False, "interval_hours": 12, "notify_p0": True})
        reloaded = Inspector(os.path.join(self.tmp.name, "inspections.json"))
        self.assertEqual(reloaded.config(), cfg)

    def test_is_due_uses_interval_and_enabled_flag(self):
        self.assertTrue(self.inspector.is_due())
        _completion(self.bank, "普通题")
        self.inspector.run(self.bank, include_snapshot=False)
        self.assertFalse(self.inspector.is_due())
        # 默认 6 小时；把上次运行时间挪到 2 小时前仍未到点
        data = self.inspector.store.read()
        data["runs"][0]["at"] = 1_000_000.0
        self.inspector.store.write(data)
        self.assertTrue(self.inspector.is_due(now=1_000_000.0 + 6 * 3600 + 1))
        self.assertFalse(self.inspector.is_due(now=1_000_000.0 + 6 * 3600 - 1))
        self.inspector.set_config({"enabled": False})
        self.assertFalse(self.inspector.is_due(now=1_000_000.0 + 999999))

    def test_summary_is_saved_on_latest_run(self):
        _completion(self.bank, "普通题")
        run = self.inspector.run(self.bank, include_snapshot=False)
        self.assertTrue(self.inspector.set_summary(run["at"], "巡检简报"))
        self.assertEqual(self.inspector.latest()["ai_summary"], "巡检简报")
        self.assertFalse(self.inspector.set_summary(0, "不存在"))


if __name__ == "__main__":
    unittest.main()

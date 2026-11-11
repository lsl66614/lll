"""位置映射（请求只有 A/B/C/D、题库是图片选项）的锚定与标记。

OCS 每次重做都会重排选项，字母只在当次顺序里成立。位置映射无法完全避免，
但必须做到两件事：
1. 能用图片指纹锚定就锚定，不能拿同名题的另一条记录凑答案；
2. 把「这是猜位置」标出来（``_positional``），调用方不能当成内容核对过的官方答案。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bank import Bank, translation_is_positional  # noqa: E402

TITLE = "行列式 A 的充分必要条件是（ ）."
URLS_A = [f"http://img.example/a{i}.png" for i in range(4)]
URLS_B = [f"http://img.example/b{i}.png" for i in range(4)]
BARE = ["A", "B", "C", "D"]


class PositionalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = Bank(
            os.path.join(self.tmp.name, "bank.json"),
            os.path.join(self.tmp.name, "bank.db"),
        )
        self.addCleanup(self.bank.db.close)

    def add(self, options, answer, image_hash="", title=TITLE):
        return self.bank.add({
            "type": "single",
            "title": title,
            "options": list(options),
            "answer": answer,
            "source": "official",
            "status": "adopted",
            "image_hash": image_hash,
        })

    def test_translation_is_positional_detects_the_guess(self):
        self.assertTrue(translation_is_positional(URLS_A, BARE))
        self.assertFalse(translation_is_positional(URLS_A, URLS_B))
        self.assertFalse(translation_is_positional(BARE, BARE))
        self.assertFalse(translation_is_positional(URLS_A, []))

    def test_image_hash_anchors_the_right_record(self):
        left = self.add(URLS_A, "A", image_hash="hashA")
        right = self.add(URLS_B, "B", image_hash="hashB")
        hit = self.bank.find_by_position("single", TITLE, BARE, "默认", image_hash="hashA")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["id"], left["id"])
        self.assertEqual(hit["answer"], "A")
        self.assertTrue(hit["_positional"])

        other = self.bank.find_by_position("single", TITLE, BARE, "默认", image_hash="hashB")
        self.assertEqual(other["id"], right["id"])
        self.assertEqual(other["answer"], "B")

    def test_without_image_hash_the_newest_record_wins(self):
        self.add(URLS_A, "A")
        second = self.add(URLS_B, "B")
        time.sleep(0.01)
        oldest = next(i for i in self.bank.all() if i["answer"] == "A")
        # 让「A」那条变成最近确认过的记录
        self.bank.update(oldest["id"], {"via": "later"})
        hit = self.bank.find_by_position("single", TITLE, BARE, "默认")
        self.assertEqual(hit["answer"], "A")
        self.assertNotEqual(hit["id"], second["id"])

    def test_find_official_marks_positional_hits(self):
        self.add(URLS_A, "C")
        hit = self.bank.find_official("single", TITLE, BARE)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["answer"], "C")
        self.assertTrue(hit.get("_positional"))

    def test_content_match_is_not_marked_positional(self):
        self.add(URLS_A, "C")
        hit = self.bank.find_official("single", TITLE, [URLS_A[2], URLS_A[0], URLS_A[1], URLS_A[3]])
        self.assertIsNotNone(hit)
        self.assertEqual(hit["answer"], "A")  # 内容 2 挪到第一位
        self.assertFalse(hit.get("_positional"))


if __name__ == "__main__":
    unittest.main()

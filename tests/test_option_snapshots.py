"""选项快照存储的单元测试。"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.option_snapshots import (  # noqa: E402
    OptionSnapshots,
    snapshot_key,
    option_fingerprint,
)


def data_url(payload: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(payload).decode()


class OptionSnapshotStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1_800_000_000.0
        self.store = OptionSnapshots(
            os.path.join(self.tmp.name, "option_snapshots.json"),
            clock=lambda: self.now,
        )
        self.images = [data_url(b"image-one"), data_url(b"image-two"), data_url(b"image-three")]
        self.title = "以下哪一项描述正确"

    def tearDown(self):
        self.tmp.cleanup()

    def _put(self, images=None, title=None):
        images = images or self.images
        return self.store.put({
            "title": title or self.title,
            "type": "single",
            "options": [
                {"letter": "A", "image": images[0]},
                {"letter": "B", "image": images[1]},
                {"letter": "C", "image": images[2]},
            ],
            "subject": "测试",
            "client_id": "c1",
        })

    def test_put_and_lookup_exact(self):
        result = self._put()
        self.assertTrue(result["ok"])
        snapshot = self.store.lookup("single", self.title, 3)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["order"], ["A", "B", "C"])
        self.assertFalse(snapshot["pending_images"])

    def test_answer_remaps_after_reorder(self):
        result = self._put()
        snapshot = self.store.lookup("single", self.title, 3)
        self.assertTrue(self.store.record_answer(result["key"], "B", snapshot["options"]))
        self.assertEqual(self.store.answer_for(snapshot)["letters"], "B")

        reordered = [self.images[1], self.images[0], self.images[2]]
        again = self._put(images=reordered)
        self.assertTrue(again["order_changed"])
        self.assertEqual(again["previous_order"], ["A", "B", "C"])
        current = self.store.lookup("single", self.title, 3)
        self.assertEqual(self.store.answer_for(current)["letters"], "A")

    def test_multiple_answer_remaps(self):
        result = self.store.put({
            "title": "多选题干",
            "type": "multiple",
            "options": [
                {"letter": "A", "image": self.images[0]},
                {"letter": "B", "image": self.images[1]},
                {"letter": "C", "image": self.images[2]},
            ],
        })
        snapshot = self.store.lookup("multiple", "多选题干", 3)
        self.assertTrue(self.store.record_answer(result["key"], "A#C", snapshot["options"]))
        reordered = [self.images[2], self.images[1], self.images[0]]
        self._put_images("多选题干", "multiple", reordered)
        current = self.store.lookup("multiple", "多选题干", 3)
        self.assertEqual(self.store.answer_for(current)["letters"], "A#C")

    def _put_images(self, title, qtype, images):
        return self.store.put({
            "title": title, "type": qtype,
            "options": [
                {"letter": "A", "image": images[0]},
                {"letter": "B", "image": images[1]},
                {"letter": "C", "image": images[2]},
            ],
        })

    def test_record_selected_by_fingerprint_remaps_after_reorder(self):
        result = self._put()
        fingerprints = [option_fingerprint("", self.images[1])]
        self.assertTrue(
            self.store.record_selected(result["key"], fingerprints, source="official"))
        snapshot = self.store.lookup("single", self.title, 3)
        answer = self.store.answer_for(snapshot)
        self.assertEqual(answer["letters"], "B")
        self.assertEqual(answer["source"], "official")

        reordered = [self.images[1], self.images[0], self.images[2]]
        self._put(images=reordered)
        current = self.store.lookup("single", self.title, 3)
        self.assertEqual(self.store.answer_for(current)["letters"], "A")

    def test_record_selected_rejects_unknown_fingerprint(self):
        result = self._put()
        self.assertFalse(self.store.record_selected(result["key"], ["t:不存在的选项"]))
        snapshot = self.store.lookup("single", self.title, 3)
        self.assertIsNone(self.store.answer_for(snapshot))

    def test_record_selected_rejects_expired_snapshot(self):
        result = self._put()
        self.now += 601
        self.assertFalse(
            self.store.record_selected(
                result["key"], [option_fingerprint("", self.images[0])]))

    def test_ambiguous_fingerprint_returns_none(self):
        result = self._put()
        snapshot = self.store.lookup("single", self.title, 3)
        self.assertTrue(self.store.record_answer(result["key"], "A", snapshot["options"]))
        duplicate = [self.images[0], self.images[0], self.images[2]]
        self._put(images=duplicate)
        current = self.store.lookup("single", self.title, 3)
        self.assertIsNone(self.store.answer_for(current))

    def test_snapshot_expires_but_answer_survives(self):
        self._put()
        self.now += 601
        self.assertIsNone(self.store.lookup("single", self.title, 3))
        self.assertEqual(self.store.stats()["snapshots"], 0)

    def test_answer_expires_after_30_days(self):
        result = self._put()
        snapshot = self.store.lookup("single", self.title, 3)
        self.store.record_answer(result["key"], "B", snapshot["options"])
        self.now += 30 * 86400 + 1
        self.assertIsNone(self.store.answer_for(snapshot))

    def test_fuzzy_title_lookup(self):
        title = "下列关于矩阵秩的说法正确的是哪一个选项"
        self._put(title=title)
        # 少一个「哪」字：规范后仍应达到 0.9
        self.assertIsNotNone(self.store.lookup("single", "下列关于矩阵秩的说法正确的是一个选项", 3))

    def test_rejects_url_image(self):
        with self.assertRaises(ValueError):
            self.store.put({
                "title": self.title, "type": "single",
                "options": [
                    {"letter": "A", "image": "https://example.com/a.png"},
                    {"letter": "B", "image": self.images[1]},
                ],
            })

    def test_rejects_oversized_image(self):
        huge = "data:image/png;base64," + base64.b64encode(b"x" * 1_300_000).decode()
        with self.assertRaises(ValueError):
            self.store.put({
                "title": self.title, "type": "single",
                "options": [
                    {"letter": "A", "image": huge},
                    {"letter": "B", "image": self.images[1]},
                ],
            })

    def test_rejects_empty_options(self):
        with self.assertRaises(ValueError):
            self.store.put({
                "title": self.title, "type": "single",
                "options": [{"letter": "A"}, {"letter": "B"}],
            })

    def test_answer_persists_across_instances(self):
        result = self._put()
        snapshot = self.store.lookup("single", self.title, 3)
        self.store.record_answer(result["key"], "B", snapshot["options"])
        again = OptionSnapshots(self.store.path, clock=lambda: self.now)
        reloaded = again.lookup("single", self.title, 3)
        self.assertIsNone(reloaded)  # 快照只在内存，重启后不保留
        # 但答案还在：同 key 的快照重新上报后可立即重映射
        again.put({
            "title": self.title, "type": "single",
            "options": [
                {"letter": "A", "image": self.images[1]},
                {"letter": "B", "image": self.images[0]},
                {"letter": "C", "image": self.images[2]},
            ],
        })
        current = again.lookup("single", self.title, 3)
        self.assertEqual(again.answer_for(current)["letters"], "A")

    def test_key_is_stable(self):
        self.assertEqual(
            snapshot_key("single", "  中国  首都 是哪里？ ", 3),
            snapshot_key("single", "中国首都是哪里", 3),
        )
        self.assertEqual(
            option_fingerprint("北京", ""),
            option_fingerprint(" 北 京 ", ""),
        )


if __name__ == "__main__":
    unittest.main()

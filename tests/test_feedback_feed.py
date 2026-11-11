"""最近错题流的去重与「每题只看最近一次」规则。"""

from __future__ import annotations

import unittest

from app.feedback_feed import FeedbackFeed


def _key(title: str, options: list[str], qtype: str = "single") -> tuple:
    return FeedbackFeed._dedupe_key(
        {"qtype": qtype, "title": title, "options": options})


def _item(answer: str, is_correct: bool, selected: list[str],
          title: str = "错题", options: list[str] | None = None) -> dict:
    return {
        "type": "single",
        "title": title,
        "options": options or ["甲", "乙", "丙", "丁"],
        "selected_letters": selected,
        "correct_letters": [answer],
        "is_correct": is_correct,
        "analysis_visible": True,
    }


class FeedbackFeedTests(unittest.TestCase):
    def test_dedupe_separates_different_option_sets(self) -> None:
        """短题干「设」相同、选项不同，是两道题，不能并成一条。"""
        self.assertNotEqual(_key("设", ["甲", "乙"]), _key("设", ["丙", "丁"]))

    def test_dedupe_merges_reordered_options(self) -> None:
        """同一道题换选项顺序仍应合并（按内容排序后比较）。"""
        self.assertEqual(_key("设", ["甲", "乙", "丙"]),
                         _key("设", ["丙", "甲", "乙"]))

    def test_dedupe_merges_option_url_host_drift(self) -> None:
        """选项图片 URL 的 host/查询串变化不应把同一道题拆成两条。"""
        self.assertEqual(
            _key("设", ["http://a.com/x.png", "乙"]),
            _key("设", ["http://b.com/x.png?q=1", "乙"]),
        )

    def test_dedupe_separates_same_title_image_with_different_options(self) -> None:
        """题干图片相同但选项不同，可能是两道小题，不能误并。"""
        title = "设 http://a.com/x.png"
        self.assertNotEqual(_key(title, ["甲", "乙"]), _key(title, ["丙", "丁"]))

    def test_recent_uses_latest_attempt_and_skips_correct(self) -> None:
        feed = FeedbackFeed("/nonexistent", "/nonexistent/cache.json")
        wrong_old = FeedbackFeed.extract([_item("A", False, ["B"])], 100, "a.html")
        wrong_new = FeedbackFeed.extract([_item("C", False, ["D"])], 200, "b.html")
        correct_new = FeedbackFeed.extract([_item("C", True, ["C"])], 300, "c.html")
        other_wrong = FeedbackFeed.extract(
            [_item("B", False, ["A"], title="另一题")], 250, "d.html")
        feed._files = {
            "a.html": {"entries": wrong_old},
            "b.html": {"entries": wrong_new},
            "c.html": {"entries": correct_new},
            "d.html": {"entries": other_wrong},
        }
        items = feed.recent(10)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "另一题")
        self.assertEqual(items[0]["answer"], "B")

    def test_recent_only_latest_wrong_attempt_is_shown(self) -> None:
        feed = FeedbackFeed("/nonexistent", "/nonexistent/cache.json")
        feed._files = {
            "a.html": {"entries": FeedbackFeed.extract(
                [_item("A", False, ["B"])], 100, "a.html")},
            "b.html": {"entries": FeedbackFeed.extract(
                [_item("C", False, ["D"])], 200, "b.html")},
        }
        items = feed.recent(10)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["answer"], "C")
        self.assertEqual(items[0]["selected"], ["D"])


if __name__ == "__main__":
    unittest.main()

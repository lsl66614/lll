"""视觉与题干完整性相关的新增测试。

覆盖三件事：
1. inline_images.extract_urls 传单个字符串也能提取（早先只接受 list，
   传字符串会逐字符迭代，导致选项图提取不到）。
2. 选项图要标出归属（[选项A的图N]），否则模型不知道哪张图对应哪个选项。
3. 题干补全：请求题干被截断时，用题库里的完整题干补上（只补题干不补答案）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bank import Bank  # noqa: E402
from app.inline_images import extract_urls, fetch_inline_images  # noqa: E402
from app.option_images import annotate_options, option_image_urls  # noqa: E402
from app.shape_key import image_key, same_question_truncated, text_key, url_fingerprint  # noqa: E402
from app.title_fill import find_full_title  # noqa: E402


class InlineImageTests(unittest.TestCase):
    def test_extract_urls_accepts_single_string(self):
        url = "http://edu-image.nosdn.127.net/_PhotoUploadUtils_abc.png"
        self.assertEqual(extract_urls(url), [url])
        self.assertEqual(extract_urls([url, "无图文字"]), [url])

    def test_extract_urls_keeps_order_and_dedupes(self):
        a = "http://x/1.png"
        b = "http://x/2.png"
        self.assertEqual(extract_urls([a, b, a]), [a, b])

    def test_fetch_inline_images_returns_empty_on_bad_url(self):
        import asyncio

        result = asyncio.run(fetch_inline_images(["http://127.0.0.1:9/none.png"]))
        self.assertEqual(result, [])

    def test_fetch_inline_images_empty_list(self):
        import asyncio

        self.assertEqual(asyncio.run(fetch_inline_images([])), [])


class OptionImageTests(unittest.TestCase):
    def test_annotate_options_marks_ownership(self):
        options = [
            "http://x/a.png",
            "文字选项",
            "http://x/b.png http://x/c.png",
        ]
        annotated, urls = annotate_options(options)
        self.assertEqual(urls, ["http://x/a.png", "http://x/b.png", "http://x/c.png"])
        self.assertIn("[选项A的图1]", annotated[0])
        self.assertEqual(annotated[1], "文字选项")
        self.assertIn("[选项C的图2]", annotated[2])
        self.assertIn("[选项C的图3]", annotated[2])

    def test_annotate_options_without_images_unchanged(self):
        options = ["甲", "乙"]
        annotated, urls = annotate_options(options)
        self.assertEqual(annotated, options)
        self.assertEqual(urls, [])

    def test_option_image_urls(self):
        self.assertEqual(
            option_image_urls(["http://x/a.png", "文字", "http://x/b.png"]),
            ["http://x/a.png", "http://x/b.png"],
        )


class ShapeKeyTests(unittest.TestCase):
    def test_text_key_strips_urls_and_fragments(self):
        key = text_key("设 http://x/a.png 为三阶方阵 http://x/b")
        self.assertNotIn("http", key)
        self.assertIn("设为三阶方阵", key)

    def test_same_question_truncated_requires_same_urls(self):
        stored = "设 http://x/a.png 为三阶方阵，求行列式的值"
        same = "设 http://x/a.png 为三阶方阵，求行列"
        other = "设 http://x/zzz.png 为三阶方阵，求行列"
        self.assertTrue(same_question_truncated(same, stored))
        self.assertFalse(same_question_truncated(other, stored))

    def test_same_question_truncated_ignores_text_only(self):
        """纯文字题不走截断容错，避免把不同题混为一谈。"""
        self.assertFalse(same_question_truncated("1+1=?", "1+1=等于几"))


class ImageKeyTests(unittest.TestCase):
    """img_key：题干图片的身份，用来区分「文字相同、公式图不同」的两道题。"""

    def test_same_text_different_images_have_different_keys(self):
        a = "设 http://x/a.png 为三阶方阵，求行列式的值"
        b = "设 http://x/b.png 为三阶方阵，求行列式的值"
        self.assertNotEqual(image_key(a), image_key(b))
        # 但它们在文字骨架上确实是同一道「题形」—— 这正是补 img_key 的理由
        self.assertEqual(text_key(a), text_key(b))

    def test_key_ignores_protocol_host_and_query(self):
        base = "http://edu-image.nosdn.127.net/abc123.png"
        variants = [
            "https://edu-image.nosdn.127.net/abc123.png",
            "http://another-cdn.example.com/abc123.png",
            "http://edu-image.nosdn.127.net/abc123.png?imageView&thumbnail=100x100",
        ]
        expected = url_fingerprint(base)
        self.assertEqual(expected, "abc123")
        for url in variants:
            self.assertEqual(url_fingerprint(url), expected, url)

    def test_key_strips_photouploadutils_prefix_case_insensitively(self):
        # 真实 URL 里是大写 P/U；判重链路会先 lower()，这里必须自己能剥掉，
        # 否则同一张图会得到两个不同的 key。
        upper = "http://edu-image.nosdn.127.net/_PhotoUploadUtils_ACF7260D-5168-4B38"
        lower = "http://edu-image.nosdn.127.net/_photouploadutils_acf7260d-5168-4b38"
        self.assertEqual(url_fingerprint(upper), "acf7260d-5168-4b38")
        self.assertEqual(url_fingerprint(upper), url_fingerprint(lower))

    def test_key_joins_multiple_images_in_order(self):
        text = "由 http://x/a.png 与 http://x/b.png 可得"
        self.assertEqual(image_key(text), "a|b")
        swapped = "由 http://x/b.png 与 http://x/a.png 可得"
        self.assertEqual(image_key(swapped), "b|a")
        self.assertNotEqual(image_key(text), image_key(swapped))

    def test_no_image_gives_empty_key(self):
        self.assertEqual(image_key("纯文字题，没有图"), "")
        self.assertEqual(image_key(""), "")
        self.assertEqual(image_key(None), "")

    def test_bank_populates_img_key_on_add(self):
        """写库链路自动补 img_key —— 派生列不靠调用方记得传。"""
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({
                "type": "judge",
                "title": "设 http://x/a.png 为三阶方阵",
                "answer": "A",
                "source": "official",
                "status": "adopted",
            })
            self.assertEqual(bank._items[0]["img_key"], "a")

    def test_img_key_survives_db_round_trip(self):
        """img_key 必须真的落到 SQLite 并被 load_all 读回来。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "bank.db")
            bank = Bank(os.path.join(tmp, "bank.json"), db_path)
            bank.add({
                "type": "judge",
                "title": "设 http://x/a.png 为三阶方阵",
                "answer": "A",
                "source": "official",
                "status": "adopted",
            })
            bank.db.close()

            reloaded = Bank(os.path.join(tmp, "bank.json"), db_path)
            try:
                self.assertEqual(len(reloaded._items), 1)
                self.assertEqual(reloaded._items[0]["img_key"], "a")
            finally:
                reloaded.db.close()


class TitleFillTests(unittest.TestCase):
    def test_find_full_title_returns_longer_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            full = ("设 http://x/a.png 为三阶方阵， http://x/b.png 将 http://x/c.png "
                    "按列分块为 http://x/d.png , 则行列式 http://x/e.png 的值为")
            bank.add({"type": "completion", "title": full, "answer": "6",
                      "source": "official", "status": "adopted"})
            # 请求只带了前半截
            truncated = full[:60]
            found = find_full_title(bank.all(), "completion", truncated)
            self.assertIsNotNone(found)
            self.assertEqual(found[0], full)

    def test_find_full_title_none_when_not_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "completion", "title": "1+1=?", "answer": "2",
                      "source": "official", "status": "adopted"})
            self.assertIsNone(find_full_title(bank.all(), "completion", "1+1=?"))

    def test_find_full_title_requires_matching_image_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "completion",
                      "title": "设 http://x/a.png 为三阶方阵，求行列式的值",
                      "answer": "6", "source": "official", "status": "adopted"})
            self.assertIsNone(find_full_title(
                bank.all(), "completion", "设 http://x/other.png 为三阶方阵，求行列"))


if __name__ == "__main__":
    unittest.main()

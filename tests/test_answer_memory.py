"""答题记忆的单元测试：召回信任边界、错题提示、选项翻译、衰减清理。"""

from __future__ import annotations

import os
import tempfile
import unittest

from app.answer_memory import AnswerMemory, question_fingerprint

NOW = 1_800_000_000.0


class AnswerMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = AnswerMemory(os.path.join(self.tmp.name, "memory.db"))

    def tearDown(self) -> None:
        self.mem.close()
        self.tmp.cleanup()

    def test_exact_recall_requires_confirmed_answer(self) -> None:
        kwargs = dict(qtype="single", title="1+1=?", options=["1", "2", "3", "4"],
                      subject="数学")
        self.mem.record(answer="B", source="ai", provider="p1", strategy="single",
                        now=NOW, **kwargs)
        self.assertIsNone(self.mem.recall(**kwargs))

        updated = self.mem.record_outcome(
            official_answer="B", ok=True, source="feedback", now=NOW + 1, **kwargs)
        self.assertGreaterEqual(updated, 1)

        hit = self.mem.recall(**kwargs)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["answer"], "B")
        self.assertEqual(hit["status"], "trusted")
        self.assertEqual(hit["correct_count"], 1)
        self.assertEqual(hit["wrong_count"], 0)

    def test_wrong_ai_answer_is_hinted_not_returned(self) -> None:
        title = ("设随机变量X的分布列为P(X=k)=1/4, k=1,2,3,4，"
                 "则E(X)=?")
        near = ("设随机变量X的分布列为P(X=k)=1/4, k=1,2,3,4，"
                "则E(X)等于?")
        options = ["1", "2", "2.5", "3"]
        self.mem.record(qtype="single", title=title, options=options, answer="A",
                        source="ai", provider="p1", strategy="single", now=NOW)
        self.mem.record_outcome(qtype="single", title=title, options=options,
                                official_answer="C", ok=False, source="feedback",
                                now=NOW + 1)

        # 旧错误答案 A 不再被召回；官方确认的 C 才是可复用答案。
        hit = self.mem.recall(qtype="single", title=title, options=options)
        self.assertEqual(hit["answer"], "C")

        hint = self.mem.hints(qtype="single", title=near, options=options)
        self.assertIn("A", hint)
        self.assertIn("C", hint)
        self.assertIn("记忆提示", hint)

    def test_hint_respects_negation_and_options(self) -> None:
        self.mem.record(qtype="single", title="下列说法正确的是",
                        options=["甲", "乙"], answer="A", now=NOW)
        self.mem.record_outcome(qtype="single", title="下列说法正确的是",
                                options=["甲", "乙"], official_answer="B", ok=False,
                                now=NOW + 1)
        self.assertEqual(
            self.mem.hints(qtype="single", title="下列说法不正确的是",
                           options=["甲", "乙"]),
            "",
        )
        self.assertEqual(
            self.mem.hints(qtype="single", title="下列说法正确的是",
                           options=["丙", "丁"]),
            "",
        )

    def test_blank_has_no_fuzzy_hint(self) -> None:
        title = "请写出圆周率小数点后第100位的数字（填数字）"
        near = "请写出圆周率小数点后第100位的数字（只填数字）"
        self.mem.record(qtype="blank", title=title, options=[], answer="9", now=NOW)
        self.mem.record_outcome(qtype="blank", title=title, options=[],
                                official_answer="7", ok=False, now=NOW + 1)
        self.assertEqual(self.mem.hints(qtype="blank", title=near, options=[]), "")

    def test_letter_translation_follows_option_order(self) -> None:
        title = "选项换序题"
        source_options = ["甲", "乙", "丙", "丁"]
        target_options = ["乙", "甲", "丙", "丁"]
        self.mem.record(qtype="single", title=title, options=source_options,
                        answer="B", now=NOW)
        self.mem.record_outcome(qtype="single", title=title, options=source_options,
                                official_answer="B", ok=True, now=NOW + 1)
        hit = self.mem.recall(qtype="single", title=title, options=target_options)
        self.assertIsNotNone(hit)
        # 源选项 B = 乙；在目标选项里乙排第一，应翻译成 A。
        self.assertEqual(hit["answer"], "A")

    def test_official_feedback_translates_to_memory_option_order(self) -> None:
        """官方反馈的选项顺序和记忆行不同：best_answer 和 episode 都要按内容翻译。"""
        title = "官方换序回写题"
        memory_options = ["甲", "乙", "丙", "丁"]
        feedback_options = ["丙", "甲", "丁", "乙"]
        # 记忆里已有的 AI 答案 A = 甲
        self.mem.record(qtype="single", title=title, options=memory_options,
                        answer="A", provider="p1", now=NOW)
        # 官方反馈顺序里 B = 甲
        self.mem.record_outcome(qtype="single", title=title, options=feedback_options,
                                official_answer="B", ok=True, now=NOW + 1)
        # 记忆按旧顺序存 A；第三种顺序 [乙,丁,甲,丙] 里甲排第三，应召回 C
        hit = self.mem.recall(qtype="single", title=title,
                              options=["乙", "丁", "甲", "丙"])
        self.assertIsNotNone(hit)
        self.assertEqual(hit["answer"], "C")
        fingerprint = question_fingerprint("single", title, memory_options)
        rows = self.mem.timeline(qkey=fingerprint["qkey"])
        self.assertEqual(rows[0]["outcome"], "correct")

    def test_wrong_hint_translates_across_option_orders(self) -> None:
        """历史错答来自另一套选项顺序时，提示里的字母要翻译到当前顺序。"""
        title = "设A为三阶方阵，且A的行列式等于零，则下列结论中正确的是"
        near = "设A为三阶方阵，且A的行列式等于零，则下列结论中正确的是（变体）"
        memory_options = ["甲", "乙", "丙", "丁"]
        feedback_options = ["丙", "甲", "丁", "乙"]
        self.mem.record(qtype="single", title=title, options=memory_options,
                        answer="A", provider="p1", now=NOW)  # 甲
        self.mem.record(qtype="single", title=title, options=feedback_options,
                        answer="C", provider="p2", now=NOW + 1)  # 丁
        # 官方 D（feedback 顺序）= 乙；两条历史答案都错
        self.mem.record_outcome(qtype="single", title=title, options=feedback_options,
                                official_answer="D", ok=False, now=NOW + 2)
        # 当前顺序 [乙,甲,丙,丁]：甲=B，丁=D
        hint = self.mem.hints(qtype="single", title=near,
                              options=["乙", "甲", "丙", "丁"])
        self.assertIn("曾判错：D", hint)
        self.assertNotIn("曾判错：C", hint)

    def test_image_identity_is_strict(self) -> None:
        title = "带图题"
        options = ["甲", "乙"]
        self.mem.record(qtype="single", title=title, options=options, answer="A",
                        image_hash="h1", now=NOW)
        self.mem.record_outcome(qtype="single", title=title, options=options,
                                official_answer="A", ok=True, image_hash="h1",
                                now=NOW + 1)
        self.assertIsNone(self.mem.recall(qtype="single", title=title, options=options,
                                          image_hash="h2"))
        self.assertIsNone(self.mem.recall(qtype="single", title=title, options=options,
                                          image_hash=""))
        self.assertEqual(
            self.mem.hints(qtype="single", title=title, options=options,
                           image_hash="h2"),
            "",
        )

    def test_feedback_without_image_only_matches_single_variant(self) -> None:
        title = "同题干多图片变体"
        options = ["甲", "乙"]
        for image_hash in ("h1", "h2"):
            self.mem.record(qtype="single", title=title, options=options, answer="A",
                            image_hash=image_hash, now=NOW)
        self.mem.record_outcome(qtype="single", title=title, options=options,
                                official_answer="B", ok=False, image_hash="",
                                now=NOW + 1)
        for image_hash in ("h1", "h2"):
            fingerprint = question_fingerprint("single", title, options, image_hash)
            rows = self.mem.timeline(qkey=fingerprint["qkey"])
            self.assertEqual(rows[0]["outcome"], "unknown")

        self.mem.forget("")
        self.mem.record(qtype="single", title=title, options=options, answer="A",
                        image_hash="h1", now=NOW + 2)
        self.mem.record_outcome(qtype="single", title=title, options=options,
                                official_answer="B", ok=False, image_hash="",
                                now=NOW + 3)
        fingerprint = question_fingerprint("single", title, options, "h1")
        rows = self.mem.timeline(qkey=fingerprint["qkey"])
        self.assertEqual(rows[0]["outcome"], "wrong")

    def test_feedback_url_identity_links_hash_episodes(self) -> None:
        """请求带 base64、结果页只有 URL 时，历史 episode 也要被判对/判错。"""
        title = ("若 http://edu-image.nosdn.127.net/_PhotoUploadUtils_abc-123.jpg "
                 "是五阶行列式")
        options = ["甲", "乙", "丙", "丁"]
        self.mem.record(qtype="single", title=title, options=options, answer="A",
                        source="ai", provider="p1", strategy="single",
                        image_hash="hash123", now=NOW)
        updated = self.mem.record_outcome(
            qtype="single", title=title, options=options,
            official_answer="B", source="page", now=NOW + 1)
        self.assertEqual(updated, 1)
        fingerprint = question_fingerprint("single", title, options, "hash123")
        rows = self.mem.timeline(qkey=fingerprint["qkey"])
        self.assertEqual(rows[0]["outcome"], "wrong")
        self.assertEqual(rows[0]["correct_answer"], "B")
        self.assertEqual(rows[0]["outcome_source"], "page")
        self.assertEqual(len(self.mem.timeline()), 1)

        state = self.mem.truth_state(qtype="single", title=title,
                                     options=options, answer="B")
        self.assertTrue(state["synced"])
        self.assertEqual(state["best"], "B")

        # 换一张图（URL 不同）不能被合并，episode 保持原判。
        other = title.replace("abc-123", "def-456")
        self.assertEqual(
            self.mem.record_outcome(qtype="single", title=other, options=options,
                                    official_answer="C", source="page", now=NOW + 2),
            0,
        )
        rows = self.mem.timeline(qkey=fingerprint["qkey"])
        self.assertEqual(rows[0]["outcome"], "wrong")
        self.assertEqual(rows[0]["correct_answer"], "B")

    def test_repeat_is_deduped(self) -> None:
        kwargs = dict(qtype="single", title="重复题", options=["甲", "乙"],
                      answer="A", source="ai", provider="p1", strategy="single")
        for _ in range(3):
            self.mem.record(now=NOW, **kwargs)
        self.assertEqual(self.mem.count(), 1)
        self.mem.record(now=NOW + 700, **kwargs)
        self.assertEqual(self.mem.count(), 2)

    def test_provider_accuracy(self) -> None:
        title = "多模型题"
        options = ["甲", "乙"]
        now = NOW
        for provider, answer in (("p1", "A"), ("p2", "A"), ("p3", "B")):
            self.mem.record(qtype="single", title=title, options=options,
                            answer=answer, source="ai", provider=provider,
                            strategy="single", now=now)
        self.mem.record_outcome(qtype="single", title=title, options=options,
                                official_answer="A", ok=False, now=now + 1)
        rows = {row["provider"]: row for row in self.mem.stats()["providers"]}
        self.assertEqual(rows["p1"]["rate"], 1.0)
        self.assertEqual(rows["p2"]["rate"], 1.0)
        self.assertEqual(rows["p3"]["rate"], 0.0)

    def test_outcome_reevaluates_all_episodes(self) -> None:
        title = "官方改答案题"
        options = ["甲", "乙"]
        self.mem.record(qtype="single", title=title, options=options, answer="A",
                        provider="p1", now=NOW)
        self.mem.record(qtype="single", title=title, options=options, answer="B",
                        provider="p2", now=NOW)
        self.mem.record_outcome(qtype="single", title=title, options=options,
                                official_answer="A", ok=True, now=NOW + 1)
        self.mem.record_outcome(qtype="single", title=title, options=options,
                                official_answer="B", ok=True, now=NOW + 2)
        rows = {row["provider"]: row for row in self.mem.stats()["providers"]}
        self.assertEqual(rows["p1"]["rate"], 0.0)
        self.assertEqual(rows["p2"]["rate"], 1.0)

    def test_manual_wrong_without_official_is_disputed(self) -> None:
        title = "人工判错题"
        options = ["甲", "乙"]
        self.mem.record(qtype="single", title=title, options=options, answer="A",
                        now=NOW)
        self.mem.record_outcome(qtype="single", title=title, options=options,
                                official_answer="", ok=False, source="manual",
                                now=NOW + 1)
        self.assertIsNone(self.mem.recall(qtype="single", title=title,
                                          options=options))
        hint = self.mem.hints(qtype="single", title=title, options=options)
        self.assertIn("A", hint)

    def test_official_feedback_seeds_memory_without_episode(self) -> None:
        kwargs = dict(qtype="single", title="2+2=?", options=["3", "4"])
        self.mem.record_outcome(official_answer="B", ok=True, source="feedback",
                                now=NOW, **kwargs)
        hit = self.mem.recall(**kwargs)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["answer"], "B")
        self.assertEqual(hit["correct_count"], 0)
        self.assertEqual(hit["wrong_count"], 0)

    def test_prune_and_forget(self) -> None:
        for title, age_days, answer in (
            ("旧未确认", 100, "A"),
            ("旧已确认", 200, "B"),
            ("新未确认", 10, "C"),
        ):
            self.mem.record(qtype="blank", title=title, options=[], answer=answer,
                            now=NOW - age_days * 86400)
        self.mem.record_outcome(qtype="blank", title="旧已确认", options=[],
                                official_answer="B", ok=True,
                                now=NOW - 199 * 86400)
        result = self.mem.prune(now=NOW, unknown_days=30, episode_days=180)
        self.assertGreaterEqual(result["unknown"], 1)
        self.assertGreaterEqual(result["old"], 1)
        self.assertEqual(self.mem.count(), 1)

        fingerprint = question_fingerprint("blank", "新未确认", [])
        self.assertEqual(self.mem.forget(fingerprint["qkey"]), 1)
        self.assertEqual(self.mem.count(), 0)

    def test_search_matches_text_key(self) -> None:
        self.mem.record(qtype="single", title="1+1=?", options=["1", "2"],
                        answer="B", now=NOW)
        results = self.mem.search("1+1", limit=5)
        self.assertTrue(results)
        self.assertGreater(results[0]["score"], 0)
        self.assertEqual(results[0]["title"], "1+1=?")

    def test_default_subject_does_not_block_recall(self) -> None:
        kwargs = dict(qtype="single", title="默认专业题", options=["甲", "乙"],
                      subject="默认")
        self.mem.record(answer="A", now=NOW, **kwargs)
        self.mem.record_outcome(official_answer="A", ok=True, now=NOW + 1, **kwargs)
        hit = self.mem.recall(qtype="single", title="默认专业题",
                              options=["甲", "乙"], subject="数学")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["answer"], "A")


if __name__ == "__main__":
    unittest.main()

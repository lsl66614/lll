"""答案质量策略的单测：路由、加权投票、置信度解析。"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import main  # noqa: E402
from app.bank import Bank, _norm_text  # noqa: E402
from app.parsing import build_messages, parse_answer_detail, parse_confidence  # noqa: E402
from app.quality import (  # noqa: E402
    OfficialCheck,
    canonical_answer,
    check_official,
    is_low_confidence,
    normalize_routes,
    provider_weight,
    route_providers,
    sample_score,
    weighted_choice,
)
from app.settings import Settings  # noqa: E402


def _provider(pid: str, name: str) -> dict:
    return {"id": pid, "name": name}


class RouteTests(unittest.TestCase):
    def test_normalize_routes(self):
        raw = {
            "single": ["a", "a", " b ", ""],
            "multiple": "c",
            "essay": ["x"],
            "judge": [str(i) for i in range(20)],
            "image": None,
        }
        routes = normalize_routes(raw)
        self.assertEqual(routes["single"], ["a", "b"])
        self.assertEqual(routes["multiple"], ["c"])
        self.assertNotIn("essay", routes)
        self.assertEqual(len(routes["judge"]), 8)
        self.assertNotIn("image", routes)
        self.assertEqual(normalize_routes("nope"), {})

    def test_route_order_and_fallback(self):
        providers = [_provider("a", "甲"), _provider("b", "乙"), _provider("c", "丙")]
        routes = normalize_routes({"single": ["c", "missing"]})
        ordered = route_providers(providers, routes, "single")
        self.assertEqual([p["id"] for p in ordered], ["c", "a", "b"])
        # 未配置的题型保持原顺序
        self.assertEqual(route_providers(providers, routes, "judge"), providers)

    def test_route_accepts_provider_name(self):
        providers = [_provider("a", "甲"), _provider("b", "乙")]
        routes = normalize_routes({"multiple": ["乙"]})
        ordered = route_providers(providers, routes, "multiple")
        self.assertEqual([p["id"] for p in ordered], ["b", "a"])

    def test_image_route_takes_precedence(self):
        providers = [_provider("a", "甲"), _provider("b", "乙"), _provider("c", "丙")]
        routes = normalize_routes({"single": ["b"], "image": ["c"]})
        ordered = route_providers(providers, routes, "single", has_images=True)
        self.assertEqual([p["id"] for p in ordered], ["c", "a", "b"])
        # 没配 image 时退回题型路由
        routes2 = normalize_routes({"single": ["b"]})
        ordered2 = route_providers(providers, routes2, "single", has_images=True)
        self.assertEqual([p["id"] for p in ordered2], ["b", "a", "c"])


class WeightTests(unittest.TestCase):
    def test_provider_weight_smoothing(self):
        self.assertEqual(provider_weight({}, "甲"), 1.0)
        self.assertEqual(provider_weight({"甲": {"ok": 4, "bad": 4}}, "甲"), 1.0)
        self.assertEqual(provider_weight({"甲": {"ok": 100, "bad": 0}}, "甲"), 1.5)
        self.assertEqual(provider_weight({"甲": {"ok": 0, "bad": 100}}, "甲"), 0.5)
        # 支持 Stats.quality 原始键
        self.assertEqual(provider_weight({"p:甲": {"ok": 100, "bad": 0}}, "甲"), 1.5)

    def test_sample_score_confidence(self):
        base = sample_score(None, "甲", None)
        self.assertEqual(base, 1.0)
        self.assertGreater(sample_score(None, "甲", 0.9), base)
        self.assertLess(sample_score(None, "甲", 0.1), base)
        self.assertEqual(sample_score(None, "甲", 0.9, use_confidence=False), base)

    def test_weighted_choice_majority(self):
        result = weighted_choice([
            ("A", "甲", None),
            ("A", "乙", None),
            ("B", "丙", None),
        ])
        self.assertEqual(result.winner, "A")
        self.assertEqual(result.counts, {"A": 2, "B": 1})
        self.assertAlmostEqual(result.share, 0.6667, places=4)

    def test_weighted_choice_confidence_breaks_tie(self):
        result = weighted_choice([
            ("A", "甲", 0.95),
            ("B", "乙", 0.10),
        ])
        self.assertEqual(result.winner, "A")

    def test_weighted_choice_history_breaks_tie(self):
        rows = {"甲": {"ok": 100, "bad": 0}, "乙": {"ok": 0, "bad": 100}}
        result = weighted_choice([("A", "甲", None), ("B", "乙", None)], rows=rows)
        self.assertEqual(result.winner, "A")
        self.assertEqual(result.counts, {"A": 1, "B": 1})

    def test_weighted_choice_empty(self):
        result = weighted_choice([("", "甲", None)])
        self.assertIsNone(result.winner)
        self.assertEqual(result.share, 0.0)

    def test_low_confidence(self):
        self.assertFalse(is_low_confidence(None, 0.6))
        self.assertFalse(is_low_confidence(0.3, 0))
        self.assertTrue(is_low_confidence(0.3, 0.6))
        self.assertFalse(is_low_confidence(0.6, 0.6))
        self.assertFalse(is_low_confidence("bad", 0.6))


class OfficialCheckTests(unittest.TestCase):
    def test_agree_keeps_official(self):
        result = check_official("正确", "正确", 3, 1.0)
        self.assertIsInstance(result, OfficialCheck)
        self.assertEqual(result.decision, "agree")
        self.assertEqual(result.answer, "正确")
        self.assertEqual(result.ai_answer, "正确")

    def test_consensus_overrides_official(self):
        result = check_official("正确", "错误", 2, 0.6667)
        self.assertEqual(result.decision, "override")
        self.assertEqual(result.answer, "错误")
        self.assertEqual(result.official_answer, "正确")
        self.assertEqual(result.reason, "ai-consensus")

    def test_single_ai_cannot_override_official(self):
        result = check_official("正确", "错误", 1, 1.0)
        self.assertEqual(result.decision, "weak")
        self.assertEqual(result.answer, "正确")

    def test_consensus_but_low_share_keeps_official(self):
        result = check_official("正确", "错误", 2, 0.49)
        self.assertEqual(result.decision, "weak")
        self.assertEqual(result.answer, "正确")

    def test_unavailable_without_ai_answer(self):
        result = check_official("正确", None, 0, 0.0)
        self.assertEqual(result.decision, "unavailable")
        self.assertEqual(result.answer, "正确")
        self.assertIsNone(result.ai_answer)

    def test_compare_ignores_whitespace_and_case(self):
        self.assertEqual(check_official("A # C", "a#c", 2, 1.0).decision, "agree")


class CanonicalAnswerTests(unittest.TestCase):
    def test_completion_numeric_equivalence(self):
        self.assertEqual(canonical_answer("completion", "1/2"),
                         canonical_answer("completion", "0.5"))
        self.assertEqual(canonical_answer("completion", "0.50"),
                         canonical_answer("completion", "0.5"))
        self.assertEqual(canonical_answer("completion", "50%"),
                         canonical_answer("completion", "0.5"))
        # 只合并精确等价，避免把 1/3 和 0.333 误判成同一个答案
        self.assertNotEqual(canonical_answer("completion", "1/3"),
                            canonical_answer("completion", "0.333"))

    def test_completion_text_normalization(self):
        self.assertEqual(canonical_answer("completion", "随机 事件"),
                         canonical_answer("completion", "随机事件"))
        self.assertEqual(canonical_answer("completion", "Ａ"),
                         canonical_answer("completion", "A"))
        self.assertEqual(canonical_answer("completion", "答案。"),
                         canonical_answer("completion", "答案"))

    def test_judge_and_choice_normalization(self):
        self.assertEqual(canonical_answer("judge", "对"),
                         canonical_answer("judge", "正确"))
        self.assertEqual(canonical_answer("judge", "不正确"), "错误")
        self.assertEqual(canonical_answer("multiple", "C#A"), "A#C")
        self.assertEqual(canonical_answer("single", "b"), "B")

    def test_official_comparison_uses_canonical(self):
        result = check_official("1/2", "0.5", 3, 1.0, qtype="completion")
        self.assertEqual(result.decision, "agree")
        self.assertEqual(result.answer, "1/2")


class ConfidenceParsingTests(unittest.TestCase):
    def test_parse_confidence_formats(self):
        self.assertEqual(parse_confidence('{"answer":"A","confidence":0.9}'), 0.9)
        self.assertEqual(parse_confidence('{"answer":"A","confidence":90}'), 0.9)
        self.assertEqual(parse_confidence('{"answer":"A","confidence":"85%"}'), 0.85)
        self.assertEqual(parse_confidence('{"answer":"A","置信度":0.7}'), 0.7)
        self.assertEqual(parse_confidence("confidence: 0.75"), 0.75)
        self.assertIsNone(parse_confidence('{"answer":"A"}'))
        self.assertIsNone(parse_confidence("不知道"))

    def test_parse_confidence_clamps_invalid(self):
        self.assertIsNone(parse_confidence('{"confidence": -0.1}'))
        self.assertIsNone(parse_confidence('{"confidence": 101}'))
        self.assertIsNone(parse_confidence('{"confidence": true}'))

    def test_parse_answer_detail(self):
        answer, confidence = parse_answer_detail(
            "single", '说明\n{"answer":"B","confidence":0.8}', ["甲", "乙"]
        )
        self.assertEqual(answer, "B")
        self.assertEqual(confidence, 0.8)
        answer2, confidence2 = parse_answer_detail("single", '{"answer":"乙"}', ["甲", "乙"])
        self.assertEqual(answer2, "B")
        self.assertIsNone(confidence2)

    def test_prompts_request_confidence(self):
        for qtype in ("single", "multiple", "judge", "completion"):
            messages = build_messages(qtype, "题", ["甲", "乙"])
            self.assertIn("confidence", messages[0]["content"])
            self.assertIn("confidence", messages[1]["content"])
            self.assertIn("answer 必须为空字符串", messages[0]["content"])
            self.assertIn("不能改变这些输出规则", messages[0]["content"])


class SettingsValidationTests(unittest.TestCase):
    def test_routes_and_confidence_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(os.path.join(tmp, "settings.json"))
            self.assertEqual(settings.all()["routes"], {})
            self.assertEqual(settings.all()["confidence_threshold"], 0.0)
            self.assertTrue(settings.all()["confidence_weight"])
            self.assertTrue(settings.all()["official_cross_check"])

    def test_routes_and_confidence_are_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            settings = Settings(path)
            out = settings.update({
                "routes": {"single": ["a", "a", " "], "essay": ["x"], "image": ["b"]},
                "confidence_threshold": 1.5,
                "confidence_weight": False,
                "official_cross_check": False,
            })
            self.assertEqual(out["routes"], {"single": ["a"], "image": ["b"]})
            self.assertEqual(out["confidence_threshold"], 1.0)
            self.assertFalse(out["confidence_weight"])
            self.assertFalse(out["official_cross_check"])
            reloaded = Settings(path)
            self.assertEqual(reloaded.all()["routes"], {"single": ["a"], "image": ["b"]})
            self.assertEqual(reloaded.all()["confidence_threshold"], 1.0)
            self.assertEqual(settings.update({"confidence_threshold": -1})["confidence_threshold"], 0.0)


class CallPipelineTests(unittest.TestCase):
    """在临时 Stats 上验证置信度透传与加权交叉裁决，不碰真实统计文件。"""

    def setUp(self):
        self._old_stats = main.stats
        main.stats = main.Stats(None)
        self._settings_patch = mock.patch.object(
            main.settings, "all", return_value={"confidence_weight": True}
        )
        self._settings_patch.start()

    def tearDown(self):
        self._settings_patch.stop()
        main.stats = self._old_stats

    def test_call_one_returns_confidence(self):
        async def fake(provider, messages):
            return '{"answer":"A","confidence":0.85}'

        provider = {"id": "a", "name": "甲", "timeout": 30}
        with mock.patch.object(main, "call_provider", fake):
            answer, confidence, _elapsed, error = asyncio.run(
                main._call_one(provider, "single", ["x", "y"], [])
            )
        self.assertEqual(answer, "A")
        self.assertEqual(confidence, 0.85)
        self.assertEqual(error, "")

    def test_cross_disagreement_uses_history_weight(self):
        async def fake(provider, messages):
            letter = "A" if provider["name"] == "甲" else "B"
            return '{"answer":"%s","confidence":0.9}' % letter

        providers = [
            {"id": "a", "name": "甲", "timeout": 30},
            {"id": "b", "name": "乙", "timeout": 30},
        ]
        main.stats.quality_bump("乙", "", True)
        with mock.patch.object(main, "call_provider", fake):
            answer, provider, _ms, _errors, meta = asyncio.run(
                main._ask_providers(providers, "single", ["x", "y"], [], cross=True)
            )
        self.assertEqual(answer, "B")
        self.assertEqual(provider["name"], "乙")
        self.assertEqual(meta["cross"], "disagree")
        self.assertEqual(meta["arbitrated"], "weight")

    def test_cross_disagreement_uses_confidence(self):
        async def fake(provider, messages):
            if provider["name"] == "甲":
                return '{"answer":"A","confidence":0.95}'
            return '{"answer":"B","confidence":0.05}'

        providers = [
            {"id": "a", "name": "甲", "timeout": 30},
            {"id": "b", "name": "乙", "timeout": 30},
        ]
        with mock.patch.object(main, "call_provider", fake):
            answer, provider, _ms, _errors, _meta = asyncio.run(
                main._ask_providers(providers, "single", ["x", "y"], [], cross=True)
            )
        self.assertEqual(answer, "A")
        self.assertEqual(provider["name"], "甲")

    def test_unit_vote_merges_equivalent_completion_answers(self):
        """1/2、0.5、0.50 是同一个答案，必须合并成一票而不是三票分歧。"""
        answers = {"甲": "1/2", "乙": "0.5", "丙": "0.50"}

        async def fake(provider, messages):
            return '{"answer":"%s","confidence":0.9}' % answers[provider["name"]]

        providers = [
            {"id": "a", "name": "甲", "timeout": 30},
            {"id": "b", "name": "乙", "timeout": 30},
            {"id": "c", "name": "丙", "timeout": 30},
        ]
        with mock.patch.object(main, "call_provider", fake):
            answer, _provider, _ms, _errors, meta = asyncio.run(
                main._unit_ask(providers, "completion", [], [])
            )
        self.assertEqual(answer, "1/2")
        self.assertEqual(meta["unit_agree"], 3)
        self.assertEqual(meta["unit_share"], 1.0)
        self.assertEqual(meta["unit_rounds"], 1)

    def test_unit_vote_second_round_when_no_consensus(self):
        """首轮三模型各执一词时，让同一批模型再答一次，用两轮样本重算。"""
        rounds = {"甲": ["A", "A"], "乙": ["B", "A"], "丙": ["C", "A"]}

        async def fake(provider, messages):
            return '{"answer":"%s","confidence":0.9}' % rounds[provider["name"]].pop(0)

        providers = [
            {"id": "a", "name": "甲", "timeout": 30},
            {"id": "b", "name": "乙", "timeout": 30},
            {"id": "c", "name": "丙", "timeout": 30},
        ]
        with mock.patch.object(main, "call_provider", fake):
            answer, _provider, _ms, _errors, meta = asyncio.run(
                main._unit_ask(providers, "single", ["x", "y", "z"], [], extra_round=True)
            )
        self.assertEqual(answer, "A")
        self.assertEqual(meta["unit_rounds"], 2)
        self.assertGreaterEqual(meta["unit_agree"], 2)
        self.assertGreaterEqual(meta["unit_share"], 0.5)


class BankQualityTests(unittest.TestCase):
    def test_math_symbols_are_not_collapsed(self):
        self.assertNotEqual(_norm_text("|A|≠0"), _norm_text("|A|=0"))
        self.assertNotEqual(_norm_text("A>B"), _norm_text("A<B"))
        self.assertNotEqual(_norm_text("1.5"), _norm_text("15"))
        self.assertNotEqual(_norm_text("第1-2章"), _norm_text("第12章"))
        self.assertEqual(_norm_text("What is X?"), "whatisx")
        self.assertEqual(_norm_text("１．５"), _norm_text("1.5"))

    def test_exact_match_distinguishes_math_signs(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            ne = bank.add({"type": "judge", "title": "若 |A|≠0 则 A 可逆",
                           "options": ["对", "错"], "answer": "正确"})
            eq = bank.add({"type": "judge", "title": "若 |A|=0 则 A 可逆",
                           "options": ["对", "错"], "answer": "错误"})
            hit_ne = bank.find_exact("judge", "若 |A|≠0 则 A 可逆", ["对", "错"])
            hit_eq = bank.find_exact("judge", "若 |A|=0 则 A 可逆", ["对", "错"])
            self.assertEqual(hit_ne["id"], ne["id"])
            self.assertEqual(hit_eq["id"], eq["id"])

    def test_upsert_calibrated_records_via(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Bank(os.path.join(tmp, "bank.json"))
            item = bank.upsert_calibrated(
                "single", "通过来源题", ["甲", "乙"], "A", None,
                adopted=False, via="模型甲",
            )
            self.assertEqual(item["via"], "模型甲")
            # 后续不带 via 更新时仍保留原来源，避免把历史归因冲掉
            again = bank.upsert_calibrated(
                "single", "通过来源题", ["甲", "乙"], "A", None, adopted=False,
            )
            self.assertEqual(again["via"], "模型甲")


if __name__ == "__main__":
    unittest.main()

"""答案可靠性分级与弃答判断。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.reliability import (  # noqa: E402
    ABSTAIN_CODE,
    UNVERIFIED,
    VERIFIED,
    annotate,
    assess_answer,
    should_abstain,
)


class AssessAnswerTests(unittest.TestCase):
    def test_official_is_verified(self):
        self.assertEqual(
            assess_answer({"source": "official", "official_check": "agree"}),
            (VERIFIED, ""),
        )

    def test_official_with_weak_check_is_unverified(self):
        status, reason = assess_answer(
            {"source": "official", "official_check": "weak"}
        )
        self.assertEqual(status, UNVERIFIED)
        self.assertIn("复核", reason)

    def test_manual_and_import_are_verified(self):
        for source in ("manual", "import"):
            self.assertEqual(assess_answer({"source": source}), (VERIFIED, ""))

    def test_memory_is_verified(self):
        self.assertEqual(assess_answer({"source": "memory"}), (VERIFIED, ""))

    def test_unit_consensus_is_verified(self):
        self.assertEqual(
            assess_answer({"source": "ai", "consensus": "unit"}),
            (VERIFIED, ""),
        )

    def test_cross_agree_is_verified(self):
        self.assertEqual(
            assess_answer({"source": "ai", "cross": "agree"}),
            (VERIFIED, ""),
        )

    def test_adopted_ai_is_verified(self):
        self.assertEqual(
            assess_answer({"source": "ai", "status": "adopted"}),
            (VERIFIED, ""),
        )

    def test_pending_bank_is_unverified(self):
        status, reason = assess_answer({"source": "bank", "status": "pending"})
        self.assertEqual(status, UNVERIFIED)
        self.assertIn("共识", reason)

    def test_risk_downgrades_verified(self):
        status, reason = assess_answer(
            {"source": "official", "official_check": "agree", "risk": True}
        )
        self.assertEqual(status, UNVERIFIED)
        self.assertIn("风险", reason)

    def test_low_confidence_is_unverified(self):
        status, reason = assess_answer({"source": "ai", "low_confidence": True})
        self.assertEqual(status, UNVERIFIED)
        self.assertIn("置信", reason)

    def test_positional_is_unverified(self):
        status, reason = assess_answer(
            {"source": "official", "match": "official-positional"}
        )
        self.assertEqual(status, UNVERIFIED)
        self.assertIn("位置", reason)

    def test_positional_can_be_ignored(self):
        self.assertEqual(
            assess_answer(
                {"source": "official", "match": "official-positional"},
                include_positional=False,
            ),
            (VERIFIED, ""),
        )

    def test_min_confidence_downgrades(self):
        status, reason = assess_answer(
            {"source": "ai", "consensus": "cross", "confidence": 0.4},
            min_confidence=0.7,
        )
        self.assertEqual(status, UNVERIFIED)
        self.assertIn("阈值", reason)

    def test_min_confidence_keeps_high_confidence(self):
        self.assertEqual(
            assess_answer(
                {"source": "ai", "consensus": "cross", "confidence": 0.9},
                min_confidence=0.7,
            ),
            (VERIFIED, ""),
        )


class AbstainTests(unittest.TestCase):
    def test_default_is_off(self):
        self.assertFalse(should_abstain(UNVERIFIED, {}))

    def test_enabled_rejects_only_unverified(self):
        opts = {"abstain_enabled": True}
        self.assertTrue(should_abstain(UNVERIFIED, opts))
        self.assertFalse(should_abstain(VERIFIED, opts))

    def test_annotate_sets_fields(self):
        data = {"source": "ai"}
        reliability, reason, reject = annotate(data, {"abstain_enabled": True})
        self.assertEqual(reliability, UNVERIFIED)
        self.assertTrue(reject)
        self.assertEqual(data["reliability"], UNVERIFIED)
        self.assertTrue(data["reliability_reason"])
        self.assertEqual(ABSTAIN_CODE, 50003)

    def test_annotate_verified_has_no_reason(self):
        data = {"source": "official"}
        reliability, reason, reject = annotate(data, {"abstain_enabled": True})
        self.assertEqual(reliability, VERIFIED)
        self.assertFalse(reject)
        self.assertNotIn("reliability_reason", data)


if __name__ == "__main__":
    unittest.main()

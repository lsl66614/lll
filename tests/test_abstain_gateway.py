"""弃答策略在 _resolve 最后一层的集成测试。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OCS_DATA_DIR", tempfile.mkdtemp(prefix="ocs-abstain-test-"))

from fastapi.responses import JSONResponse  # noqa: E402

from app import main  # noqa: E402
from app.reliability import ABSTAIN_CODE  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="ocs-abstain-settings-")


class AbstainGatewayTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)

    def setUp(self):
        main.settings = main.Settings(os.path.join(_TMP, "settings.json"))
        main.settings.update({"abstain_enabled": False, "abstain_min_confidence": 0})

    def _run(self, data, **settings):
        main.settings.update(settings)

        async def fake(title, qtype, options, images=None, prompt_title=None,
                       prompt_options_raw=None, subject=None, context=None):
            return JSONResponse({"code": 0, "data": dict(data)})

        with mock.patch.object(main, "_resolve_core", side_effect=fake), \
                mock.patch.object(main, "_record_answer") as recorded, \
                mock.patch.object(main, "_record_failure") as failed:
            response = asyncio.run(main._resolve(
                "弃答测试题 " + str(id(data)), "single", ["甲", "乙"]
            ))
            payload = json.loads(response.body.decode("utf-8"))
            return response, payload, recorded, failed

    def test_unverified_answer_is_rejected_when_enabled(self):
        response, payload, recorded, failed = self._run(
            {"answer": "A", "source": "ai", "reliability": "unverified"},
            abstain_enabled=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["code"], ABSTAIN_CODE)
        self.assertIn("可靠性", payload["message"])
        recorded.assert_not_called()
        failed.assert_called_once()

    def test_verified_answer_passes_with_reliability(self):
        _response, payload, recorded, failed = self._run(
            {"answer": "A", "source": "official", "official_check": "agree"},
            abstain_enabled=True,
        )
        self.assertEqual(payload["code"], 0)
        self.assertEqual(payload["data"]["reliability"], "verified")
        recorded.assert_called_once()
        failed.assert_not_called()

    def test_default_off_keeps_unverified_answer(self):
        _response, payload, recorded, _failed = self._run(
            {"answer": "A", "source": "ai"},
            abstain_enabled=False,
        )
        self.assertEqual(payload["code"], 0)
        self.assertEqual(payload["data"]["reliability"], "unverified")
        recorded.assert_called_once()

    def test_confidence_threshold_rejects(self):
        _response, payload, _recorded, _failed = self._run(
            {"answer": "A", "source": "ai", "consensus": "unit", "confidence": 0.3},
            abstain_enabled=True,
            abstain_min_confidence=0.7,
        )
        self.assertEqual(payload["code"], ABSTAIN_CODE)

    def test_positional_answer_is_rejected(self):
        _response, payload, _recorded, _failed = self._run(
            {"answer": "A", "source": "official", "match": "official-positional"},
            abstain_enabled=True,
        )
        self.assertEqual(payload["code"], ABSTAIN_CODE)


if __name__ == "__main__":
    unittest.main()

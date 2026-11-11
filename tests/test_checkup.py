"""题库体检：分类规则缓存必须随文件更新而失效。"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock

from app import checkup, config
from app.subjects import SubjectRules


class CheckupRulesReloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "subjects.json")
        self.patch = mock.patch.object(config, "SUBJECTS_PATH", self.path)
        self.patch.start()
        self._saved_rules = checkup._subject_rules
        self._saved_sig = checkup._subject_rules_sig
        checkup._subject_rules = None
        checkup._subject_rules_sig = None
        SubjectRules(self.path).replace([{"name": "甲专业", "keywords": ["量子"]}])

    def tearDown(self):
        checkup._subject_rules = self._saved_rules
        checkup._subject_rules_sig = self._saved_sig
        self.patch.stop()
        self.tmp.cleanup()

    def test_rules_reload_after_file_change(self):
        self.assertEqual(checkup._pick_subject("量子力学基础", []), "甲专业")

        SubjectRules(self.path).replace([{"name": "乙专业", "keywords": ["量子"]}])
        # 明确推进 mtime，避免文件系统时间精度掩盖变更
        bumped = time.time() + 2
        os.utime(self.path, (bumped, bumped))

        self.assertEqual(checkup._pick_subject("量子力学基础", []), "乙专业")

    def test_rules_reuse_when_file_unchanged(self):
        first = checkup._rules()
        second = checkup._rules()
        self.assertIs(first, second)


if __name__ == "__main__":
    unittest.main()

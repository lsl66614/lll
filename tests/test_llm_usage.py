"""LLM usage 解析：兼容 chat_completions 与 responses 两种返回。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.llm import _extract_usage  # noqa: E402


class ExtractUsageTests(unittest.TestCase):
    def test_chat_completions(self):
        data = {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        self.assertEqual(
            _extract_usage(data, "chat_completions"),
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    def test_responses(self):
        data = {"usage": {"input_tokens": 20, "output_tokens": 8, "total_tokens": 28}}
        self.assertEqual(
            _extract_usage(data, "responses"),
            {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
        )

    def test_total_is_derived(self):
        data = {"usage": {"prompt_tokens": 7, "completion_tokens": 3}}
        self.assertEqual(_extract_usage(data, "chat_completions")["total_tokens"], 10)

    def test_missing_or_invalid(self):
        self.assertIsNone(_extract_usage({}, "chat_completions"))
        self.assertIsNone(_extract_usage({"usage": None}, "chat_completions"))
        self.assertIsNone(_extract_usage({"usage": {"prompt_tokens": "x"}}, "chat_completions"))


if __name__ == "__main__":
    unittest.main()

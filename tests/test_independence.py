"""投票独立性：同一模型的多 Key / 多 Base URL 只算一票。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.independence import independent_providers, provider_identity  # noqa: E402


class ProviderIdentityTests(unittest.TestCase):
    def test_model_name_is_the_identity(self):
        self.assertEqual(
            provider_identity({"model": "DeepSeek-Chat", "base_url": "https://a/v1"}),
            provider_identity({"model": "deepseek-chat", "base_url": "https://b/v1"}),
        )

    def test_base_url_is_used_when_model_is_missing(self):
        self.assertEqual(
            provider_identity({"base_url": "https://API.example.com/v1/"}),
            provider_identity({"base_url": "https://api.example.com/v1"}),
        )

    def test_id_is_the_last_resort(self):
        self.assertEqual(provider_identity({"id": "p1"}), "id:p1")
        self.assertEqual(provider_identity({"name": "备用"}), "id:备用")


class IndependentProviderTests(unittest.TestCase):
    def test_duplicate_model_keys_collapse(self):
        providers = [
            {"id": "a", "name": "A", "model": "deepseek-chat", "base_url": "https://one/v1"},
            {"id": "b", "name": "B", "model": "deepseek-chat", "base_url": "https://two/v1"},
            {"id": "c", "name": "C", "model": "qwen-plus", "base_url": "https://two/v1"},
        ]
        picked = independent_providers(providers)
        self.assertEqual([p["id"] for p in picked], ["a", "c"])

    def test_order_is_preserved(self):
        providers = [
            {"id": "z", "model": "m-z"},
            {"id": "a", "model": "m-a"},
            {"id": "z2", "model": "M-Z"},
        ]
        self.assertEqual([p["id"] for p in independent_providers(providers)], ["z", "a"])

    def test_missing_model_falls_back_to_base_url(self):
        providers = [
            {"id": "a", "base_url": "https://api.example.com/v1"},
            {"id": "b", "base_url": "https://api.example.com/v1/"},
            {"id": "c", "base_url": "https://other.example.com/v1"},
        ]
        self.assertEqual([p["id"] for p in independent_providers(providers)], ["a", "c"])

    def test_empty_input(self):
        self.assertEqual(independent_providers([]), [])


if __name__ == "__main__":
    unittest.main()

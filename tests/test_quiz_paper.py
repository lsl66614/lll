"""官方卷 DTO 留档：用于分数页 showAnalysis=false 时定位错题。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app import main
from app.main import _quiz_paper_digest


class QuizPaperDigestTest(unittest.TestCase):
    def test_keeps_scores_and_truncates_strings(self):
        paper = {
            "tname": "第三章 单元测试",
            "showAnalysis": False,
            "submitStatus": 2,
            "objectiveQList": [
                {
                    "id": 1,
                    "type": 1,
                    "score": 2,
                    "userScore": 0,
                    "stdAnswer": "B",
                    "title": "x" * 500,
                    "optionDtos": [
                        {"content": "A", "answer": False},
                        {"content": "B", "answer": True},
                    ],
                    "nested": {"a": 1},
                }
            ],
        }
        digest = _quiz_paper_digest(paper)
        self.assertEqual(digest["name"], "第三章 单元测试")
        self.assertFalse(digest["showAnalysis"])
        self.assertEqual(digest["question_count"], 1)
        self.assertIn("userScore", digest["question_keys"])
        question = digest["questions"][0]
        self.assertEqual(question["userScore"], 0)
        self.assertEqual(question["stdAnswer"], "B")
        self.assertEqual(len(question["title"]), 200)
        self.assertNotIn("nested", question)
        self.assertTrue(question["optionDtos"][1]["answer"])

    def test_empty_paper_is_safe(self):
        digest = _quiz_paper_digest({})
        self.assertEqual(digest["question_count"], 0)
        self.assertEqual(digest["questions"], [])
        self.assertEqual(digest["question_keys"], [])


class QuizPaperEndpointTest(unittest.TestCase):
    def test_post_paper_saves_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(main.config, "DATA_DIR", tmp), \
                    mock.patch.object(main.config, "GATEWAY_TOKEN", ""):
                client = TestClient(main.app)
                response = client.post(
                    "/api/quiz/paper",
                    json={
                        "tid": "1258789618",
                        "aid": "3552991279",
                        "url": "https://www.icourse163.org/x#/learn/quizscore?id=1258789618",
                        "paper": {
                            "tname": "第三章 单元测试",
                            "showAnalysis": False,
                            "submitStatus": 2,
                            "objectiveQList": [
                                {"id": 1, "score": 2, "userScore": 2},
                                {"id": 2, "score": 2, "userScore": 0},
                            ],
                        },
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()
                self.assertTrue(data["ok"])
                self.assertEqual(data["questions"], 2)
                path = os.path.join(tmp, "quiz_papers", data["file"])
                self.assertTrue(os.path.exists(path))
                with open(path, encoding="utf-8") as fp:
                    saved = json.load(fp)
                self.assertEqual(saved["digest"]["questions"][1]["userScore"], 0)
                self.assertEqual(saved["tid"], "1258789618")

    def test_rejects_non_object_paper(self):
        with mock.patch.object(main.config, "GATEWAY_TOKEN", ""):
            client = TestClient(main.app)
            response = client.post("/api/quiz/paper", json={"paper": "not-an-object"})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()

"""盲测基准的用例加载、判定、汇总与报告。"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import benchmark  # noqa: E402


def _case(**overrides):
    base = {
        "id": "q1",
        "type": "single",
        "title": "首都是哪里",
        "options": ["北京", "上海"],
        "answer": "A",
    }
    base.update(overrides)
    return base


class LoadCasesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bench-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, payload):
        path = os.path.join(self.tmp, "cases.json")
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False)
        return path

    def test_top_level_list(self):
        cases, meta = benchmark.load_cases(self._write([_case()]))
        self.assertEqual(meta, {})
        self.assertEqual(cases[0]["truth"], ("北京",))

    def test_questions_object(self):
        path = self._write({"name": "x", "questions": [_case()]})
        cases, meta = benchmark.load_cases(path)
        self.assertEqual(meta["name"], "x")
        self.assertEqual(len(cases), 1)

    def test_multiple_answer_list(self):
        case = benchmark.normalize_case(
            _case(type="multiple", options=["2", "4", "5"], answer=["A", "C"])
        )
        self.assertEqual(case["answer"], "A#C")
        self.assertEqual(case["truth"], ("2", "5"))

    def test_judge_text_answer(self):
        case = benchmark.normalize_case(
            _case(type="judge", options=["正确", "错误"], answer="正确")
        )
        self.assertEqual(case["truth"], ("正确",))

    def test_judge_letter_answer(self):
        case = benchmark.normalize_case(
            _case(type="judge", options=["正确", "错误"], answer="B")
        )
        self.assertEqual(case["truth"], ("错误",))

    def test_completion_answer_is_kept(self):
        case = benchmark.normalize_case(
            _case(type="completion", options=[], answer="3/4")
        )
        self.assertEqual(case["truth"], ("3/4",))

    def test_text_answer_maps_to_option(self):
        case = benchmark.normalize_case(_case(answer="上海"))
        self.assertEqual(case["truth"], ("上海",))

    def test_missing_field_is_rejected(self):
        bad = _case()
        bad.pop("title")
        with self.assertRaises(ValueError):
            benchmark.load_cases(self._write([bad]))

    def test_unknown_type_is_rejected(self):
        with self.assertRaises(ValueError):
            benchmark.load_cases(self._write([_case(type="essay")]))

    def test_duplicate_id_is_rejected(self):
        with self.assertRaises(ValueError):
            benchmark.load_cases(self._write([_case(), _case()]))

    def test_letter_out_of_range_is_rejected(self):
        with self.assertRaises(ValueError):
            benchmark.normalize_case(_case(answer="D"))


class JudgePayloadTests(unittest.TestCase):
    def setUp(self):
        self.case = benchmark.normalize_case(_case())

    def test_correct(self):
        row = benchmark.judge_payload(self.case, {
            "code": 0, "data": {"answer": "A", "source": "ai", "elapsed_ms": 12},
        })
        self.assertEqual(row["outcome"], "correct")
        self.assertEqual(row["elapsed_ms"], 12)

    def test_wrong(self):
        row = benchmark.judge_payload(self.case, {
            "code": 0, "data": {"answer": "B", "source": "ai"},
        })
        self.assertEqual(row["outcome"], "wrong")

    def test_abstained(self):
        row = benchmark.judge_payload(self.case, {
            "code": benchmark.ABSTAIN_CODE, "msg": "可靠性不足",
        })
        self.assertEqual(row["outcome"], "abstained")
        self.assertIn("可靠性", row["error"])

    def test_failed(self):
        row = benchmark.judge_payload(self.case, {"code": 50001, "msg": "全失败"})
        self.assertEqual(row["outcome"], "failed")
        self.assertEqual(row["error"], "全失败")

    def test_empty_answer(self):
        row = benchmark.judge_payload(self.case, {"code": 0, "data": {}})
        self.assertEqual(row["outcome"], "empty")

    def test_bad_payload(self):
        row = benchmark.judge_payload(self.case, "not-json")
        self.assertEqual(row["outcome"], "error")


class RunAndSummarizeTests(unittest.TestCase):
    def setUp(self):
        self.cases = [
            benchmark.normalize_case(_case(id="a")),
            benchmark.normalize_case(_case(id="b")),
            benchmark.normalize_case(_case(id="c", type="completion", options=[], answer="3/4")),
        ]

    def test_run_cases_collects_outcomes(self):
        answers = {"a": "A", "b": "B", "c": "3/4"}

        def ask(case):
            return {"code": 0, "data": {"answer": answers[case["id"]], "source": "ai"}}

        results = benchmark.run_cases(self.cases, ask, concurrency=2)
        self.assertEqual([row["outcome"] for row in results], ["correct", "wrong", "correct"])

    def test_run_cases_isolates_errors(self):
        def ask(case):
            if case["id"] == "b":
                raise RuntimeError("boom")
            return {"code": 0, "data": {"answer": "A"}}

        results = benchmark.run_cases(self.cases[:2], ask, concurrency=2)
        self.assertEqual(results[0]["outcome"], "correct")
        self.assertEqual(results[1]["outcome"], "error")
        self.assertIn("boom", results[1]["error"])

    def test_summary_accuracy_excludes_failures(self):
        results = [
            {"id": "a", "type": "single", "outcome": "correct", "source": "ai", "elapsed_ms": 10},
            {"id": "b", "type": "single", "outcome": "wrong", "source": "ai", "elapsed_ms": 20},
            {"id": "c", "type": "completion", "outcome": "abstained", "source": "ai", "elapsed_ms": 30},
            {"id": "d", "type": "completion", "outcome": "failed", "source": "", "elapsed_ms": 0},
        ]
        summary = benchmark.summarize(results)
        overall = summary["overall"]
        self.assertEqual(overall["total"], 4)
        self.assertEqual(overall["solved"], 2)
        self.assertEqual(overall["accuracy"], 0.5)
        self.assertEqual(overall["coverage"], 0.5)
        self.assertEqual(summary["by_type"]["single"]["accuracy"], 0.5)
        self.assertEqual(summary["latency_ms"]["p50"], 20.0)

    def test_markdown_contains_tables(self):
        summary = benchmark.summarize([
            {"id": "a", "type": "single", "outcome": "correct", "source": "ai", "elapsed_ms": 5},
        ])
        text = benchmark.render_markdown(summary)
        self.assertIn("答题盲测报告", text)
        self.assertIn("| 总题数 |", text)
        self.assertIn("| single |", text)

    def test_write_reports(self):
        tmp = tempfile.mkdtemp(prefix="bench-report-")
        try:
            summary = benchmark.summarize([
                {"id": "a", "type": "single", "outcome": "correct", "source": "ai"},
            ])
            json_path, md_path = benchmark.write_reports(summary, [], tmp)
            self.assertTrue(os.path.exists(json_path))
            self.assertTrue(os.path.exists(md_path))
            with open(json_path, encoding="utf-8") as fp:
                self.assertIn("summary", json.load(fp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

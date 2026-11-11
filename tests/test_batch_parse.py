"""`app.batch_parse` 的多题 JSON 容错解析测试。

覆盖计划里的 14 类坏样本：围栏、前后废话、尾逗号、中文/单引号、裸 key、
截断、映射结构、null/数字/布尔/数组答案、重复 id、纯垃圾输出等。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.batch_parse import parse_answer_array  # noqa: E402
from app.parsing import parse_answer_detail  # noqa: E402

OK_JSON = (
    '{"answers":['
    '{"id":"q1","answer":"A","confidence":0.9},'
    '{"id":"q2","answer":["A","C"],"confidence":"85%"}'
    "]}"
)


class BatchParseTests(unittest.TestCase):
    def test_plain_json(self):
        hits, missing = parse_answer_array(OK_JSON, ["q1", "q2"])
        self.assertEqual(hits["q1"], {"answer": "A", "confidence": 0.9})
        self.assertEqual(hits["q2"], {"answer": "A#C", "confidence": 0.85})
        self.assertEqual(missing, [])

    def test_markdown_fence(self):
        raw = "```json\n" + OK_JSON + "\n```"
        hits, missing = parse_answer_array(raw, ["q1", "q2"])
        self.assertEqual(hits["q1"]["answer"], "A")
        self.assertEqual(missing, [])

    def test_prose_around_json(self):
        raw = "好的，答案如下：\n" + OK_JSON + "\n以上，请核对。"
        hits, missing = parse_answer_array(raw, ["q1", "q2"])
        self.assertEqual(hits["q2"]["answer"], "A#C")
        self.assertEqual(missing, [])

    def test_trailing_comma(self):
        raw = '{"answers":[{"id":"q1","answer":"A",},]}'
        hits, missing = parse_answer_array(raw, ["q1"])
        self.assertEqual(hits["q1"]["answer"], "A")
        self.assertEqual(missing, [])

    def test_single_quotes(self):
        raw = "{'answers': [{'id': 'q1', 'answer': 'B'}]}"
        hits, missing = parse_answer_array(raw, ["q1"])
        self.assertEqual(hits["q1"]["answer"], "B")
        self.assertEqual(missing, [])

    def test_chinese_quotes(self):
        raw = "{“answers”:[{“id”:“q1”,“answer”:“C”}]}"
        hits, missing = parse_answer_array(raw, ["q1"])
        self.assertEqual(hits["q1"]["answer"], "C")
        self.assertEqual(missing, [])

    def test_unquoted_keys(self):
        raw = '{answers:[{id:"q1",answer:"D"}]}'
        hits, missing = parse_answer_array(raw, ["q1"])
        self.assertEqual(hits["q1"]["answer"], "D")
        self.assertEqual(missing, [])

    def test_brace_inside_answer_is_not_corrupted(self):
        raw = '{answers:[{id:"q1",answer:"x{a:b}"}]}'
        hits, _missing = parse_answer_array(raw, ["q1"])
        self.assertEqual(hits["q1"]["answer"], "x{a:b}")

    def test_truncated_recovers_complete_items(self):
        raw = '{"answers":[{"id":"q1","answer":"A"},{"id":"q2","ans'
        hits, missing = parse_answer_array(raw, ["q1", "q2"])
        self.assertEqual(hits["q1"]["answer"], "A")
        self.assertNotIn("q1", missing)
        self.assertIn("q2", missing)

    def test_missing_comma_between_objects(self):
        raw = '{"answers":[{"id":"q1","answer":"A"} {"id":"q2","answer":"B"}]}'
        hits, missing = parse_answer_array(raw, ["q1", "q2"])
        self.assertEqual(hits["q1"]["answer"], "A")
        self.assertEqual(hits["q2"]["answer"], "B")
        self.assertEqual(missing, [])

    def test_id_to_answer_mapping(self):
        hits, missing = parse_answer_array('{"q1":"A","q2":"B"}', ["q1", "q2"])
        self.assertEqual(hits["q1"]["answer"], "A")
        self.assertEqual(hits["q2"]["answer"], "B")
        self.assertEqual(missing, [])

    def test_mapping_inside_answers_key(self):
        hits, _missing = parse_answer_array('{"answers":{"q1":"A"}}', ["q1"])
        self.assertEqual(hits["q1"]["answer"], "A")

    def test_top_level_array(self):
        hits, _missing = parse_answer_array('[{"id":"q1","answer":"A"}]', ["q1"])
        self.assertEqual(hits["q1"]["answer"], "A")

    def test_data_container(self):
        hits, _missing = parse_answer_array('{"data":[{"id":"q1","answer":"A"}]}', ["q1"])
        self.assertEqual(hits["q1"]["answer"], "A")

    def test_null_answer_counts_as_missing(self):
        raw = '{"answers":[{"id":"q1","answer":"A"},{"id":"q2","answer":null}]}'
        hits, missing = parse_answer_array(raw, ["q1", "q2"])
        self.assertEqual(hits["q2"]["answer"], "")
        self.assertEqual(missing, ["q2"])

    def test_numeric_answer(self):
        raw = '{"answers":[{"id":"q1","answer":1.0},{"id":"q2","answer":42}]}'
        hits, _missing = parse_answer_array(raw, ["q1", "q2"])
        self.assertEqual(hits["q1"]["answer"], "1")
        self.assertEqual(hits["q2"]["answer"], "42")

    def test_boolean_answer(self):
        raw = '{"answers":[{"id":"q1","answer":true}]}'
        hits, _missing = parse_answer_array(raw, ["q1"])
        self.assertEqual(hits["q1"]["answer"], "true")

    def test_duplicate_id_last_wins(self):
        raw = (
            '{"answers":[{"id":"q1","answer":"A"},{"id":"q1","answer":"B"}]}'
        )
        hits, _missing = parse_answer_array(raw, ["q1"])
        self.assertEqual(hits["q1"]["answer"], "B")

    def test_integer_id_is_stringified(self):
        raw = '{"answers":[{"id":1,"answer":"A"}]}'
        hits, missing = parse_answer_array(raw, ["1"])
        self.assertEqual(hits["1"]["answer"], "A")
        self.assertEqual(missing, [])

    def test_extra_ids_are_filtered(self):
        raw = '{"answers":[{"id":"q1","answer":"A"},{"id":"q9","answer":"Z"}]}'
        hits, missing = parse_answer_array(raw, ["q1"])
        self.assertNotIn("q9", hits)
        self.assertEqual(missing, [])

    def test_alias_keys(self):
        raw = '{"answers":[{"qid":"q1","ans":"B","conf":0.7}]}'
        hits, _missing = parse_answer_array(raw, ["q1"])
        self.assertEqual(hits["q1"], {"answer": "B", "confidence": 0.7})

    def test_regex_fallback_without_valid_json(self):
        raw = '"id":"q1","answer":"A"  "id":"q2","answer":"B"'
        hits, missing = parse_answer_array(raw, ["q1", "q2"])
        self.assertEqual(hits["q1"]["answer"], "A")
        self.assertEqual(hits["q2"]["answer"], "B")
        self.assertEqual(missing, [])

    def test_garbage_returns_all_missing(self):
        hits, missing = parse_answer_array("模型今天不想答题。", ["q1", "q2"])
        self.assertEqual(hits, {})
        self.assertEqual(missing, ["q1", "q2"])

    def test_empty_input(self):
        hits, missing = parse_answer_array("", ["q1"])
        self.assertEqual(hits, {})
        self.assertEqual(missing, ["q1"])

    def test_without_expected_returns_all_ids(self):
        hits, missing = parse_answer_array(OK_JSON)
        self.assertEqual(set(hits), {"q1", "q2"})
        self.assertEqual(missing, [])

    def test_chain_with_question_type(self):
        raw = (
            '{"answers":['
            '{"id":"q1","answer":"b"},'
            '{"id":"q2","answer":["A","C"]},'
            '{"id":"q3","answer":true},'
            '{"id":"q4","answer":12}'
            "]}"
        )
        hits, missing = parse_answer_array(raw, ["q1", "q2", "q3", "q4"])
        self.assertEqual(missing, [])
        self.assertEqual(parse_answer_detail("single", hits["q1"]["answer"], ["x", "y"])[0], "B")
        self.assertEqual(
            parse_answer_detail("multiple", hits["q2"]["answer"], ["x", "y", "z"])[0],
            "A#C",
        )
        self.assertEqual(
            parse_answer_detail("judge", hits["q3"]["answer"], ["正确", "错误"])[0],
            "正确",
        )
        self.assertEqual(parse_answer_detail("completion", hits["q4"]["answer"], [])[0], "12")


if __name__ == "__main__":
    unittest.main()

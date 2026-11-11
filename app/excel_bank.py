"""考试宝兼容 Excel 导入：把「chaoxing-quiz-tool」导出的题库转成网关条目。

格式（第 1 行空、第 2 行表头，共 14 列）：
题干 | 题型 | 选项A..选项H | 正确答案 | 解析 | 章节 | 难度
"""

from __future__ import annotations

import html as html_lib
import io
import re
from typing import Any

from .parsing import parse_options

TYPE_MAP = {
    "单选题": "single",
    "单项选择": "single",
    "多选题": "multiple",
    "多项选择": "multiple",
    "不定项选择题": "multiple",
    "不定项": "multiple",
    "判断题": "judge",
    "填空题": "completion",
}

_TRUE = {"对", "正确", "√", "✓", "是", "true", "t"}
_FALSE = {"错", "错误", "×", "✗", "否", "false", "f"}
_LETTERS = "ABCDEFGH"


def _clean(value: Any) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text).replace("\u00a0", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def _norm_type(value: str) -> str | None:
    text = _clean(value)
    for name, code in TYPE_MAP.items():
        if name in text:
            return code
    return None


def _letters_from(answer: str, options: list[str]) -> str | None:
    raw = answer.strip().upper()
    if not raw:
        return None
    found = sorted({ch for ch in raw if ch in _LETTERS})
    if found and re.fullmatch(r"[A-H\s,，、;；|/\\#-]+", raw):
        return "#".join(found)
    # 答案写成了选项原文：反查字母
    target = re.sub(r"\s+", "", answer)
    for index, option in enumerate(options):
        if re.sub(r"\s+", "", option) == target:
            return _LETTERS[index]
    return None


def _judge_answer(answer: str, options: list[str]) -> str | None:
    text = _clean(answer)
    if text in _TRUE or text.lower() in _TRUE:
        return "正确"
    if text in _FALSE or text.lower() in _FALSE:
        return "错误"
    for index, option in enumerate(options[:2]):
        if _clean(option) == text:
            return "正确" if index == 0 else "错误"
    return None


def _find_header(rows: list[tuple]) -> tuple[int, dict[str, int]]:
    for row_index, row in enumerate(rows[:6]):
        cells = [_clean(cell) for cell in row]
        if any("题干" in cell for cell in cells) and any("正确答案" in cell for cell in cells):
            mapping: dict[str, int] = {}
            for index, cell in enumerate(cells):
                if "题干" in cell:
                    mapping["title"] = index
                elif "题型" in cell:
                    mapping["type"] = index
                elif "正确答案" in cell:
                    mapping["answer"] = index
                elif "解析" in cell:
                    mapping["analysis"] = index
                elif "章节" in cell:
                    mapping["chapter"] = index
                elif re.match(r"^选项\s*[A-H]", cell):
                    mapping[cell.replace(" ", "").replace("\n", "")[-1]] = index
            if "title" in mapping and "answer" in mapping:
                return row_index, mapping
    return 1, {"title": 0, "type": 1, "A": 2, "B": 3, "C": 4, "D": 5,
               "answer": 10, "analysis": 11, "chapter": 12}


def parse_kaoshibao(content: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    sheet = workbook[workbook.sheetnames[0]]
    rows = [tuple(row) for row in sheet.iter_rows(values_only=True)]
    if not rows:
        return [], {"total": 0, "imported": 0, "skipped": 0, "reasons": []}

    header_index, columns = _find_header(rows)
    items: list[dict[str, Any]] = []
    reasons: list[dict[str, Any]] = []

    for row_number, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        if not row:
            continue

        def cell(key: str) -> str:
            index = columns.get(key)
            return _clean(row[index]) if index is not None and index < len(row) else ""

        title = cell("title")
        if not title:
            continue
        qtype = _norm_type(cell("type"))
        raw_answer = cell("answer")

        option_values = [cell(letter) for letter in _LETTERS]
        options = [value for value in option_values if value]
        if qtype == "judge" and len(options) < 2:
            options = ["正确", "错误"]
        if qtype == "completion":
            # 导出脚本把填空答案放在「选项A」列
            if not raw_answer and options:
                raw_answer = options[0]
            options = []
        if qtype in ("single", "multiple"):
            options = parse_options(options[:8])

        if qtype is None:
            reasons.append({"row": row_number, "title": title[:60], "reason": "题型不支持"})
            continue
        if qtype in ("single", "multiple") and len(options) < 2:
            reasons.append({"row": row_number, "title": title[:60], "reason": "选项不足"})
            continue

        if qtype in ("single", "multiple"):
            answer = _letters_from(raw_answer, options)
        elif qtype == "judge":
            answer = _judge_answer(raw_answer, options)
        else:
            answer = raw_answer.strip()
        if not answer:
            reasons.append({"row": row_number, "title": title[:60], "reason": "答案无法解析"})
            continue

        items.append({
            "type": qtype,
            "title": title,
            "options": options,
            "answer": answer,
            "analysis": cell("analysis"),
            "chapter": cell("chapter"),
        })

    workbook.close()
    return items, {
        "total": len(items) + len(reasons),
        "parsed": len(items),
        "skipped": len(reasons),
        "reasons": reasons[:20],
    }

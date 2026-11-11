"""解析 icourse163 官方卷 DTO（getOpenQuizPaperDto），转成题库反馈条目。"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any

from .parsing import normalize_type, parse_answer, parse_options

_ZW = re.compile(r"[\u200B-\u200F\u202A-\u202E\u2060\uFEFF]")
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_TYPE_MAP = {1: "single", 2: "multiple", 3: "completion", 4: "judge"}


def _text(fragment: str) -> str:
    """HTML 转文本，图片保留原 URL（与 OCS 传给题库的题干写法一致）。"""
    raw = re.sub(r'<img[^>]*src="([^"]+)"[^>]*>', r" \1 ", fragment or "", flags=re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html_lib.unescape(raw)
    raw = _ZW.sub("", raw)
    return re.sub(r"\s+", " ", raw).strip()


def parse_paper(paper: dict[str, Any]) -> dict[str, Any]:
    """从官方卷 DTO 抽取可入库的题目（含标准答案），无答案的题跳过。"""
    if not isinstance(paper, dict):
        return {"items": [], "total": 0, "answered": 0, "unanswered": 0}
    questions = paper.get("objectiveQList") or paper.get("questions") or []
    items: list[dict[str, Any]] = []
    unanswered = 0

    for q in questions:
        if not isinstance(q, dict):
            continue
        option_dtos = q.get("optionDtos") or []
        if not option_dtos and isinstance(q.get("options"), list):
            # DWR 回包：options 是 [{content, answer}] 形式的列表
            option_dtos = [o for o in q["options"] if isinstance(o, dict)]
        qtype = _TYPE_MAP.get(q.get("type"))
        if qtype is None and q.get("type") is not None:
            qtype = normalize_type(q.get("type"))
        if qtype is None:
            if q.get("stdAnswer"):
                qtype = "completion"
            else:
                options_text = [_text(o.get("content") or "") for o in option_dtos]
                true_count = sum(1 for o in option_dtos if o.get("answer"))
                if len(options_text) == 2 and all(o in ("正确", "错误", "对", "错") for o in options_text):
                    qtype = "judge"
                elif true_count > 1:
                    qtype = "multiple"
                else:
                    qtype = "single"
        title = _text(q.get("title") or q.get("plainTextTitle") or q.get("ocrTitle") or q.get("content") or "")
        if qtype is None or not title:
            continue
        options = [_text(o.get("content") or o.get("ocrContent") or "") for o in option_dtos]
        correct_letters = [
            _LETTERS[i] for i, o in enumerate(option_dtos) if o.get("answer") and i < len(_LETTERS)
        ]
        correct_text = ""
        if qtype == "judge":
            for o in option_dtos:
                if o.get("answer"):
                    raw = _text(o.get("content") or "")
                    correct_text = parse_answer("judge", raw, options) or ""
                    break
        answer = None
        if qtype == "completion":
            answer = str(q.get("stdAnswer") or "").strip()
        elif qtype == "judge":
            answer = correct_text
        elif qtype == "single" and correct_letters:
            answer = correct_letters[0]
        elif qtype == "multiple" and correct_letters:
            answer = "#".join(sorted(correct_letters))
        if not answer:
            unanswered += 1
            continue
        if qtype in ("single", "multiple") and len(parse_options(options)) < 2:
            unanswered += 1
            continue
        items.append(
            {
                "type": qtype,
                "title": title,
                "options": options,
                "answer": answer,
                "selected_letters": [],
                "correct_letters": correct_letters,
                "correct_text": correct_text,
                "is_correct": True,
                "blank": False,
                "status_text": "",
            }
        )

    return {
        "items": items,
        "total": len(questions),
        "answered": len(items),
        "unanswered": unanswered,
        "show_analysis": bool(paper.get("showAnalysis")),
        "submit_status": paper.get("submitStatus"),
        "tid": paper.get("tid"),
        "aid": paper.get("aid"),
        "name": paper.get("tname") or paper.get("name") or "",
    }

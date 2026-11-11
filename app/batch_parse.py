"""多题批量作答的 JSON 容错解析。

模型被要求返回::

    {"answers": [{"id": "q1", "answer": "A", "confidence": 0.9}]}

实际输出经常带 markdown 围栏、前后废话、尾逗号、中文引号，甚至被截断。
这里做 5 级兜底：直解 → 剥围栏 → 平衡括号切片 → 修复/补括号 → 正则抓取，
返回按 id 对齐的结果；调用方只对 ``missing`` 里的题补问。

本模块只负责「把模型输出变成 {id: {answer, confidence}}」，不做题型校验；
单选的字母规范化仍由 ``app.parsing.parse_answer_detail`` 完成。
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable

_ID_KEYS = ("id", "qid", "question_id", "number", "index", "题号", "题目id")
_ANSWER_KEYS = ("answer", "ans", "value", "choice", "result", "答案")
_CONFIDENCE_KEYS = ("confidence", "conf", "probability", "score", "置信度")
_CONTAINER_KEYS = ("answers", "data", "results", "items")

_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\s*\n?(.*?)```", re.S)

_ID_TEXT_RE = re.compile(
    r'"(?:id|qid|question_id|题号)"\s*:\s*("(?:[^"\\]|\\.)*"|[A-Za-z0-9_\-\u4e00-\u9fff]+)'
)
_ANS_TEXT_RE = re.compile(
    r'"(?:answer|ans|value|choice|result|答案)"\s*:\s*'
    r'("(?:[^"\\]|\\.)*"|\[[^\]]*\]|[^,}\s]+)'
)
_CONF_TEXT_RE = re.compile(
    r'"(?:confidence|conf|probability|置信度)"\s*:\s*([0-9]+(?:\.[0-9]+)?\s*%?)'
)


def _strip_fence(text: str) -> str:
    """剥掉 ```json ... ``` 围栏；没有围栏时去掉首尾反引号。"""
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip().strip("`").strip()


def _balanced_slice(text: str) -> str:
    """取第一个 ``{`` / ``[`` 起始的平衡片段，跳过字符串里的括号。"""
    starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
    if not starts:
        return ""
    start = min(starts)
    stack: list[str] = []
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack:
                continue
            opening = stack.pop()
            if (opening, char) not in (("{", "}"), ("[", "]")):
                return ""
            if not stack:
                return text[start:index + 1]
    return ""


def _close_brackets(text: str) -> str:
    """给被截断的 JSON 补齐引号和括号；已经平衡时原样返回。"""
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]" and stack:
            stack.pop()
    if not stack and not in_string:
        return text
    out = text + ('"' if in_string else "")
    closers = {"{": "}", "[": "]"}
    out += "".join(closers[char] for char in reversed(stack))
    return out


def _salvage_truncated(text: str) -> str:
    """截断输出：退到最后一个完整的 ``}`` 处，剩下的交给补括号。"""
    end = text.rfind("}")
    if end < 0 or end >= len(text) - 1:
        return ""
    return text[:end + 1]


def _repair_once(text: str) -> str:
    """修常见 JSON 错误：中文引号、单引号、尾逗号、裸 key、缺逗号。"""
    fixed = (text.replace("“", '"').replace("”", '"')
             .replace("‘", "'").replace("’", "'"))
    if '"' not in fixed:
        fixed = fixed.replace("'", '"')
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    fixed = re.sub(r"}\s*{", "},{", fixed)
    return _quote_bare_keys(fixed)


def _quote_bare_keys(text: str) -> str:
    """给 ``{answers:`` / ``{id:`` 这类裸 key 补引号。

    逐字符扫描并跟踪字符串状态——不能直接用正则，否则填空题答案里的
    ``{a:b}`` 也会被插引号，把好数据改坏。
    """
    out: list[str] = []
    in_string = False
    escaped = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char not in "{,":
            out.append(char)
            index += 1
            continue
        out.append(char)
        index += 1
        while index < length and text[index].isspace():
            out.append(text[index])
            index += 1
        start = index
        if index < length and (text[index].isalpha() or text[index] == "_"):
            index += 1
            while index < length and (text[index].isalnum() or text[index] in "_-"):
                index += 1
        key = text[start:index]
        probe = index
        while probe < length and text[probe].isspace():
            probe += 1
        if key and probe < length and text[probe] == ":":
            out.append(f'"{key}"')
            continue
        out.append(key)
    return "".join(out)


def _candidates(text: str) -> list[str]:
    """按「最像正确答案」的顺序给出待尝试的文本，去重保序。"""
    out: list[str] = []
    seen: set[str] = set()

    def add(value: str | None) -> None:
        if not value:
            return
        value = value.strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)

    add(text)
    stripped = _strip_fence(text)
    add(stripped)
    sliced = _balanced_slice(text)
    add(sliced)
    for base in (sliced, stripped, text):
        if not base:
            continue
        repaired = _repair_once(base)
        add(repaired)
        add(_close_brackets(repaired))
        salvaged = _salvage_truncated(repaired)
        add(salvaged)
        add(_close_brackets(salvaged) if salvaged else "")
    return out


def _pick(entry: dict, keys: Iterable[str]) -> Any:
    for key in keys:
        if key in entry:
            return entry[key]
    return None


def _map_entries(mapping: dict) -> list[dict]:
    entries: list[dict] = []
    for key, value in mapping.items():
        if isinstance(value, dict):
            item = dict(value)
            item.setdefault("id", key)
            entries.append(item)
        elif isinstance(value, (str, int, float, bool)) or value is None or isinstance(value, list):
            entries.append({"id": key, "answer": value})
    return entries


def _iter_entries(data: Any) -> list[dict]:
    """把各种外层结构统一成 entry 列表。"""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in _CONTAINER_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return _map_entries(value)
    if any(key in data for key in _ID_KEYS) or any(key in data for key in _ANSWER_KEYS):
        return [data]
    return _map_entries(data)


def _entry_id(entry: dict) -> str:
    value = _pick(entry, _ID_KEYS)
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (str, int)):
        return str(value).strip()
    return ""


def _normalize_answer(value: Any) -> str:
    """把模型给的任意标量/数组统一成 OCS 答案字符串。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        parts = [_normalize_answer(item) for item in value]
        return "#".join(part for part in parts if part)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dict):
        return ""
    return str(value).strip()


def _confidence(value: Any) -> float | None:
    """0.9 / 90 / "85%" 统一成 0~1；非法值返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    percent = False
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        percent = text.endswith("%")
        text = text.rstrip("%").strip()
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number):
        return None
    if percent:
        number /= 100.0
    elif 1.0 < number <= 100.0:
        number /= 100.0
    if number < 0.0 or number > 1.0:
        return None
    return round(number, 4)


def _literal(token: str) -> Any:
    token = token.strip()
    if not token:
        return None
    if token[0] in "\"[" and token[-1] in "\"]":
        try:
            return json.loads(token)
        except (json.JSONDecodeError, ValueError):
            return token.strip("\"[] ").strip()
    return token


def _regex_entries(text: str) -> dict[str, dict]:
    """最后一级兜底：按 "id": ... "answer": ... 抓取，不需要整体是合法 JSON。"""
    matches = list(_ID_TEXT_RE.finditer(text))
    out: dict[str, dict] = {}
    for index, match in enumerate(matches):
        qid = str(_literal(match.group(1)) or "").strip()
        if not qid:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():end]
        answer_match = _ANS_TEXT_RE.search(body)
        if not answer_match:
            continue
        conf_match = _CONF_TEXT_RE.search(body)
        out[qid] = {
            "answer": _normalize_answer(_literal(answer_match.group(1))),
            "confidence": _confidence(conf_match.group(1)) if conf_match else None,
        }
    return out


def parse_answer_array(
    raw: str,
    expected: Iterable[str] | None = None,
) -> tuple[dict[str, dict], list[str]]:
    """解析批量作答结果。

    返回 ``({id: {"answer": str, "confidence": float|None}}, missing_ids)``。
    ``answer`` 为空字符串的题也会出现在 results 里，同时计入 ``missing``，
    调用方可以据此只补问缺失的题。
    """
    text = str(raw or "").strip()
    wanted = [str(item) for item in expected] if expected is not None else None
    parsed: dict[str, dict] = {}
    if text:
        for candidate in _candidates(text):
            try:
                data = json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                continue
            entries = _iter_entries(data)
            if not entries:
                continue
            current: dict[str, dict] = {}
            for entry in entries:
                qid = _entry_id(entry)
                if not qid:
                    continue
                current[qid] = {
                    "answer": _normalize_answer(_pick(entry, _ANSWER_KEYS)),
                    "confidence": _confidence(_pick(entry, _CONFIDENCE_KEYS)),
                }
            if not current:
                continue
            if wanted is not None and wanted and not (set(current) & set(wanted)):
                continue
            parsed = current
            break
        else:
            parsed = _regex_entries(text)

    if wanted is not None:
        parsed = {key: value for key, value in parsed.items() if key in set(wanted)}
        missing = [
            key for key in wanted
            if not (parsed.get(key) or {}).get("answer")
        ]
    else:
        missing = [key for key, value in parsed.items() if not value.get("answer")]
    return parsed, missing

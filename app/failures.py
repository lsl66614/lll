"""未答/失败题目记录：非 0 返回的 /query 都留痕，便于排查漏题。

同一道题（题干 + 选项）只保留一行：重复失败只累加 ``count``，刷新错误与
时间，不再让同一个问题占满 200 条上限。题目后来被答对（重试、补录进题库、
缓存/记忆命中）时由 :meth:`FailureLog.resolve` 自动移除该行，形成闭环。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

# 可重试的失败类型：临时故障、答案解析失败、其他未知错误。
_RETRYABLE_KINDS = frozenset({"transient", "unparsed", "other"})


def _norm_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _norm_options(options: Any) -> tuple[str, ...]:
    if isinstance(options, (list, tuple)):
        return tuple(text for text in (_norm_text(item) for item in options) if text)
    if isinstance(options, str):
        text = _norm_text(options)
        return (text,) if text else ()
    return ()


def question_key(entry: dict[str, Any]) -> tuple[Any, ...]:
    """题目身份：题干 + 选项。

    题干为空（OCS 传坏了）时退化为错误码 + 错误信息，避免把所有空题干
    的不同错误混成一条。
    """
    title = _norm_text(entry.get("title"))
    if title:
        return ("question", title, _norm_options(entry.get("options")))
    return ("empty", str(entry.get("code") or ""), _norm_text(entry.get("message")))


def classify_failure(code: Any, message: Any) -> str:
    """给失败记录分一个可操作的类型，供管理台显示与决定是否可重试。"""
    text = str(message or "")
    try:
        value = int(code)
    except (TypeError, ValueError):
        value = 0
    if "选项内容是图片" in text:
        return "image_options"
    if "答案无法解析" in text:
        return "unparsed"
    if value == 50002:
        return "config"
    if 40000 <= value < 50000:
        return "invalid"
    if value >= 50000:
        return "transient"
    return "other"


class FailureLog:
    def __init__(self, path: str, limit: int = 200) -> None:
        self.path = path
        self.limit = max(10, int(limit))
        self._lock = threading.RLock()
        self._items: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            items = data.get("items") if isinstance(data, dict) else data
        except (OSError, json.JSONDecodeError, ValueError):
            return
        if not isinstance(items, list):
            return
        raw = [dict(item) for item in items if isinstance(item, dict)]
        # 从旧到新合并，后到的错误信息覆盖先到的，count 累加。
        merged: dict[tuple[Any, ...], dict[str, Any]] = {}
        for entry in sorted(raw, key=lambda item: float(item.get("at") or 0)):
            item = self._seed(entry)
            key = question_key(item)
            merged[key] = self._merge(merged[key], item) if key in merged else item
        self._items = sorted(
            merged.values(), key=lambda item: float(item.get("at") or 0), reverse=True
        )[: self.limit]
        if len(self._items) != len(raw):
            self._save()

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump({"items": self._items}, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def _seed(self, entry: dict[str, Any]) -> dict[str, Any]:
        item = dict(entry)
        at = float(item.get("at") or 0)
        if not at:
            at = time.time()
        item["at"] = at
        item["first_at"] = float(item.get("first_at") or at)
        item["count"] = max(1, int(item.get("count") or 1))
        item["kind"] = str(item.get("kind") or classify_failure(item.get("code"), item.get("message")))
        item["retryable"] = bool(
            item["kind"] in _RETRYABLE_KINDS
            and _norm_text(item.get("title"))
            and int(item.get("images") or 0) == 0
        )
        return item

    def _merge(self, prev: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
        at = float(entry.get("at") or 0)
        merged = dict(prev)
        for field in ("code", "type", "title", "options", "subject", "images", "message"):
            if field not in entry:
                continue
            value = entry[field]
            # 后一次请求没带题干/选项/题型时，不要把已有信息抹掉。
            if field in ("type", "title", "subject") and not _norm_text(value):
                continue
            if field == "options" and not _norm_options(value):
                continue
            if field == "images":
                # 只要有一次带了显式图片，就不能把它当成可纯文本重试的题。
                merged["images"] = max(int(prev.get("images") or 0), int(value or 0))
                continue
            merged[field] = value
        merged["first_at"] = min(float(prev.get("first_at") or prev.get("at") or at), at)
        merged["at"] = max(float(prev.get("at") or 0), at)
        merged["count"] = int(prev.get("count") or 0) + int(entry.get("count") or 1)
        merged["kind"] = classify_failure(merged.get("code"), merged.get("message"))
        merged["retryable"] = bool(
            merged["kind"] in _RETRYABLE_KINDS
            and _norm_text(merged.get("title"))
            and int(merged.get("images") or 0) == 0
        )
        return merged

    def add(self, entry: dict[str, Any]) -> None:
        with self._lock:
            item = self._seed(entry)
            key = question_key(item)
            prev = None
            for index, old in enumerate(self._items):
                if question_key(old) == key:
                    prev = self._items.pop(index)
                    break
            merged = self._merge(prev, item) if prev else item
            self._items.insert(0, merged)
            self._items.sort(key=lambda row: float(row.get("at") or 0), reverse=True)
            self._items = self._items[: self.limit]
            self._save()

    def resolve(self, title: Any, options: Any) -> int:
        """题目已经答出来了：移除对应的失败记录，返回删除条数。"""
        if not _norm_text(title):
            return 0
        key = question_key({"title": title, "options": options})
        with self._lock:
            before = len(self._items)
            kept = [item for item in self._items if question_key(item) != key]
            removed = before - len(kept)
            if removed:
                self._items = kept
                self._save()
            return removed

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._items]

    def remove(self, at: float) -> int:
        """删掉指定时间戳的记录（管理台用来清掉单条无用的记录）。"""
        with self._lock:
            before = len(self._items)
            self._items = [i for i in self._items if abs(float(i.get("at") or 0) - at) > 0.001]
            removed = before - len(self._items)
            if removed:
                self._save()
            return removed

    def clear(self) -> int:
        with self._lock:
            total = len(self._items)
            self._items = []
            self._save()
            return total

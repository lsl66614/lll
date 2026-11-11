"""公式图片文字索引：把题库里的图片 URL 逐张 OCR 成文字，供模糊检索使用。"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from typing import Any
from urllib.parse import urlparse

URL_RE = re.compile(r"https?://[^\s，。；）)\"']+")

# 可重试失败的退避表（秒）：1m、5m、30m、2h、12h、24h，之后固定 24h。
RETRY_SCHEDULE = (60.0, 300.0, 1800.0, 7200.0, 43200.0, 86400.0)
MAX_ATTEMPTS = 8


def retry_delay(attempts: int) -> float:
    """第 N 次失败后等多久再试；N 从 1 开始。"""
    if attempts <= 0:
        return RETRY_SCHEDULE[0]
    return RETRY_SCHEDULE[min(attempts - 1, len(RETRY_SCHEDULE) - 1)]


def is_actionable_url(url: str) -> bool:
    """URL 是否值得发起 OCR：必须是 http(s)、有像域名的 host、有路径或查询串。

    题库里有不少被截断的碎片（`http://moo`、`http://edu-image.no`、
    `http://mooc-image.`），它们不是有效图片，直接标永久失败，避免反复重试。
    """
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip(".")
    if "." not in host:
        return False
    return bool(parsed.path or parsed.query)


def extract_urls(text: str) -> list[str]:
    return URL_RE.findall(str(text or ""))


def latex_plain(text: str) -> str:
    """把 LaTeX 片段降成可比较的纯文本：去掉命令反斜杠、$、{}、&、换行等。"""
    value = str(text or "")
    value = re.sub(r"\\begin\{[^}]+\}|\\end\{[^}]+\}", " ", value)
    value = re.sub(r"\\[a-zA-Z]+", lambda m: " " + m.group(0)[1:] + " ", value)
    value = value.replace("\\", " ")
    value = re.sub(r"[\$\{\}&_^#~]", " ", value)
    return value


class ImageTextStore:
    """图片 URL → OCR 文本的持久缓存。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._items: dict[str, dict[str, Any]] = {}
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if isinstance(data, dict):
                self._items = {}
                for key, value in data.items():
                    if not isinstance(value, dict):
                        continue
                    item = dict(value)
                    if str(item.get("text") or "").strip():
                        item["status"] = "ok"
                        self._items[str(key)] = item
                        continue
                    # 旧格式失败条目只有 failed/at：迁移成「立即允许重试一次」，
                    # 让新一轮 OCR 把它们分成 transient / permanent。
                    if item.get("status") not in ("retryable", "permanent"):
                        item["status"] = "retryable"
                        item["next_retry_at"] = 0.0
                        item.setdefault("last_error", "legacy failure")
                    try:
                        item["attempts"] = max(1, int(item.get("attempts") or 1))
                    except (TypeError, ValueError):
                        item["attempts"] = 1
                    self._items[str(key)] = item
        except (OSError, json.JSONDecodeError, ValueError):
            self._items = {}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(self._items, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def get(self, url: str) -> str:
        with self._lock:
            item = self._items.get(url) or {}
            return str(item.get("text") or "")

    def known(self, url: str, retry_days: float = 7) -> bool:
        """是否已有结论、且现在不该再试。

        成功和永久失败返回 True；可重试失败只在退避时间未到时返回 True，
        到点后返回 False，让队列/清扫器重新尝试。``retry_days`` 只用于
        兼容没有 status 的旧条目。
        """
        with self._lock:
            item = self._items.get(url)
            if not item:
                return False
            if str(item.get("text") or "").strip():
                return True
            status = item.get("status")
            if status == "permanent":
                return True
            if status == "retryable":
                return time.time() < float(item.get("next_retry_at") or 0)
            failed_at = float(item.get("at") or 0)
            return (time.time() - failed_at) < retry_days * 86400

    def put_failed(
        self,
        url: str,
        *,
        retryable: bool = True,
        error: str = "",
        retry_after: float | None = None,
    ) -> None:
        """记录一次失败；已有成功文本时不覆盖，避免一次坏重试抹掉好结果。"""
        if not url:
            return
        now = time.time()
        with self._lock:
            old = self._items.get(url) or {}
            if str(old.get("text") or "").strip():
                return
            try:
                attempts = int(old.get("attempts") or 0) + 1
            except (TypeError, ValueError):
                attempts = 1
            if retryable and attempts >= MAX_ATTEMPTS:
                retryable = False
                error = f"{error}；超过最大重试次数 {MAX_ATTEMPTS}".strip("；")
            delay = retry_delay(attempts) if retry_after is None else max(0.0, float(retry_after))
            self._items[url] = {
                "text": "",
                "failed": True,
                "status": "retryable" if retryable else "permanent",
                "attempts": attempts,
                "last_error": str(error or "")[:300],
                "at": now,
                "next_retry_at": now + delay if retryable else 0.0,
            }
            self._save()

    def put(self, url: str, text: str, provider: str = "") -> None:
        text = str(text or "").strip()
        if not url or not text:
            return
        with self._lock:
            old = self._items.get(url) or {}
            try:
                attempts = int(old.get("attempts") or 0) + 1
            except (TypeError, ValueError):
                attempts = 1
            self._items[url] = {
                "text": text,
                "provider": provider,
                "at": time.time(),
                "status": "ok",
                "attempts": attempts,
                "last_error": "",
                "next_retry_at": 0.0,
            }
            self._save()

    def retryable_urls(self, now: float | None = None) -> list[str]:
        """所有到点该重试的 URL（含不在题库里的查询图）。"""
        moment = time.time() if now is None else now
        with self._lock:
            return [
                url for url, item in self._items.items()
                if not str(item.get("text") or "").strip()
                and item.get("status") == "retryable"
                and float(item.get("next_retry_at") or 0) <= moment
            ]

    def stats(self) -> dict[str, int]:
        with self._lock:
            counts = {"total": len(self._items), "ok": 0, "retryable": 0, "permanent": 0, "other": 0}
            for item in self._items.values():
                if str(item.get("text") or "").strip():
                    counts["ok"] += 1
                elif item.get("status") == "permanent":
                    counts["permanent"] += 1
                elif item.get("status") == "retryable":
                    counts["retryable"] += 1
                else:
                    counts["other"] += 1
            return counts

    def ok_count(self) -> int:
        return self.stats()["ok"]

    def mapping(self) -> dict[str, str]:
        with self._lock:
            return {url: str(item.get("text") or "") for url, item in self._items.items() if item.get("text")}

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def clear(self) -> int:
        with self._lock:
            count = len(self._items)
            self._items = {}
            self._save()
            return count


def collect_urls(items: list[dict[str, Any]]) -> list[str]:
    """从题库条目（题干+选项）里收集去重后的图片 URL。"""
    seen: dict[str, None] = {}
    for item in items:
        for text in [item.get("title") or ""] + list(item.get("options") or []):
            for url in extract_urls(text):
                seen.setdefault(url, None)
    return list(seen)


def make_data_url(raw: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(raw).decode()

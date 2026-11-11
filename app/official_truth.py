"""结果页快照的「页面真值」索引 —— 自动裁决的官方答案来源之一。

只收录**答案块可见**的结果页（答题页 examMode 的隐藏占位值不算）。按
「题型 + 文本指纹 + 选项指纹」分组，只有组内答案唯一时才收录；同一图片
指纹下出现多个答案的条目直接丢弃，避免把错位/歧义值当真值。

索引本身是快照目录的派生缓存：文件名/大小/mtime 指纹不变就直接读
``data/official_truth.json``，变了才重建。重建在后台线程做，查询只读内存。
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import threading
import time
from typing import Any

from .answer_memory import _img_compatible, question_fingerprint
from .bank import translate_answer
from .mooc_result import _answer_text, parse_result_html
from .parsing import parse_options


class OfficialTruth:
    """快照真值索引：``lookup`` 返回与题目身份匹配且唯一的一条页面答案。"""

    def __init__(self, snapshot_dir: str, cache_path: str) -> None:
        self.snapshot_dir = snapshot_dir
        self.cache_path = cache_path
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str, str], list[dict[str, Any]]] | None = None
        self._fingerprint = ""
        self._built_at = 0.0
        self._building = False

    # ------------------------------------------------------------------ 指纹
    def source_fingerprint(self) -> str:
        """快照文件集合的指纹（名字 + 大小 + mtime），用于判断缓存是否过期。"""
        digest = hashlib.sha1()
        count = 0
        for path in sorted(glob.glob(os.path.join(self.snapshot_dir, "*.html"))):
            try:
                stat = os.stat(path)
            except OSError:
                continue
            count += 1
            digest.update(os.path.basename(path).encode("utf-8", "replace"))
            digest.update(b"|%d|%d\n" % (int(stat.st_mtime), stat.st_size))
        return "%d:%s" % (count, digest.hexdigest())

    # ------------------------------------------------------------------ 构建
    def _build(self) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
        grouped: dict[tuple[str, str, str], dict[str, dict[str, dict]]] = {}
        for path in sorted(glob.glob(os.path.join(self.snapshot_dir, "*.html"))):
            filename = os.path.basename(path)
            try:
                with open(path, encoding="utf-8", errors="replace") as fp:
                    html = fp.read()
            except OSError:
                continue
            if "u-questionItem" not in html:
                continue
            for item in parse_result_html(html):
                if not item.get("analysis_visible"):
                    continue
                answer = _answer_text(item)
                if not answer:
                    continue
                fp = question_fingerprint(
                    item.get("type"), item.get("title") or "", item.get("options") or [], "")
                if not fp["qtype"]:
                    continue
                key = (fp["qtype"], fp["text_key"], fp["options_key"])
                bucket = grouped.setdefault(key, {}).setdefault(fp["img_key"], {})
                entry = bucket.setdefault(answer, {"options": set(), "files": set()})
                entry["options"].add(tuple(item.get("options") or []))
                entry["files"].add(filename)
        entries: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for key, by_image in grouped.items():
            rows: list[dict[str, Any]] = []
            for img_key, by_answer in by_image.items():
                if len(by_answer) != 1:
                    # 同一图片指纹下答案不唯一，这条证据不可用。
                    continue
                answer, payload = next(iter(by_answer.items()))
                # 同一答案可能来自多份快照，选项原文略有差异（如带 ``A.`` 前缀）。
                # 取字典序最小的那份，保证索引内容稳定可复现。
                options = min(payload["options"]) if payload["options"] else ()
                rows.append({"img_key": img_key, "answer": answer,
                             "options": list(options),
                             "files": len(payload["files"])})
            if rows:
                entries[key] = rows
        return entries

    # ------------------------------------------------------------------ 缓存
    def _load_cache(self) -> tuple[str, dict] | None:
        try:
            with open(self.cache_path, encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        entries = {}
        for row in data.get("entries") or []:
            try:
                key = (str(row["qtype"]), str(row["text_key"]), str(row["options_key"]))
            except (KeyError, TypeError):
                return None
            rows = row.get("rows")
            if isinstance(rows, list):
                entries[key] = [r for r in rows if isinstance(r, dict)]
        return str(data.get("fingerprint") or ""), entries

    def _save_cache(self, fingerprint: str, entries: dict) -> None:
        payload = {
            "fingerprint": fingerprint,
            "built_at": time.time(),
            "entries": [
                {"qtype": key[0], "text_key": key[1], "options_key": key[2], "rows": rows}
                for key, rows in entries.items()
            ],
        }
        tmp = self.cache_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False)
            os.replace(tmp, self.cache_path)
        except OSError:
            pass

    # ------------------------------------------------------------------ 刷新
    def ensure(self, force: bool = False) -> bool:
        """确保索引可用；返回本次是否真的重建过。"""
        with self._lock:
            fingerprint = self.source_fingerprint()
            if not force and self._entries is not None and fingerprint == self._fingerprint:
                return False
            if not force and self._entries is None and not self._building:
                cached = self._load_cache()
                if cached is not None and cached[0] == fingerprint:
                    self._entries = cached[1]
                    self._fingerprint = fingerprint
                    return False
            entries = self._build()
            self._entries = entries
            self._fingerprint = fingerprint
            self._built_at = time.time()
            self._save_cache(fingerprint, entries)
            return True

    def ensure_async(self, force: bool = False) -> None:
        """快照变了就在后台重建；查询继续用上一版索引，不阻塞调用方。"""
        with self._lock:
            if self._building:
                return
            fingerprint = self.source_fingerprint()
            if not force and self._entries is not None and fingerprint == self._fingerprint:
                return
            self._building = True

        def run() -> None:
            try:
                self.ensure(force=True)
            finally:
                with self._lock:
                    self._building = False

        threading.Thread(target=run, daemon=True, name="official-truth").start()

    # ------------------------------------------------------------------ 查询
    def lookup(
        self,
        qtype: Any,
        title: str,
        options: Any,
        image_hash: str = "",
    ) -> dict[str, Any] | None:
        """返回与题目身份匹配且答案唯一的页面真值；有歧义或没证据时返回 None。"""
        if self._entries is None:
            self.ensure()
        fingerprint = question_fingerprint(qtype, title, options or [], image_hash)
        if not fingerprint["qtype"]:
            return None
        key = (fingerprint["qtype"], fingerprint["text_key"], fingerprint["options_key"])
        rows = (self._entries or {}).get(key) or []
        if not rows:
            return None
        target_options = parse_options(options)
        found: dict[str, dict[str, Any]] = {}
        evidence = 0
        files = 0
        for row in rows:
            if not _img_compatible(str(row.get("img_key") or ""),
                                   fingerprint["img_key"], strict=True):
                continue
            evidence += 1
            files = max(files, int(row.get("files") or 1))
            translated = translate_answer(
                str(row.get("answer") or ""), list(row.get("options") or []),
                target_options, fingerprint["qtype"])
            if translated:
                found.setdefault(translated, row)
        if len(found) != 1:
            return None
        answer, row = next(iter(found.items()))
        return {
            "answer": answer,
            "title": str(row.get("title") or ""),
            "options": list(row.get("options") or []),
            "evidence": evidence,
            "files": files,
        }

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entries": len(self._entries or {}),
                "built_at": self._built_at,
                "fingerprint": self._fingerprint,
                "ready": self._entries is not None,
                "building": self._building,
            }

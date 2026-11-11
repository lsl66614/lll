"""最近做错的题：从结果页快照里抽「用户选了什么 / 官方正确答案是什么」。

管理台「冲突裁决」板块要实时显示刚做过的错题，并告诉用户官方答案有没有
被题库和答题记忆吸收。数据来源就是 ``data/snapshots/*.html`` —— 每份结果页
回传时都会落盘，里面同时带着「你错选为 X」与「正确答案 Y」，不需要另建一套
上报协议。

索引是快照目录的派生缓存：按「文件名 + mtime + size」判断某份快照要不要重解析，
解析结果写进 ``data/feedback_feed.json``。新快照在 ``/api/snapshot`` 落盘时
直接 ``ingest``（页面已经解析过，零额外成本），所以管理台下一次刷新就能看到。

索引收录答案块可见的**全部**题目（对错都收），查询时按题目去重、只看每题
最近一次：最近一次仍错/未作答才展示，后来做对的题自动退出错题列表。
答题页 examMode 的隐藏答案块不收（那是占位值，不是平台真值）。
"""

from __future__ import annotations

import glob
import json
import os
import threading
import time
from typing import Any

from .answer_memory import question_fingerprint
from .bank import _loose_option_key
from .mooc_result import _answer_text, parse_result_html
from .parsing import normalize_type, parse_options
from .shape_key import image_key


class FeedbackFeed:
    """结果页错题索引（带磁盘缓存与追加写入）。"""

    def __init__(self, snapshot_dir: str, cache_path: str) -> None:
        self.snapshot_dir = snapshot_dir
        self.cache_path = cache_path
        self._lock = threading.RLock()
        self._files: dict[str, dict[str, Any]] | None = None
        self._built_at = 0.0

    # ------------------------------------------------------------------ 缓存
    @staticmethod
    def _sig(path: str) -> str:
        try:
            stat = os.stat(path)
        except OSError:
            return ""
        return "%d:%d" % (int(stat.st_mtime_ns), int(stat.st_size))

    @staticmethod
    def _file_at(path: str) -> float:
        """优先用快照旁车 meta 里的回传时间；没有就退回文件 mtime。"""
        meta_path = path + ".json"
        try:
            with open(meta_path, encoding="utf-8") as fp:
                meta = json.load(fp)
            value = float(meta.get("at") or 0)
            if value > 0:
                return value
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        try:
            return float(os.stat(path).st_mtime)
        except OSError:
            return 0.0

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        try:
            with open(self.cache_path, encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, json.JSONDecodeError, ValueError):
            return {}
        # 版本升级：v1 只存错题，v2 存全部可见题（查询时按「最近一次」判断）。
        if not isinstance(data, dict) or str(data.get("version") or "") != "2":
            return {}
        files = data.get("files")
        clean: dict[str, dict[str, Any]] = {}
        for name, row in files.items():
            if not isinstance(row, dict) or not isinstance(row.get("entries"), list):
                continue
            clean[str(name)] = row
        return clean

    def _save(self) -> None:
        payload = {
            "version": 2,
            "built_at": self._built_at or time.time(),
            "files": self._files or {},
        }
        tmp = self.cache_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False)
            os.replace(tmp, self.cache_path)
        except OSError:
            # 缓存写失败不影响本次查询：内存里的索引仍然可用。
            pass

    # ------------------------------------------------------------------ 抽取
    @staticmethod
    def _entry(item: dict[str, Any], at: float, filename: str) -> dict[str, Any] | None:
        qtype = normalize_type(item.get("type"))
        if qtype is None:
            return None
        title = str(item.get("title") or "")
        if not title:
            return None
        options = parse_options(item.get("options"))
        selected = [str(x).upper() for x in (item.get("selected_letters") or [])
                    if str(x).strip()]
        return {
            "at": float(at or 0),
            "file": filename,
            "qtype": qtype,
            "title": title[:2000],
            "options": options,
            "selected": selected,
            "answer": str(_answer_text(item) or ""),
            "is_correct": bool(item.get("is_correct")),
            "blank": bool(item.get("blank")),
            "status_text": str(item.get("status_text") or "")[:80],
        }

    @classmethod
    def extract(cls, items: list[dict[str, Any]], at: float,
                filename: str = "") -> list[dict[str, Any]]:
        """收录答案块可见的题目（对错都收，查询时只看每题最近一次）。"""
        entries: list[dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if not item.get("analysis_visible"):
                continue
            entry = cls._entry(item, at, filename)
            if entry is not None:
                entries.append(entry)
        return entries

    def _parse_file(self, path: str) -> list[dict[str, Any]]:
        try:
            with open(path, encoding="utf-8", errors="replace") as fp:
                html = fp.read()
        except OSError:
            return []
        if "u-questionItem" not in html:
            return []
        return self.extract(parse_result_html(html), self._file_at(path),
                            os.path.basename(path))

    # ------------------------------------------------------------------ 刷新
    def ensure(self, force: bool = False) -> bool:
        """确保索引与快照目录一致；返回是否重建/更新过。"""
        with self._lock:
            if self._files is None:
                self._files = {} if force else self._load_cache()
            changed = False
            existing: set[str] = set()
            for path in sorted(glob.glob(os.path.join(self.snapshot_dir, "*.html"))):
                name = os.path.basename(path)
                existing.add(name)
                sig = self._sig(path)
                row = self._files.get(name)
                if not force and row and str(row.get("sig") or "") == sig:
                    continue
                self._files[name] = {
                    "sig": sig,
                    "at": self._file_at(path),
                    "entries": self._parse_file(path),
                }
                changed = True
            for name in list(self._files):
                if name not in existing:
                    del self._files[name]
                    changed = True
            if changed:
                self._built_at = time.time()
                self._save()
            return changed

    def ingest(self, filename: str, at: float,
               items: list[dict[str, Any]]) -> None:
        """新快照落盘后直接入索引，不必等下一次全目录扫描。"""
        entries = self.extract(items, at, filename)
        with self._lock:
            if self._files is None:
                self._files = self._load_cache()
            path = os.path.join(self.snapshot_dir, filename)
            self._files[filename] = {
                "sig": self._sig(path),
                "at": float(at or time.time()),
                "entries": entries,
            }
            self._built_at = time.time()
            self._save()

    # ------------------------------------------------------------------ 查询
    @staticmethod
    def _dedupe_key(entry: dict[str, Any]) -> tuple:
        """同一道题只看最近一次错答，避免同一个测验重做几遍刷屏。

        只用**题干**的文字骨架 + 题干里的图片指纹：选项常被平台重新渲染，
        URL 的 host/查询串一变（甚至选项图重新上传），整题指纹就会变，
        同一道题会被当成新题反复显示。题干里的公式图指纹则稳定得多。

        选项内容单独按「排序后的宽松指纹」入键：换序不影响合并，但两道
        题干相同、选项不同的题不会被误并成一条（短题干「设」尤其明显）。
        """
        fp = question_fingerprint(entry.get("qtype"), entry.get("title") or "",
                                  entry.get("options") or [], "")
        title_image = image_key(entry.get("title") or "")
        options_key = tuple(sorted(
            _loose_option_key(option, fp["qtype"])
            for option in (entry.get("options") or [])
        ))
        if fp["text_key"] or title_image:
            return ("t", fp["qtype"], fp["text_key"], title_image, options_key)
        return ("r", fp["qtype"], str(entry.get("title") or ""), options_key)

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        if self._files is None:
            self.ensure()
        limit = max(1, min(int(limit or 30), 100))
        with self._lock:
            entries = [entry for row in (self._files or {}).values()
                       for entry in (row.get("entries") or [])
                       if isinstance(entry, dict)]
        entries.sort(key=lambda e: float(e.get("at") or 0), reverse=True)
        seen: set[tuple] = set()
        picked: list[dict[str, Any]] = []
        for entry in entries:
            key = self._dedupe_key(entry)
            if key in seen:
                continue
            seen.add(key)
            # 每题只看**最近一次**：后来做对了就说明题目已解决，不再占着「错题」
            # 版面；只有最近一次仍然错/未作答才展示，避免翻出早已改对的旧记录。
            if entry.get("is_correct"):
                continue
            picked.append(entry)
            if len(picked) >= limit:
                break
        return picked

    def stats(self) -> dict[str, Any]:
        if self._files is None:
            self.ensure()
        with self._lock:
            total = sum(len(row.get("entries") or [])
                        for row in (self._files or {}).values())
            return {
                "files": len(self._files or {}),
                "entries": total,
                "built_at": self._built_at,
                "ready": self._files is not None,
            }

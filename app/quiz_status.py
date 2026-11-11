"""记录每个测验的官方答案发布状态（来自浏览器侧回捞结果）。

一条测验只保留一行：``tid`` 是测验 id，``aid`` 是每次打开试卷的作答/试卷实例 id，
同一测验重做或重复回捞会产生多个 ``aid``。旧实现按 ``tid:aid`` 存储，管理台会出现
大量重复行；现在按 ``tid`` 合并，并把每次上报的官方答案身份做并集
（``answer_keys``），所以「官方答案」覆盖数只增不减。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

# 后一次上报只在字段出现时覆盖前值；空值不会把已有信息抹掉。
_MERGE_FIELDS = (
    "name",
    "url",
    "show_analysis",
    "submit_status",
    "rpc_code",
    "rpc_message",
    "ver",
    # 官方答案还没放出来时为 True，拿到答案后翻回 False。
    # 「已发布」是一次性事实（published 只置位不清零），但界面上要能看出
    # 「这个测验还在等官方更新」，所以单独记一个最新的 pending 状态。
    "pending",
)
# 这些字段的空字符串不覆盖旧值（后一次上报没带名字，不应把已有标题抹掉）。
_EMPTY_KEEPS = frozenset({"name", "url", "ver"})


class QuizStatus:
    def __init__(self, path: str, limit: int = 500) -> None:
        self.path = path
        self.limit = limit
        self._lock = threading.RLock()
        self._items: dict[str, dict[str, Any]] = {}
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._load()
        self._compact()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if isinstance(data, dict):
                self._items = {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, json.JSONDecodeError, ValueError):
            self._items = {}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(self._items, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def _compact(self) -> None:
        """把旧的 ``tid:aid`` 记录合并成一条 tid（按上报时间从旧到新）。"""
        raw = self._items
        if not raw:
            return
        merged: dict[str, dict[str, Any]] = {}
        changed = False
        for key in sorted(raw, key=lambda k: float((raw[k] or {}).get("at") or 0)):
            record = raw[key]
            tid = str(record.get("tid") or "").strip()
            aid = str(record.get("aid") or "").strip()
            mkey = tid or aid or str(key)
            at = float(record.get("at") or time.time())
            merged[mkey] = self._merge(merged.get(mkey) or {}, record, at)
            if mkey != key:
                changed = True
        if changed or len(merged) != len(raw):
            self._items = merged
            self._save()
        else:
            self._items = merged

    def _merge(self, prev: dict[str, Any], record: dict[str, Any], at: float) -> dict[str, Any]:
        merged = dict(prev)
        for field in _MERGE_FIELDS:
            if field not in record:
                continue
            value = record[field]
            if field in _EMPTY_KEEPS and not value:
                continue
            merged[field] = value

        tid = str(record.get("tid") or merged.get("tid") or "").strip()
        aid = str(record.get("aid") or merged.get("aid") or "").strip()
        merged["tid"] = tid
        merged["aid"] = aid
        merged["key"] = tid or aid

        merged["total"] = max(int(prev.get("total") or 0), int(record.get("total") or 0))
        keys = {str(k) for k in (prev.get("answer_keys") or []) if k}
        incoming = record.get("answer_keys")
        if isinstance(incoming, (list, tuple, set)):
            keys.update(str(k) for k in incoming if k)
        if keys:
            merged["answer_keys"] = sorted(keys)
        merged["answered"] = max(
            int(prev.get("answered") or 0),
            int(record.get("answered") or 0),
            len(keys),
        )
        # 已发布是「曾经拿到过官方答案」的事实，后一次 0 条不应把它抹掉。
        merged["published"] = bool(prev.get("published")) or bool(record.get("published"))
        merged["reports"] = int(prev.get("reports") or 0) + int(record.get("reports") or 1)
        merged["first_at"] = float(prev.get("first_at") or prev.get("at") or at)
        merged["at"] = at
        return merged

    def upsert(self, record: dict[str, Any]) -> dict[str, Any]:
        tid = str(record.get("tid") or "").strip()
        aid = str(record.get("aid") or "").strip()
        if not tid and not aid:
            raise ValueError("tid/aid 至少填一个")
        key = tid or aid
        now = time.time()
        with self._lock:
            prev = self._items.get(key)
            if prev is None and tid:
                # 兼容旧记录：键是 ``tid:aid``，但 tid 相同就是同一条测验。
                for old_key, old in list(self._items.items()):
                    if str((old or {}).get("tid") or "").strip() == tid:
                        prev = old
                        self._items.pop(old_key, None)
                        break
            item = self._merge(prev or {}, record, now)
            item["tid"] = item.get("tid") or tid
            item["aid"] = item.get("aid") or aid
            item["key"] = key
            self._items[key] = item
            if len(self._items) > self.limit:
                ordered = sorted(self._items.items(), key=lambda kv: kv[1].get("at") or 0)
                for old_key, _ in ordered[: len(self._items) - self.limit]:
                    self._items.pop(old_key, None)
            self._save()
            return dict(item)

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted((dict(v) for v in self._items.values()), key=lambda v: v.get("at") or 0, reverse=True)

    def clear(self) -> int:
        with self._lock:
            count = len(self._items)
            self._items = {}
            self._save()
            return count

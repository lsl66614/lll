"""学习进度快照：按「账号 + 课程」保存最新进度，供管理台监控。"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

MAX_ITEMS = 500
# 幂等去重：记住最近这些 (client_id, seq)，重复上报直接忽略。
SEEN_LIMIT = 2000


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


class ProgressStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._items: list[dict[str, Any]] = []
        self._seen: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            items = data.get("items") if isinstance(data, dict) else data
            if isinstance(items, list):
                self._items = [i for i in items if isinstance(i, dict)][:MAX_ITEMS]
        except (OSError, json.JSONDecodeError, ValueError):
            self._items = []

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump({"items": self._items}, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def upsert(
        self,
        account: str,
        course: str,
        done: Any,
        total: Any,
        url: str = "",
        extra: Any = None,
        percent: Any = None,
        key: Any = None,
        chapter: Any = "",
        section: Any = "",
        live: Any = None,
        position_only: Any = False,
        deadline: Any = "",
    ) -> dict[str, Any]:
        account = str(account or "").strip()[:60] or "未命名账号"
        course = str(course or "").strip()[:120] or "未命名课程"
        key = str(key or course).strip()[:200] or course
        chapter = str(chapter or "").strip()[:120]
        section = str(section or "").strip()[:120]
        deadline = str(deadline or "").strip()[:40]
        position_only = bool(position_only)
        done = _int(done)
        total = _int(total)
        if total and done > total:
            done = total
        pct = None if percent in (None, "") else min(100, _int(percent))
        now = time.time()
        with self._lock:
            if key == course:
                # 学习页没有 total，无法用「课程名|总小节」匹配时，落到该账号同名课程行
                same = [i for i in self._items if i.get("account") == account and i.get("course") == course]
                keyed = [i for i in same if (i.get("key") or course) != course]
                if len(keyed) == 1:
                    key = keyed[0]["key"]
                elif len(keyed) > 1:
                    key = max(keyed, key=lambda i: i.get("updated") or 0)["key"]
            if key != course:
                kept = [
                    i
                    for i in self._items
                    if not (
                        i.get("account") == account
                        and i.get("course") == course
                        and (i.get("key") or course) == course
                    )
                ]
                if len(kept) != len(self._items):
                    self._items = kept
            for item in self._items:
                if item.get("account") == account and (item.get("key") or item.get("course")) == key:
                    patch = {
                        "course": course,
                        "key": key,
                        "chapter": chapter or item.get("chapter", ""),
                        "section": section or item.get("section", ""),
                        "deadline": deadline or item.get("deadline", ""),
                        "live": item.get("live", False) if live is None else bool(live),
                        "url": str(url or "")[:500] or item.get("url", ""),
                        "updated": now,
                    }
                    if isinstance(extra, dict):
                        merged = dict(item.get("extra") or {})
                        merged.update(extra)
                        stats = extra.get("video_stats")
                        if isinstance(stats, dict) and stats.get("minutes") is not None:
                            history = list(merged.get("video_history") or [])
                            minutes = _int(stats.get("minutes"))
                            unchanged = history and history[-1].get("minutes") == minutes
                            if not unchanged:
                                history.append({"at": round(now, 3), "minutes": minutes,
                                                "count": _int(stats.get("count"))})
                                merged["video_history"] = history[-120:]
                        patch["extra"] = merged
                    else:
                        patch["extra"] = item.get("extra") or {}
                    if not position_only:
                        patch.update({"done": done, "total": total, "percent": pct})
                        patch["history"] = self._append_point(
                            list(item.get("history") or []), done, total, pct, now
                        )
                    item.update(patch)
                    self._save()
                    return dict(item)
            extra_value = extra if isinstance(extra, dict) else {}
            stats = extra_value.get("video_stats") if isinstance(extra_value, dict) else None
            if isinstance(stats, dict) and stats.get("minutes") is not None:
                extra_value = dict(extra_value)
                extra_value["video_history"] = [{"at": round(now, 3), "minutes": _int(stats.get("minutes")),
                                                 "count": _int(stats.get("count"))}]
            item = {
                "account": account,
                "course": course,
                "key": key,
                "done": done,
                "total": total,
                "percent": pct,
                "chapter": chapter,
                "section": section,
                "deadline": deadline,
                "live": bool(live),
                "url": str(url or "")[:500],
                "extra": extra_value,
                "history": [] if position_only else self._append_point([], done, total, pct, now),
                "created": now,
                "updated": now,
            }
            self._items.append(item)
            self._items = self._items[-MAX_ITEMS:]
            self._save()
            return dict(item)

    def set_extra(self, account: str, key: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            for item in self._items:
                if item.get("account") == account and (item.get("key") or item.get("course")) == key:
                    merged = dict(item.get("extra") or {})
                    merged.update(patch or {})
                    item["extra"] = merged
                    item["updated"] = time.time()
                    self._save()
                    return dict(item)
        return None

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = []
            for item in self._items:
                row = dict(item)
                row["today_delta"] = self._today_delta(item.get("history") or [])
                rows.append(row)
            return sorted(rows, key=lambda i: i.get("updated") or 0, reverse=True)

    # ---- 幂等：同一 client_id + seq 只处理一次 -------------------------------

    def _seen_key(self, client_id: Any, seq: Any) -> str:
        cid = str(client_id or "").strip()[:64]
        if not cid:
            return ""
        try:
            number = int(seq)
        except (TypeError, ValueError):
            return ""
        if number <= 0:
            return ""
        return f"{cid}:{number}"

    def is_duplicate(self, client_id: Any, seq: Any) -> bool:
        """这条上报是否已经处理过（网络重试/多 frame 重复上报）。"""
        key = self._seen_key(client_id, seq)
        if not key:
            return False
        with self._lock:
            return key in self._seen

    def mark_seen(self, client_id: Any, seq: Any) -> None:
        key = self._seen_key(client_id, seq)
        if not key:
            return
        now = time.time()
        with self._lock:
            self._seen[key] = now
            if len(self._seen) > SEEN_LIMIT:
                cutoff = now - 3600
                self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
                if len(self._seen) > SEEN_LIMIT:
                    # 仍超限时按时间丢掉最老的一半
                    ordered = sorted(self._seen.items(), key=lambda kv: kv[1])
                    self._seen = dict(ordered[len(ordered) // 2:])

    @staticmethod
    def _append_point(history: list[dict[str, Any]], done: int, total: int, pct, now: float) -> list[dict[str, Any]]:
        if history:
            last = history[-1]
            unchanged = (last.get("done") == done and last.get("total") == total
                         and last.get("percent") == pct)
            if unchanged and now - float(last.get("at") or 0) < 3600:
                return history
        return (history + [{"at": round(now, 3), "done": done, "total": total, "percent": pct}])[-120:]

    @staticmethod
    def _today_delta(history: list[dict[str, Any]]) -> int:
        """当天第一次记录到现在的完成增量（进度回退时按 0 算）。"""
        if not history:
            return 0
        midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        today = [p for p in history if float(p.get("at") or 0) >= midnight]
        if len(today) < 2:
            return 0
        delta = int(today[-1].get("done") or 0) - int(today[0].get("done") or 0)
        return max(0, delta)

    def clear(self) -> int:
        with self._lock:
            total = len(self._items)
            self._items = []
            self._save()
            return total

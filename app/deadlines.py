"""测验/作业/考试 DDL 汇总：按账号+课程上报，长期积累，过期归档。

旧实现每次上报都整组替换 ``items``，一次只读到部分条目（``partial=True``）
就会把之前积累的 DDL 全部抹掉；过期的 DDL 也会永远留在看板上。现在：

- 上报按稳定身份（tid/aid，缺失时用名称+类型）合并，未出现的旧条目继续保留；
  完整上报（``partial=False``）里没再读到的条目标记 ``missing_since``，但不删除。
- 截止时间超过 ``retention_days`` 的条目自动移入 ``archived``，看板只显示
  当前条目；归档仍保留在数据文件里，可单独查看或清理。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

MAX_COURSES = 200
MAX_ITEMS_PER_COURSE = 500
MAX_ARCHIVE_PER_COURSE = 2000
DEFAULT_RETENTION_DAYS = 14


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _norm_id(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text in {"", "0", "None", "null", "undefined"} else text[:32]


def deadline_stamp(value: Any) -> float | None:
    """把 ``YYYY-MM-DD HH:MM`` / ``YYYY-MM-DD`` 转成本地时间戳。"""
    text = str(value or "").strip()
    if not text:
        return None
    for fmt, size in (("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return time.mktime(time.strptime(text[:size], fmt))
        except (ValueError, TypeError):
            continue
    return None


class DeadlineStore:
    def __init__(self, path: str, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
        self.path = path
        self.retention_days = max(1, int(retention_days))
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = {}
        self._load_dirty = False
        self._load()
        with self._lock:
            if self._load_dirty or self._prune_locked(time.time()):
                self._save()

    def _key(self, account: str, course: str) -> str:
        return f"{account}\u0000{course}"

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            groups = data.get("groups") if isinstance(data, dict) else data
        except (OSError, json.JSONDecodeError, ValueError):
            return
        if not isinstance(groups, dict):
            return
        for key, raw in groups.items():
            if not isinstance(raw, dict):
                continue
            self._data[str(key)] = self._normalize_group(raw)
            if "archived" not in raw:
                self._load_dirty = True
            for item in raw.get("items") or []:
                if isinstance(item, dict) and "first_seen" not in item:
                    self._load_dirty = True
                    break

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump({"groups": self._data}, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def _normalize_group(self, raw: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        fallback = float(raw.get("updated") or now)
        items: list[dict[str, Any]] = []
        for entry in raw.get("items") or []:
            item = self._clean_item(entry, now, fallback)
            if item:
                items.append(item)
        archived: list[dict[str, Any]] = []
        for entry in raw.get("archived") or []:
            item = self._clean_item(entry, now, fallback)
            if item:
                if entry.get("archived_at"):
                    item["archived_at"] = float(entry["archived_at"])
                else:
                    item["archived_at"] = fallback
                item["archive_reason"] = str(entry.get("archive_reason") or "expired")
                archived.append(item)
        return {
            "account": _text(raw.get("account"), 60) or "未命名账号",
            "course": _text(raw.get("course"), 120) or "未命名课程",
            "items": items,
            "archived": archived,
            "partial": bool(raw.get("partial")),
            "expected": int(raw.get("expected") or 0),
            "note": _text(raw.get("note"), 200),
            "url": _text(raw.get("url"), 500),
            "updated": fallback,
        }

    def _clean_item(
        self, raw: Any, now: float, fallback_seen: float | None = None
    ) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        name = _text(raw.get("name"), 200)
        if not name:
            return None
        seen = float(raw.get("last_seen") or raw.get("first_seen") or fallback_seen or now)
        item = {
            "tid": _norm_id(raw.get("tid")),
            "aid": _norm_id(raw.get("aid")),
            "name": name,
            "kind": _text(raw.get("kind"), 20),
            "deadline": _text(raw.get("deadline"), 40),
            "status": _text(raw.get("status"), 20),
            "first_seen": float(raw.get("first_seen") or seen),
            "last_seen": seen,
            "seen_count": max(1, int(raw.get("seen_count") or 1)),
        }
        if raw.get("missing_since"):
            item["missing_since"] = float(raw["missing_since"])
        return item

    def _name_key(self, item: dict[str, Any]) -> str:
        return "name:" + str(item.get("name") or "") + "|" + str(item.get("kind") or "")

    def _item_key(self, item: dict[str, Any]) -> str:
        tid = _norm_id(item.get("tid"))
        aid = _norm_id(item.get("aid"))
        if tid or aid:
            return f"id:{tid}|{aid}"
        return self._name_key(item)

    def _merge_items(
        self, existing: list[dict[str, Any]], incoming: list[dict[str, Any]],
        now: float, partial: bool,
    ) -> list[dict[str, Any]]:
        current: dict[str, dict[str, Any]] = {}
        for raw in existing:
            item = self._clean_item(raw, now, now)
            if item:
                current[self._item_key(item)] = item

        seen: set[str] = set()
        for raw in incoming:
            item = self._clean_item(raw, now, now)
            if not item:
                continue
            key = self._item_key(item)
            seen.add(key)
            prev_key = key
            prev = current.get(prev_key)
            if prev is None:
                # 兼容旧数据：之前没有 tid/aid，只能用名称匹配；这次带上 id 后升级键。
                fallback = self._name_key(item)
                if fallback in current:
                    prev_key = fallback
                    prev = current[fallback]
            if prev is None:
                current[key] = item
                continue
            merged = dict(prev)
            for field in ("tid", "aid", "name", "kind", "deadline", "status"):
                if item.get(field):
                    merged[field] = item[field]
            merged["last_seen"] = now
            merged["seen_count"] = int(prev.get("seen_count") or 1) + 1
            merged.pop("missing_since", None)
            if prev_key != key:
                current.pop(prev_key, None)
            current[key] = merged

        if not partial:
            # 完整上报里没再出现：可能平台删了，也可能只是这次没读到；保留但标记。
            for key, item in current.items():
                if key not in seen and not item.get("missing_since"):
                    item["missing_since"] = now
        items = sorted(
            current.values(), key=lambda item: (item.get("deadline") or "9999", item.get("name") or "")
        )
        return items[:MAX_ITEMS_PER_COURSE]

    def _prune_group(self, group: dict[str, Any], now: float) -> bool:
        cutoff = now - self.retention_days * 86400
        keep: list[dict[str, Any]] = []
        expired: list[dict[str, Any]] = []
        for item in group.get("items") or []:
            stamp = deadline_stamp(item.get("deadline"))
            if stamp is not None and stamp < cutoff:
                archived = dict(item)
                archived["archived_at"] = now
                archived["archive_reason"] = "expired"
                expired.append(archived)
            else:
                keep.append(item)
        if not expired:
            return False
        group["items"] = keep
        archive = list(group.get("archived") or []) + expired
        archive.sort(
            key=lambda item: (item.get("deadline") or "", item.get("name") or ""), reverse=True
        )
        group["archived"] = archive[:MAX_ARCHIVE_PER_COURSE]
        return True

    def _prune_locked(self, now: float) -> bool:
        changed = False
        for group in self._data.values():
            if self._prune_group(group, now):
                changed = True
        return changed

    def replace_course(
        self, account: str, course: str, items: list[dict[str, Any]],
        partial: bool = False, expected: int = 0, note: str = "", url: str = "",
    ) -> dict[str, Any]:
        account = _text(account, 60) or "未命名账号"
        course = _text(course, 120) or "未命名课程"
        now = time.time()
        key = self._key(account, course)
        with self._lock:
            prev = self._data.get(key) or {}
            merged = self._merge_items(
                prev.get("items") or [], items, now, partial=bool(partial)
            )
            group = {
                "account": account,
                "course": course,
                "items": merged,
                "archived": [dict(item) for item in (prev.get("archived") or [])],
                "partial": bool(partial),
                "expected": int(expected or 0),
                "note": _text(note, 200),
                "url": _text(url, 500),
                "updated": now,
            }
            self._data[key] = group
            self._prune_group(group, now)
            if len(self._data) > MAX_COURSES:
                for old_key in sorted(
                    self._data, key=lambda name: self._data[name].get("updated") or 0
                )[:-MAX_COURSES]:
                    self._data.pop(old_key, None)
            self._save()
            return {**group, "items": list(group.get("items") or [])}

    def groups(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            if self._prune_locked(now):
                self._save()
            rows = []
            for group in self._data.values():
                items = group.get("items") or []
                next_deadline = min(
                    (str(item.get("deadline") or "") for item in items if item.get("deadline")),
                    default="",
                )
                rows.append({
                    "account": group.get("account", ""),
                    "course": group.get("course", ""),
                    "items": items,
                    "active": len(items),
                    "archived": len(group.get("archived") or []),
                    "next": next_deadline,
                    "partial": bool(group.get("partial")),
                    "expected": int(group.get("expected") or 0),
                    "note": group.get("note", ""),
                    "url": group.get("url", ""),
                    "updated": group.get("updated") or 0,
                })
        rows.sort(key=lambda group: group.get("updated") or 0, reverse=True)
        return rows

    def all(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            if self._prune_locked(now):
                self._save()
            groups = [dict(group) for group in self._data.values()]
        items: list[dict[str, Any]] = []
        for group in groups:
            for item in group.get("items") or []:
                items.append({
                    **item,
                    "account": group.get("account", ""),
                    "course": group.get("course", ""),
                    "partial": group.get("partial", False),
                    "updated": group.get("updated") or 0,
                    "url": group.get("url", ""),
                })
        items.sort(key=lambda item: (item.get("deadline") or "9999", item.get("name") or ""))
        return items

    def archived(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            if self._prune_locked(now):
                self._save()
            groups = [dict(group) for group in self._data.values()]
        rows: list[dict[str, Any]] = []
        for group in groups:
            for item in group.get("archived") or []:
                rows.append({
                    **item,
                    "account": group.get("account", ""),
                    "course": group.get("course", ""),
                    "url": group.get("url", ""),
                })
        rows.sort(key=lambda item: item.get("archived_at") or 0, reverse=True)
        return rows

    def archive_count(self) -> int:
        with self._lock:
            return sum(len(group.get("archived") or []) for group in self._data.values())

    def clear_archive(self) -> int:
        with self._lock:
            count = sum(len(group.get("archived") or []) for group in self._data.values())
            if count:
                for group in self._data.values():
                    group["archived"] = []
                self._save()
            return count

    def clear(self) -> int:
        with self._lock:
            count = len(self._data)
            self._data = {}
            self._save()
            return count

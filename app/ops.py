"""运维日志 + 探针心跳的轻量持久化。"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any


class JsonStore:
    def __init__(self, path: str, default: Any) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data = default
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if isinstance(data, type(self._data)):
                self._data = data
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(self._data, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def read(self) -> Any:
        with self._lock:
            return json.loads(json.dumps(self._data, ensure_ascii=False))

    def write(self, data: Any) -> None:
        with self._lock:
            self._data = data
            self._save()


class OpsLog:
    def __init__(self, path: str, limit: int = 200) -> None:
        self.store = JsonStore(path, {"runs": [], "meta": {}})
        self.limit = limit

    def add_run(self, name: str, status: str, detail: Any = None, started: float | None = None) -> dict[str, Any]:
        with self.store._lock:
            data = self.store.read()
            run = {
                "name": name,
                "status": status,
                "at": time.time(),
                "started": started or time.time(),
                "detail": detail if isinstance(detail, (dict, list)) else str(detail or ""),
            }
            runs = [run] + list(data.get("runs") or [])
            data["runs"] = runs[: self.limit]
            self.store.write(data)
            return run

    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self.store.read().get("runs") or [])[:limit]

    def meta(self) -> dict[str, Any]:
        return dict(self.store.read().get("meta") or {})

    def set_meta(self, key: str, value: Any) -> None:
        with self.store._lock:
            data = self.store.read()
            meta = dict(data.get("meta") or {})
            meta[key] = value
            data["meta"] = meta
            self.store.write(data)


class ProbeStore:
    def __init__(self, path: str) -> None:
        self.store = JsonStore(path, {})

    def touch(self, name: str, ver: str = "", url: str = "", extra: Any = None) -> dict[str, Any]:
        name = str(name or "").strip() or "unknown"
        ver = str(ver or "").strip()[:40]
        with self.store._lock:
            data = self.store.read()
            item = {
                "name": name,
                "ver": ver,
                "url": str(url or "")[:300],
                "last_seen": time.time(),
                "extra": extra if isinstance(extra, dict) else {},
            }
            data[name] = item
            self.store.write(data)
            return item

    def all(self, stale_after: int = 900) -> dict[str, Any]:
        now = time.time()
        data = self.store.read()
        for item in data.values():
            seen = float(item.get("last_seen") or 0)
            item["age"] = int(now - seen) if seen else None
            item["online"] = bool(seen and now - seen <= stale_after)
        return data


class VoteQueue:
    """冲突题投票的人工复核队列：dry-run 结果先排队，人工确认后再写库。"""

    def __init__(self, path: str, limit: int = 200) -> None:
        self.store = JsonStore(path, {"items": []})
        self.limit = limit

    def add(self, item: dict[str, Any], winner: str, info: Any = None) -> dict[str, Any]:
        entry = {
            "id": item.get("id"),
            "title": str(item.get("title") or "")[:200],
            "type": item.get("type"),
            "subject": item.get("subject"),
            "current": str(item.get("answer") or ""),
            "suggestion": str(winner or ""),
            "votes": info if isinstance(info, dict) else {},
            "at": time.time(),
        }
        with self.store._lock:
            data = self.store.read()
            items = [row for row in (data.get("items") or []) if row.get("id") != entry["id"]]
            items.insert(0, entry)
            data["items"] = items[: self.limit]
            self.store.write(data)
        return entry

    def items(self) -> list[dict[str, Any]]:
        return list(self.store.read().get("items") or [])

    def remove(self, ids: list[str]) -> int:
        ids = set(ids)
        with self.store._lock:
            data = self.store.read()
            before = data.get("items") or []
            data["items"] = [row for row in before if row.get("id") not in ids]
            self.store.write(data)
            return len(before) - len(data["items"])

"""HTTP 请求日志：sqlite 持久化，支撑 p50/p95 与慢请求 TopN。"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any


class HttpLog:
    def __init__(self, path: str, max_rows: int = 5000) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.max_rows = max_rows
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS http_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, method TEXT, ms REAL, status INTEGER, at REAL)"
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_http_at ON http_log(at)")
        self._conn.commit()

    def add(self, path: str, method: str, ms: float, status: int, at: float) -> None:
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO http_log (path, method, ms, status, at) VALUES (?, ?, ?, ?, ?)",
                    (path, method, float(ms), int(status), float(at)),
                )
                self._conn.execute(
                    "DELETE FROM http_log WHERE id <= (SELECT MAX(id) FROM http_log) - ?",
                    (self.max_rows,),
                )
        except sqlite3.Error:
            pass

    def recent(self, limit: int = 800) -> list[dict[str, Any]]:
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT path, method, ms, status, at FROM http_log ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        except sqlite3.Error:
            return []
        return [
            {"path": row[0], "method": row[1], "ms": row[2], "status": row[3], "at": row[4]}
            for row in rows
        ]

    def slow(self, limit: int = 10, since: float | None = None) -> list[dict[str, Any]]:
        try:
            with self._lock:
                if since:
                    rows = self._conn.execute(
                        "SELECT path, method, ms, status, at FROM http_log WHERE at >= ? ORDER BY ms DESC LIMIT ?",
                        (float(since), int(limit)),
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        "SELECT path, method, ms, status, at FROM http_log ORDER BY ms DESC LIMIT ?",
                        (int(limit),),
                    ).fetchall()
        except sqlite3.Error:
            return []
        return [
            {"path": row[0], "method": row[1], "ms": round(row[2], 1), "status": row[3], "at": row[4]}
            for row in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

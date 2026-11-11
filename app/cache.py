"""答案缓存：同一道题只花一次 token。用 sqlite，重启也在。"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time

from .bank import DEFAULT_SUBJECT
from .parsing import parse_options


class AnswerCache:
    def __init__(
        self,
        path: str,
        ttl_hours: float = 168,
        completion_ttl_hours: float = 24,
    ) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.ttl = max(0.0, float(ttl_hours)) * 3600
        # 填空题没有选项可锚定，AI 单次作答的答案更容易漂移（同一模型换个
        # 采样就可能给出不同的近似值）。用更短的 TTL，避免一条错答案被锁一周。
        # 0 表示不单独设置，沿用全局 ttl。
        self.completion_ttl = max(0.0, float(completion_ttl_hours)) * 3600
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS answers (
                key      TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                answer   TEXT NOT NULL,
                provider TEXT,
                created  REAL NOT NULL
            )
            """
        )
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(answers)")}
        for name in ("qtype", "options", "subject", "image_hash"):
            if name not in columns:
                self._conn.execute(f"ALTER TABLE answers ADD COLUMN {name} TEXT")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transcripts (
                image_hash TEXT PRIMARY KEY,
                text       TEXT NOT NULL,
                source     TEXT,
                created    REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    @staticmethod
    def make_key(
        qtype: str, title: str, options_raw, images: list[str] | None = None,
        subject: str | None = None, context: str | None = None,
    ) -> str:
        options = parse_options(options_raw)
        subj = str(subject or DEFAULT_SUBJECT).strip() or DEFAULT_SUBJECT
        ctx = str(context or "").strip()[:300]
        raw = f"{qtype}\x00{title.strip()}\x00{'|'.join(options)}\x00{subj}\x00{ctx}"
        if images:
            raw += "\x00" + hashlib.sha256("\x00".join(images).encode("utf-8")).hexdigest()
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _ttl_for(self, qtype: str | None) -> float:
        """填空题优先用更短的 TTL；返回 0 表示永不过期。"""
        kind = str(qtype or "").strip().lower()
        if kind == "completion" and self.completion_ttl > 0:
            return self.completion_ttl
        return self.ttl

    def get(self, key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT answer, provider, created, qtype FROM answers WHERE key = ?", (key,)
            ).fetchone()
            if not row:
                return None
            answer, provider, created, qtype = row
            ttl = self._ttl_for(qtype)
            if ttl and (time.time() - created) > ttl:
                self._conn.execute("DELETE FROM answers WHERE key = ?", (key,))
                self._conn.commit()
                return None
            return {"answer": answer, "provider": provider, "created": created}

    def set(
        self,
        key: str,
        question: str,
        answer: str,
        provider: str,
        qtype: str | None = None,
        options: str | None = None,
        subject: str | None = None,
        image_hash: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO answers "
                "(key, question, answer, provider, qtype, options, subject, image_hash, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key, question[:500], answer, provider, qtype, options, subject, image_hash, time.time()),
            )
            self._conn.commit()

    def invalidate(self, qtype: str | None, title: str) -> int:
        """题库记录变更后删掉对应缓存。

        缓存 key 里含 context / 图片指纹等题库记录无法完整还原的字段，所以按
        「题型 + 题干」这一对最稳定的身份删除：同一道题在不同作业上下文下的
        缓存变体一并清掉。宁可多删一条重新作答，也不留下与题库矛盾的旧答案。
        """
        question = str(title or "").strip()[:500]
        if not question:
            return 0
        with self._lock:
            if qtype:
                cursor = self._conn.execute(
                    "DELETE FROM answers WHERE qtype = ? AND question = ?",
                    (str(qtype), question),
                )
            else:
                cursor = self._conn.execute(
                    "DELETE FROM answers WHERE question = ?", (question,)
                )
            self._conn.commit()
            return int(cursor.rowcount or 0)

    def items(self) -> list[dict]:
        """未过期的缓存题目（用于「缓存转题库」）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT question, answer, provider, qtype, options, subject, image_hash, created FROM answers"
            ).fetchall()
        now = time.time()
        out = []
        for row in rows:
            ttl = self._ttl_for(row[3])
            if ttl and (now - float(row[7] or 0)) > ttl:
                continue
            out.append({
                "question": row[0],
                "answer": row[1],
                "provider": row[2],
                "qtype": row[3],
                "options": row[4],
                "subject": row[5],
                "image_hash": row[6],
                "created": row[7],
            })
        return out

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM answers").fetchone()
            return int(row[0]) if row else 0

    def get_transcript(self, image_hash: str) -> dict | None:
        if not image_hash:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT text, source, created FROM transcripts WHERE image_hash = ?", (image_hash,)
            ).fetchone()
        if not row:
            return None
        return {"text": row[0], "source": row[1], "created": row[2]}

    def set_transcript(self, image_hash: str, text: str, source: str = "") -> None:
        if not image_hash or not text:
            return
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO transcripts (image_hash, text, source, created) VALUES (?, ?, ?, ?)",
                (image_hash, text[:20000], source, time.time()),
            )
            self._conn.commit()

    def clear(self) -> int:
        with self._lock:
            total = self.count()
            self._conn.execute("DELETE FROM answers")
            self._conn.commit()
            return total

    def close(self) -> None:
        with self._lock:
            self._conn.close()

"""答案缓存：同一道题只花一次 token。用 sqlite，重启也在。"""

from __future__ import annotations

import hashlib
import os
import random
import sqlite3
import threading
import time

from . import config
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
        for name in ("qtype", "options", "subject", "image_hash", "source", "confidence"):
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
                "SELECT answer, provider, created, qtype, source, confidence "
                "FROM answers WHERE key = ?",
                (key,),
            ).fetchone()
            if not row:
                return None
            answer, provider, created, qtype, source, confidence = row
            ttl = self._ttl_for(qtype)
            if ttl and (time.time() - created) > ttl:
                self._conn.execute("DELETE FROM answers WHERE key = ?", (key,))
                self._conn.commit()
                return None
            try:
                confidence_value = float(confidence) if confidence not in (None, "") else None
            except (TypeError, ValueError):
                confidence_value = None
            return {
                "answer": answer,
                "provider": provider,
                "created": created,
                "source": source or "",
                "confidence": confidence_value,
            }

    @staticmethod
    def _retry_allowed(row: dict, floor_value: float) -> bool:
        """只有 AI 来源、且置信度低于阈值的记录才值得重问。

        题库/官方答案命中永不重问——那是权威来源，换掉只会更差。
        """
        source = str(row.get("source") or "").strip().lower()
        if source not in ("ai", "unit-vote"):
            return False
        confidence = row.get("confidence")
        if confidence is None:
            return False
        try:
            value = float(confidence)
        except (TypeError, ValueError):
            return False
        return value < floor_value

    def lookup_for_answer(
        self,
        key: str,
        retry_prob: float | None = None,
        min_confidence: float | None = None,
    ) -> tuple[dict | None, bool]:
        """返回 ``(命中记录, 是否因低置信被要求重解)``。

        命中后按 ``retry_prob`` 概率决定是否忽略这次命中（重解结果会覆盖旧值），
        用来避免一次低质量答案被缓存永久钉死。
        """
        row = self.get(key)
        if row is None:
            return None, False
        prob = config.CACHE_RETRY_PROBABILITY if retry_prob is None else float(retry_prob)
        floor_value = (
            config.CACHE_RETRY_MIN_CONFIDENCE if min_confidence is None
            else float(min_confidence)
        )
        if prob <= 0 or not self._retry_allowed(row, floor_value):
            return row, False
        if random.random() < prob:
            return None, True
        return row, False

    def get_for_answer(
        self,
        key: str,
        retry_prob: float | None = None,
        min_confidence: float | None = None,
    ) -> dict | None:
        """``lookup_for_answer`` 的简化版：只关心拿到的记录（可能为 None）。"""
        return self.lookup_for_answer(key, retry_prob, min_confidence)[0]

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
        source: str = "",
        confidence: float | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO answers "
                "(key, question, answer, provider, qtype, options, subject, image_hash, "
                "source, confidence, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key, question[:500], answer, provider, qtype, options, subject,
                 image_hash, source, confidence, time.time()),
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

"""单元测验会话：记录每道题的答题过程，供面板在提交前复核。"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any

MAX_ANSWERS = 200
MAX_SESSIONS = 30
IDLE_SECONDS = 45 * 60


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


class UnitSessions:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._sessions: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            sessions = data.get("sessions") if isinstance(data, dict) else data
            if isinstance(sessions, list):
                self._sessions = [s for s in sessions if isinstance(s, dict)][-MAX_SESSIONS:]
        except (OSError, json.JSONDecodeError, ValueError):
            self._sessions = []

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump({"sessions": self._sessions[-MAX_SESSIONS:]}, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def _active(self) -> dict[str, Any] | None:
        if not self._sessions:
            return None
        last = self._sessions[-1]
        if last.get("finalized"):
            return None
        if time.time() - float(last.get("updated") or 0) > IDLE_SECONDS:
            return None
        return last

    def ensure(self, url: str = "", total: Any = 0, tid: str = "") -> dict[str, Any]:
        """单元测验页上报场景时调用：没有活动会话就新建。"""
        with self._lock:
            session = self._active()
            now = time.time()
            if session is None:
                session = {
                    "id": _new_id(), "started": now, "updated": now, "url": str(url or "")[:400],
                    "tid": str(tid or "")[:64], "total": int(total or 0), "finalized": 0,
                    "finalized_at": 0.0, "answers": [],
                }
                self._sessions.append(session)
            else:
                session["updated"] = now
                if url:
                    session["url"] = str(url)[:400]
                if tid:
                    session["tid"] = str(tid)[:64]
                if total:
                    session["total"] = int(total)
            self._save()
            return dict(session)

    def add_answer(self, qtype: str, title: str, answer: str, meta: dict[str, Any]) -> None:
        title = str(title or "").strip()
        if not title:
            return
        with self._lock:
            session = self._active()
            if session is None:
                session = {
                    "id": _new_id(), "started": time.time(), "url": "", "tid": "",
                    "total": 0, "finalized": 0, "finalized_at": 0.0, "answers": [],
                }
                self._sessions.append(session)
            answers = session.get("answers") or []
            entry = {
                # 题干上限与题库一致（bank.MAX_TITLE = 2000）：公式图片题的题干
                # 带一串图片 URL，很容易超过 300 字，截断后这道题再也匹配不回来。
                "title": title[:2000], "type": str(qtype or "")[:20],
                "answer": str(answer or "")[:400],
                "source": str(meta.get("source") or "")[:20],
                "provider": str(meta.get("provider") or "")[:60],
                "match": str(meta.get("match") or "")[:30],
                "cross": str(meta.get("cross") or "")[:20],
                "vote": meta.get("vote") if isinstance(meta.get("vote"), dict) else None,
                "consensus": str(meta.get("consensus") or "")[:24],
                "official_check": str(meta.get("official_check") or "")[:20],
                "official_answer": str(meta.get("official_answer") or "")[:400],
                "ai_answer": str(meta.get("ai_answer") or "")[:400],
                "official_agree": int(meta.get("official_agree") or 0),
                "official_share": (
                    float(meta["official_share"])
                    if meta.get("official_share") is not None else None
                ),
                "risk": bool(meta.get("risk")),
                "at": time.time(),
            }
            replaced = False
            for index, old in enumerate(answers):
                if old.get("title") == entry["title"]:
                    answers[index] = entry
                    replaced = True
                    break
            if not replaced:
                answers.append(entry)
            session["answers"] = answers[-MAX_ANSWERS:]
            session["updated"] = time.time()
            self._save()

    def current(self) -> dict[str, Any] | None:
        with self._lock:
            session = self._active()
            if not session:
                return None
            answers = list(session.get("answers") or [])
            answered = len(answers)
            risk = sum(1 for item in answers if item.get("risk"))
            return {
                "id": session.get("id"), "started": session.get("started"),
                "updated": session.get("updated"), "url": session.get("url"),
                "tid": session.get("tid"), "total": session.get("total") or 0,
                "answered": answered, "risk": risk,
                "finalized": bool(session.get("finalized")),
                "answers": answers,
            }

    def remove_answer(self, title: str) -> bool:
        """删除活动会话里标题匹配的一条记录；返回是否真的删掉。

        只动活动会话，已定稿/过期的历史会话保持原样，避免误删复盘数据。
        """
        key = str(title or "").strip()
        if not key:
            return False
        with self._lock:
            session = self._active()
            if session is None:
                return False
            answers = session.get("answers") or []
            kept = [item for item in answers if str(item.get("title") or "") != key[:2000]]
            if len(kept) == len(answers):
                return False
            session["answers"] = kept
            session["updated"] = time.time()
            self._save()
            return True

    def finalize(self, on: bool = True) -> dict[str, Any] | None:
        with self._lock:
            session = self._active() or (self._sessions[-1] if self._sessions else None)
            if not session:
                return None
            session["finalized"] = 1 if on else 0
            session["finalized_at"] = time.time() if on else 0.0
            session["updated"] = time.time()
            self._save()
            return dict(session)

    def clear(self) -> int:
        with self._lock:
            count = len(self._sessions)
            self._sessions = []
            self._save()
            return count

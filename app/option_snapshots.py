"""答题页选项快照：把 OCS 只传 A/B/C/D 的图片选项还原成真实内容。

OCS 在选项是图片时只传得出字母，网关只能按位置猜（``bank.find_by_position``）。
油猴脚本在答题页把每个选项的图片/文本抓下来，按「题型 + 规范题干 + 选项数」
上报到这里；``/query`` 再遇到同一道题的裸字母请求时：

- 有历史答案：按选项指纹把答案重映射到当前字母，不再依赖位置；
- 只有快照：用真实选项（图片 data URL / 文本）继续原有作答链路。

快照只在内存里保留 10 分钟（图片可能很大，不落盘）；答案只存指纹，持久化到
``option_snapshots.json``，避免把字母和选项顺序绑死。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from typing import Any

from .bank import canon_text
from .parsing import normalize_type

MAX_SNAPSHOTS = 300
MAX_ANSWERS = 2000
MAX_ORDER_HISTORY = 5
MAX_OPTION_IMAGE_BYTES = 1_200_000
MAX_TOTAL_IMAGE_BYTES = 5_000_000
MAX_OPTIONS = 12
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _env_seconds(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "") or default))
    except ValueError:
        return default


SNAPSHOT_TTL_SECONDS = _env_seconds("OPTION_SNAPSHOT_TTL_SECONDS", 600)
ANSWER_TTL_SECONDS = _env_seconds("OPTION_SNAPSHOT_ANSWER_TTL_SECONDS", 30 * 86400)

_DATA_URL_RE = re.compile(r"^data:image/(png|jpeg|jpg|webp);base64,", re.I)


def snapshot_key(qtype_raw: Any, title: Any, count: int) -> str:
    qtype = normalize_type(qtype_raw) or str(qtype_raw or "").strip().lower()
    raw = "%s\x00%s\x00%d" % (canon_text(title), qtype, int(count))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def option_fingerprint(text: Any, image: Any) -> str:
    """选项内容指纹：有图用图字节，没图用规范化文本。"""
    image = str(image or "").strip()
    if image:
        match = _DATA_URL_RE.match(image)
        if not match:
            return ""
        try:
            payload = base64.b64decode(image[match.end():], validate=False)
        except (ValueError, TypeError):
            return ""
        return "i:" + hashlib.sha256(payload).hexdigest()[:24]
    text = canon_text(text)
    return ("t:" + text[:64]) if text else ""


def _dice(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    a = {left[i:i + 2] for i in range(max(0, len(left) - 1))}
    b = {right[i:i + 2] for i in range(max(0, len(right) - 1))}
    if not a or not b:
        return 0.0
    return 2.0 * len(a & b) / (len(a) + len(b))


class OptionSnapshots:
    """内存快照 + JSON 答案存储。"""

    def __init__(self, answers_path: str, clock=time.time) -> None:
        self.path = answers_path
        self.clock = clock
        self._lock = threading.RLock()
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._answers: dict[str, dict[str, Any]] = {}
        self._bytes = 0
        self._remaps = 0
        os.makedirs(os.path.dirname(answers_path) or ".", exist_ok=True)
        self._load()

    # ------------------------------------------------------------------ 持久化
    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fp:
                data = json.load(fp)
            rows = data.get("answers") if isinstance(data, dict) else None
            if isinstance(rows, dict):
                self._answers = {str(k): v for k, v in rows.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            self._answers = {}

    def _save(self) -> None:
        payload = {"answers": self._answers, "saved_at": self.clock()}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ 清理
    def _prune(self, now: float) -> None:
        expired = [
            key for key, row in self._snapshots.items()
            if now - float(row.get("at") or 0) > SNAPSHOT_TTL_SECONDS
        ]
        for key in expired:
            self._bytes -= int(self._snapshots[key].get("bytes") or 0)
            self._snapshots.pop(key, None)
        # 超量时按最近使用时间淘汰
        if len(self._snapshots) > MAX_SNAPSHOTS or self._bytes > MAX_TOTAL_IMAGE_BYTES:
            for key, _row in sorted(
                self._snapshots.items(), key=lambda pair: float(pair[1].get("at") or 0)
            ):
                if len(self._snapshots) <= MAX_SNAPSHOTS and self._bytes <= MAX_TOTAL_IMAGE_BYTES:
                    break
                self._bytes -= int(self._snapshots[key].get("bytes") or 0)
                self._snapshots.pop(key, None)
        expired_answers = [
            key for key, row in self._answers.items()
            if now - float(row.get("at") or 0) > ANSWER_TTL_SECONDS
        ]
        for key in expired_answers:
            self._answers.pop(key, None)
        if len(self._answers) > MAX_ANSWERS:
            for key, _row in sorted(
                self._answers.items(), key=lambda pair: float(pair[1].get("at") or 0)
            )[: len(self._answers) - MAX_ANSWERS]:
                self._answers.pop(key, None)

    # ------------------------------------------------------------------ 写入
    def put(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.clock()
        title = str(payload.get("title") or "").strip()
        if not title:
            raise ValueError("title 不能为空")
        title = title[:2000]
        qtype = normalize_type(payload.get("type"))
        if qtype is None:
            raise ValueError("不支持的题型")
        raw_options = payload.get("options")
        if not isinstance(raw_options, list) or len(raw_options) > MAX_OPTIONS:
            raise ValueError("options 必须是数组且不超过 %d 项" % MAX_OPTIONS)
        if qtype in ("single", "multiple", "judge") and len(raw_options) < 2:
            raise ValueError("选项至少需要两个")

        options: list[dict[str, Any]] = []
        total_bytes = 0
        missing_images = False
        for index, raw in enumerate(raw_options):
            if not isinstance(raw, dict):
                raise ValueError("options 每一项必须是对象")
            letter = str(raw.get("letter") or LETTERS[index] if index < len(LETTERS) else "").strip().upper()
            if letter not in LETTERS:
                letter = LETTERS[index] if index < len(LETTERS) else ""
            text = str(raw.get("text") or "").strip()[:2000]
            image = str(raw.get("image") or "").strip()
            size = 0
            if image:
                match = _DATA_URL_RE.match(image)
                if not match:
                    raise ValueError("image 只接受 png/jpeg/webp 的 data URL")
                try:
                    size = len(base64.b64decode(image[match.end():], validate=False))
                except (ValueError, TypeError):
                    raise ValueError("image base64 无法解码") from None
                if size > MAX_OPTION_IMAGE_BYTES:
                    raise ValueError("单个选项图片超过 %d 字节" % MAX_OPTION_IMAGE_BYTES)
                total_bytes += size
            elif not text:
                missing_images = True
            options.append({
                "letter": letter,
                "text": text,
                "image": image,
                "size": size,
                "fp": option_fingerprint(text, image),
            })
        if total_bytes > MAX_TOTAL_IMAGE_BYTES:
            raise ValueError("选项图片总大小超过 %d 字节" % MAX_TOTAL_IMAGE_BYTES)
        if not any(option["fp"] for option in options):
            raise ValueError("没有任何可识别的选项内容")

        key = snapshot_key(qtype, title, len(options))
        order_fp = hashlib.sha256(
            "|".join(option["fp"] for option in options).encode("utf-8")
        ).hexdigest()[:16]
        order = [option["letter"] for option in options]
        with self._lock:
            self._prune(now)
            previous = self._snapshots.get(key)
            history = list((previous or {}).get("order_history") or [])
            order_changed = bool(previous and previous.get("order_fp") != order_fp)
            if order_changed:
                history.append({
                    "at": float(previous.get("at") or 0),
                    "order_fp": str(previous.get("order_fp") or ""),
                    "order": list(previous.get("order") or []),
                })
                history = history[-MAX_ORDER_HISTORY:]
            self._bytes -= int((previous or {}).get("bytes") or 0)
            self._snapshots[key] = {
                "key": key,
                "title": title,
                "qtype": qtype,
                "count": len(options),
                "subject": str(payload.get("subject") or "")[:80],
                "url": str(payload.get("url") or "")[:500],
                "client_id": str(payload.get("client_id") or "")[:80],
                "options": options,
                "order": order,
                "order_fp": order_fp,
                "order_history": history,
                "pending_images": bool(payload.get("pending_images") or missing_images),
                "bytes": total_bytes,
                "at": now,
            }
            self._bytes += total_bytes
            answer = self._answer_for(key, options)
            self._prune(now)
            return {
                "ok": True,
                "key": key,
                "order_changed": order_changed,
                "previous_order": history[-1]["order"] if order_changed else [],
                "current_order": order,
                "missing_images": missing_images,
                "answer": answer,
            }

    def lookup(self, qtype_raw: Any, title: Any, count: int) -> dict[str, Any] | None:
        """先精确 key，再同题型同选项数的模糊题干匹配（阈值 0.9）。"""
        now = self.clock()
        key = snapshot_key(qtype_raw, title, count)
        with self._lock:
            self._prune(now)
            row = self._snapshots.get(key)
            if row and now - float(row.get("at") or 0) <= SNAPSHOT_TTL_SECONDS:
                return row
            target = canon_text(title)
            best = None
            best_score = 0.0
            for candidate in self._snapshots.values():
                if int(candidate.get("count") or 0) != int(count):
                    continue
                if normalize_type(qtype_raw) != candidate.get("qtype"):
                    continue
                score = _dice(target, canon_text(candidate.get("title")))
                if score > best_score or (score == best_score and best and
                                          float(candidate.get("at") or 0) > float(best.get("at") or 0)):
                    best, best_score = candidate, score
            return best if best is not None and best_score >= 0.9 else None

    # ------------------------------------------------------------------ 答案
    def _answer_for(self, key: str, options: list[dict[str, Any]]) -> dict[str, Any] | None:
        row = self._answers.get(key)
        if not isinstance(row, dict):
            return None
        selected = [str(item) for item in (row.get("selected") or []) if str(item)]
        if not selected:
            return None
        letters: list[str] = []
        used: set[int] = set()
        for fingerprint in selected:
            matches = [
                index for index, option in enumerate(options)
                if option.get("fp") == fingerprint
            ]
            if len(matches) != 1 or matches[0] in used:
                return None
            used.add(matches[0])
            letters.append(str(options[matches[0]]["letter"]))
        now = self.clock()
        confidence = float(row.get("confidence") or 0)
        age = now - float(row.get("at") or 0)
        return {
            "letters": "#".join(sorted(letters)),
            "source": str(row.get("source") or "snapshot"),
            "confidence": confidence if confidence else None,
            "risk": bool(confidence and confidence < 0.65) or age > 86400,
            "at": float(row.get("at") or 0),
        }

    def answer_for(self, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            self._prune(self.clock())
            return self._answer_for(str(snapshot.get("key") or ""),
                                    list(snapshot.get("options") or []))

    def record_answer(
        self, key: str, answer: str, options: list[dict[str, Any]],
        source: str = "ai", confidence: float | None = None,
    ) -> bool:
        answer = str(answer or "").strip().upper()
        if not key or not answer:
            return False
        parts = [part.strip().upper() for part in answer.split("#") if part.strip()]
        if not parts or any(part not in LETTERS for part in parts):
            return False
        selected: list[str] = []
        for part in parts:
            index = LETTERS.index(part)
            if index >= len(options):
                return False
            fingerprint = str(options[index].get("fp") or "")
            if not fingerprint:
                return False
            selected.append(fingerprint)
        now = self.clock()
        with self._lock:
            self._answers[key] = {
                "selected": selected,
                "answer": "#".join(parts),
                "source": str(source or "ai")[:40],
                "confidence": float(confidence) if isinstance(confidence, (int, float)) else None,
                "at": now,
            }
            self._prune(now)
            self._save()
        return True

    def record_selected(
        self, key: str, fingerprints: list[str], source: str = "official",
        confidence: float | None = 1.0,
    ) -> bool:
        """按选项指纹记录答案（官方回流用）。仅当指纹都能在现存快照里唯一找到时接受。"""
        key = str(key or "")
        selected = [str(item) for item in (fingerprints or []) if str(item)]
        if not key or not selected:
            return False
        now = self.clock()
        with self._lock:
            self._prune(now)
            snapshot = self._snapshots.get(key)
            if not isinstance(snapshot, dict):
                return False
            if now - float(snapshot.get("at") or 0) > SNAPSHOT_TTL_SECONDS:
                return False
            known = [str(option.get("fp") or "") for option in snapshot.get("options") or []]
            if any(known.count(fingerprint) != 1 for fingerprint in selected):
                return False
            self._answers[key] = {
                "selected": selected,
                "source": str(source or "official")[:40],
                "confidence": float(confidence) if isinstance(confidence, (int, float)) else None,
                "order_fp": str(snapshot.get("order_fp") or ""),
                "at": now,
            }
            self._prune(now)
            self._save()
        return True

    # ------------------------------------------------------------------ 管理
    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        now = self.clock()
        with self._lock:
            self._prune(now)
            rows = []
            for snapshot in self._snapshots.values():
                answer = self._answer_for(
                    str(snapshot.get("key") or ""), list(snapshot.get("options") or []))
                rows.append({
                    "key": str(snapshot.get("key") or ""),
                    "title": str(snapshot.get("title") or ""),
                    "qtype": str(snapshot.get("qtype") or ""),
                    "count": int(snapshot.get("count") or 0),
                    "subject": str(snapshot.get("subject") or ""),
                    "url": str(snapshot.get("url") or ""),
                    "order": list(snapshot.get("order") or []),
                    "order_fp": str(snapshot.get("order_fp") or ""),
                    "order_history": list(snapshot.get("order_history") or []),
                    "pending_images": bool(snapshot.get("pending_images")),
                    "bytes": int(snapshot.get("bytes") or 0),
                    "at": float(snapshot.get("at") or 0),
                    "age_seconds": int(max(0, now - float(snapshot.get("at") or 0))),
                    "answer": answer,
                })
            rows.sort(key=lambda row: row["at"], reverse=True)
            return rows[:max(1, int(limit))]

    def remove(self, key: str) -> bool:
        key = str(key or "")
        if not key:
            return False
        with self._lock:
            snapshot = self._snapshots.pop(key, None)
            if snapshot is not None:
                self._bytes -= int(snapshot.get("bytes") or 0)
            removed_answer = self._answers.pop(key, None) is not None
            if snapshot is not None or removed_answer:
                self._save()
                return True
            return False

    def bump_remap(self) -> None:
        with self._lock:
            self._remaps += 1

    # ------------------------------------------------------------------ 统计
    def stats(self) -> dict[str, Any]:
        now = self.clock()
        with self._lock:
            self._prune(now)
            changes = sum(
                max(0, len(row.get("order_history") or []))
                for row in self._snapshots.values()
            )
            return {
                "snapshots": len(self._snapshots),
                "answers": len(self._answers),
                "order_changes": changes,
                "remap_hits": self._remaps,
                "bytes": self._bytes,
            }

    def clear(self) -> None:
        with self._lock:
            self._snapshots.clear()
            self._answers.clear()
            self._bytes = 0
            self._save()

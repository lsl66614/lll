"""答题记忆：事件级 episode + 题目级 consolidated memory。

它不替代题库，而是回答四个问题：

1. 这道题以前答过吗？谁答的？当时是什么策略和置信度？
2. 官方回流 / 人工裁决之后，哪些历史答案是对的、哪些是错的？
3. 题库和缓存都没命中时，有没有「已被官方确认」的答案可以直接复用？
4. 问模型之前，能不能提醒它「这道相似题以前有人答错过 X」？

安全边界（比「多存一份答案」重要）：

- 只有官方回流、人工裁决、投票队列确认过的答案，才允许 ``recall()`` 自动返回；
  AI 自答但未经确认的记忆只用于提示，不作为标准答案。
- 填空题没有选项锚定，不做模糊匹配（沿用题库语义错配的教训）。
- 图片题要求图片指纹一致；否定词签名不同的题不互相提示。
- 字母答案在返回前按「选项内容」重新翻译，避免选项换序后指向另一个选项。
- 记忆会衰减：长期未再出现且未被确认的 episode 到期清理，不无限膨胀。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from typing import Any

from .bank import (
    DEFAULT_SUBJECT,
    _bigrams,
    _negation_signature,
    _norm_option,
    translate_answer,
    translate_answer_loose,
)
from .mooc_result import _answers_match
from .parsing import normalize_type, parse_options
from .shape_key import image_key, text_key

# 模糊提示的文本相似度门槛。题库匹配用 0.9；记忆提示只用来提醒「相似题」，
# 取更高一档，宁可少提示，也不要把另一道题的错误答案扣到当前题头上。
FUZZY_THRESHOLD = 0.92
# 每次最多扫描多少条 memory 候选（按最近出现排序）。题库量级下足够。
MAX_HINT_CANDIDATES = 2000
# 同一题、同一答案、同一来源在这个窗口内重复出现，只累加 repeat，不新增 episode。
RECENT_DEDUPE_SECONDS = 600.0
# 这些 outcome 来源被视为「已确认」，允许 recall 自动返回。
TRUSTED_OUTCOME_SOURCES = ("official", "feedback", "paper", "page", "vote", "manual")
# 这些来源被确认的答案可以跨专业复用（官方/人工优先级高于专业标签）。
CROSS_SUBJECT_SOURCES = ("official", "manual")


def _options_key(qtype: str, options: list[str]) -> str:
    """选项内容指纹：单选/多选排序后比较，判断题按语义归一。"""
    if qtype in ("single", "multiple"):
        return "|".join(sorted(_norm_option(option, qtype) for option in options))
    if qtype == "judge":
        return "|".join(_norm_option(option, qtype) for option in options[:2])
    return ""


def _image_identity(title: str, options: list[str], image_hash: str = "") -> str:
    """图片身份：优先用请求 base64 的 sha256，否则用题干/选项里的 URL 指纹。"""
    raw = str(image_hash or "").strip()
    if raw:
        return "h:" + raw
    blob = " ".join([str(title or "")] + [str(option or "") for option in options])
    key = image_key(blob)
    return ("u:" + key) if key else ""


def question_fingerprint(
    qtype: Any, title: Any, options: Any, image_hash: str = ""
) -> dict[str, str]:
    """题目稳定指纹。qkey 不包含专业，专业作为独立字段在召回时判断。"""
    qtype_norm = normalize_type(qtype) or str(qtype or "").strip().lower()
    option_list = parse_options(options)
    text_key_value = text_key(title)
    options_key_value = _options_key(qtype_norm, option_list)
    img_key_value = _image_identity(str(title or ""), option_list, image_hash)
    raw = "\x00".join([qtype_norm, text_key_value, options_key_value, img_key_value])
    return {
        "qkey": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "qtype": qtype_norm,
        "text_key": text_key_value,
        "options_key": options_key_value,
        "img_key": img_key_value,
        "image_hash": str(image_hash or "").strip(),
    }


def _json_load(value: Any, default: Any) -> Any:
    try:
        loaded = json.loads(str(value or ""))
        return loaded if isinstance(loaded, type(default)) else default
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _img_compatible(left: str, right: str, *, strict: bool) -> bool:
    """图片身份是否可比较。

    strict=True 用于召回/提示：一方缺失就拒绝，避免带图题蹭到纯文字题。
    strict=False 用于结果回流：反馈页常常没有 base64 图片，允许一方缺失。
    """
    left = str(left or "")
    right = str(right or "")
    if left == right:
        return True
    if not left or not right:
        return not strict
    return False


def _age_days(now: float, moment: float) -> float:
    return max(0.0, (now - float(moment or 0)) / 86400.0)


def _age_text(days: float) -> str:
    if days < 1:
        return "今天"
    if days < 30:
        return f"{int(days)} 天前"
    if days < 365:
        return f"{int(days / 30)} 个月前"
    return f"{int(days / 365)} 年前"


class AnswerMemory:
    """答题记忆的 SQLite 存储与检索。"""

    def __init__(self, path: str, *, prune_on_init: bool = False) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._hint_cache: dict[str, list[dict[str, Any]]] = {}
        self._init_schema()
        if prune_on_init:
            try:
                self.prune()
            except (sqlite3.Error, OSError):
                pass

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    qkey TEXT NOT NULL,
                    qtype TEXT NOT NULL,
                    text_key TEXT NOT NULL DEFAULT '',
                    options_key TEXT NOT NULL DEFAULT '',
                    img_key TEXT NOT NULL DEFAULT '',
                    subject TEXT DEFAULT '',
                    scene TEXT DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    options TEXT NOT NULL DEFAULT '[]',
                    answer TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    strategy TEXT NOT NULL DEFAULT '',
                    confidence REAL,
                    risk INTEGER NOT NULL DEFAULT 0,
                    match_kind TEXT NOT NULL DEFAULT '',
                    elapsed_ms INTEGER NOT NULL DEFAULT 0,
                    outcome TEXT NOT NULL DEFAULT 'unknown',
                    correct_answer TEXT NOT NULL DEFAULT '',
                    outcome_source TEXT NOT NULL DEFAULT '',
                    outcome_at REAL NOT NULL DEFAULT 0,
                    repeat INTEGER NOT NULL DEFAULT 1,
                    created REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    meta TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_episodes_qkey ON episodes(qkey, id);
                CREATE INDEX IF NOT EXISTS idx_episodes_lookup
                    ON episodes(qtype, text_key, options_key);
                CREATE INDEX IF NOT EXISTS idx_episodes_outcome ON episodes(outcome, created);

                CREATE TABLE IF NOT EXISTS question_memory (
                    qkey TEXT PRIMARY KEY,
                    qtype TEXT NOT NULL,
                    text_key TEXT NOT NULL DEFAULT '',
                    options_key TEXT NOT NULL DEFAULT '',
                    img_key TEXT NOT NULL DEFAULT '',
                    subject TEXT DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    options TEXT NOT NULL DEFAULT '[]',
                    best_answer TEXT NOT NULL DEFAULT '',
                    best_source TEXT NOT NULL DEFAULT '',
                    best_provider TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    wrong_answers TEXT NOT NULL DEFAULT '{}',
                    correct_count INTEGER NOT NULL DEFAULT 0,
                    wrong_count INTEGER NOT NULL DEFAULT 0,
                    episode_count INTEGER NOT NULL DEFAULT 0,
                    last_outcome TEXT NOT NULL DEFAULT '',
                    last_seen REAL NOT NULL DEFAULT 0,
                    updated REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'observed'
                );
                CREATE INDEX IF NOT EXISTS idx_memory_lookup
                    ON question_memory(qtype, status, last_seen);
                """
            )
            self._conn.commit()

    def _invalidate(self) -> None:
        self._hint_cache.clear()

    # ------------------------------------------------------------------ 写入

    def record(
        self,
        *,
        qtype: Any,
        title: str,
        options: Any,
        answer: str,
        source: str = "ai",
        provider: str = "",
        strategy: str = "",
        subject: str = "",
        scene: str = "",
        image_hash: str = "",
        confidence: float | None = None,
        risk: bool = False,
        match: str = "",
        elapsed_ms: int = 0,
        meta: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> int:
        """记录一次作答。返回 episode id；空答案或非法题型返回 0。"""
        answer = str(answer or "").strip()
        if not answer:
            return 0
        fingerprint = question_fingerprint(qtype, title, options, image_hash)
        if not fingerprint["qtype"]:
            return 0
        moment = time.time() if now is None else float(now)
        option_list = parse_options(options)
        options_json = json.dumps(option_list, ensure_ascii=False)
        meta_json = json.dumps(meta or {}, ensure_ascii=False)[:4000]
        source = str(source or "ai")[:40]
        provider = str(provider or "")[:120]
        strategy = str(strategy or "")[:80]
        subject = str(subject or "")[:80]
        scene = str(scene or "")[:40]
        title_short = str(title or "")[:2000]

        with self._lock:
            # 同一道题短时间重复服务（缓存命中/并发单飞）只累加 repeat，
            # 避免把一次真实作答放大成几十条训练样本。
            row = self._conn.execute(
                "SELECT id, repeat FROM episodes "
                "WHERE qkey=? AND answer=? AND source=? AND provider=? AND strategy=? "
                "AND last_seen >= ? ORDER BY id DESC LIMIT 1",
                (fingerprint["qkey"], answer, source, provider, strategy,
                 moment - RECENT_DEDUPE_SECONDS),
            ).fetchone()
            if row is not None:
                self._conn.execute(
                    # meta 为空时保留旧值：重复服务不能把首次记录的检索来源抹掉。
                    "UPDATE episodes SET repeat=repeat+1, last_seen=?, "
                    "confidence=COALESCE(?, confidence), risk=?, "
                    "meta=CASE WHEN ? <> '{}' THEN ? ELSE meta END WHERE id=?",
                    (moment, confidence, 1 if risk else 0,
                     meta_json, meta_json, row["id"]),
                )
                self._touch_memory(fingerprint, subject, title_short, options_json, moment,
                                   increment=False)
                self._conn.commit()
                return int(row["id"])

            cursor = self._conn.execute(
                "INSERT INTO episodes (qkey, qtype, text_key, options_key, img_key, subject, scene, "
                "title, options, answer, source, provider, strategy, confidence, risk, match_kind, "
                "elapsed_ms, outcome, created, last_seen, meta) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', ?, ?, ?)",
                (fingerprint["qkey"], fingerprint["qtype"], fingerprint["text_key"],
                 fingerprint["options_key"], fingerprint["img_key"], subject, scene,
                 title_short, options_json, answer, source, provider, strategy,
                 confidence, 1 if risk else 0, str(match or "")[:80],
                 max(0, int(elapsed_ms or 0)), moment, moment, meta_json),
            )
            episode_id = int(cursor.lastrowid or 0)
            self._touch_memory(fingerprint, subject, title_short, options_json, moment)
            self._conn.commit()
            # 新建 episode 只会产生 observed 行，不改变提示内容；不在这里清缓存，
            # 否则一次答题风暴会把每次提示都打成全量重建。
            return episode_id

    def _touch_memory(
        self,
        fingerprint: dict[str, str],
        subject: str,
        title: str,
        options_json: str,
        moment: float,
        *,
        increment: bool = True,
    ) -> None:
        self._conn.execute(
            "INSERT INTO question_memory "
            "(qkey, qtype, text_key, options_key, img_key, subject, title, options, "
            "episode_count, last_seen, updated, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 'observed') "
            "ON CONFLICT(qkey) DO UPDATE SET "
            "episode_count=question_memory.episode_count+?, "
            "last_seen=excluded.last_seen, updated=excluded.updated, "
            "subject=CASE WHEN question_memory.subject='' THEN excluded.subject "
            "ELSE question_memory.subject END",
            (fingerprint["qkey"], fingerprint["qtype"], fingerprint["text_key"],
             fingerprint["options_key"], fingerprint["img_key"], subject, title,
             options_json, moment, moment, 1 if increment else 0),
        )

    def record_outcome_for_item(
        self,
        item: dict[str, Any] | None,
        official_answer: str,
        ok: bool,
        *,
        source: str = "feedback",
        scene: str = "",
        now: float | None = None,
    ) -> int:
        if not isinstance(item, dict):
            return 0
        return self.record_outcome(
            qtype=item.get("type"),
            title=str(item.get("title") or ""),
            options=item.get("options"),
            subject=str(item.get("subject") or ""),
            image_hash=str(item.get("image_hash") or ""),
            official_answer=official_answer,
            ok=ok,
            source=source,
            scene=scene,
            now=now,
        )

    def record_outcome(
        self,
        *,
        qtype: Any,
        title: str,
        options: Any,
        official_answer: str = "",
        ok: bool = False,
        subject: str = "",
        scene: str = "",
        image_hash: str = "",
        source: str = "feedback",
        now: float | None = None,
    ) -> int:
        """把一次「正确答案到手」写回历史 episode 与题目记忆。

        episode 的 outcome 不按调用方传入的 ``ok`` 一刀切，而是逐条把历史答案
        与官方答案比较；这样同一道题被不同模型答过多次时，正确率能记到各自的头上。
        """
        fingerprint = question_fingerprint(qtype, title, options, image_hash)
        if not fingerprint["qtype"]:
            return 0
        moment = time.time() if now is None else float(now)
        official = str(official_answer or "").strip()
        source = str(source or "feedback")[:40]

        with self._lock:
            # 回流只认图片身份完全一致的记录：题干/选项相同但图片不同的两道题，
            # 不能共用同一个官方答案（反馈页通常没有 base64，宁可少更新一条）。
            qkeys = self._matching_qkeys(fingerprint, strict_image=True)
            # 请求侧常带 base64（h:…），结果页只有图片 URL（u:…），严格图片身份
            # 永远对不上，历史 episode 就永远停在 unknown。用「记忆行题干/选项里
            # 的 URL 指纹」再匹配一次：它和结果页的 u: 身份是同一把钥匙，且
            # 只在 URL 完全一致时才合并，不会把另一张图扣到这道题头上。
            qkeys |= self._url_identity_qkeys(fingerprint)
            if not fingerprint["img_key"]:
                # 反馈页常常丢了 base64，只剩题干。只有这道题的全部记忆图片身份一致时，
                # 才把结果记到它们头上；存在多个图片变体就宁可不记，避免张冠李戴。
                qkeys |= self._unambiguous_qkeys(fingerprint)
            if not official and not qkeys:
                if not self._has_episodes(fingerprint["qkey"]):
                    return 0
            qkeys.add(fingerprint["qkey"])
            option_list = parse_options(options)
            updated = 0
            for qkey in qkeys:
                # 记忆行已有的选项顺序是答案字母的坐标系；新行才用本次反馈的顺序。
                target_options = self._memory_options(qkey)
                if target_options:
                    self._ensure_memory_row(fingerprint, subject, title, [], moment)
                else:
                    self._ensure_memory_row(fingerprint, subject, title, options, moment)
                    target_options = option_list
                official_for_memory = official
                if official and fingerprint["qtype"] in ("single", "multiple"):
                    # 官方答案可能是在另一套选项顺序里给的字母，先翻译到记忆行的坐标系。
                    # 选项图片 URL 只差 host/查询串时严格映射会失败，用宽松映射兜底。
                    mapped = translate_answer_loose(
                        official, option_list, target_options, fingerprint["qtype"]
                    )
                    if mapped is None:
                        # 无法映射就不写，避免把字母指向另一个选项。
                        continue
                    official_for_memory = mapped
                rows = self._conn.execute(
                    "SELECT id, answer, options FROM episodes "
                    "WHERE qkey=? AND answer != '' ORDER BY id DESC LIMIT 500",
                    (qkey,),
                ).fetchall()
                if not rows and not official:
                    continue
                for row in rows:
                    episode_answer = str(row["answer"] or "").strip()
                    if not episode_answer:
                        continue
                    episode_options = _json_load(row["options"], []) or []
                    if official:
                        official_for_episode = official
                        if fingerprint["qtype"] in ("single", "multiple"):
                            mapped = translate_answer_loose(
                                official, option_list, episode_options, fingerprint["qtype"]
                            )
                            if mapped is None:
                                continue
                            official_for_episode = mapped
                        try:
                            same = _answers_match(
                                fingerprint["qtype"], episode_answer, official_for_episode,
                                episode_options,
                            )
                        except Exception:  # noqa: BLE001 - 归一化失败退回精确比较
                            same = episode_answer == official_for_episode
                    else:
                        same = bool(ok)
                    outcome = "correct" if same else "wrong"
                    self._conn.execute(
                        "UPDATE episodes SET outcome=?, correct_answer=?, outcome_source=?, "
                        "outcome_at=? WHERE id=?",
                        (outcome, official_for_memory, source, moment, row["id"]),
                    )
                    updated += 1
                self._refresh_memory(
                    qkey, official_for_memory, ok, source, moment,
                    subject=subject, title=title, options=target_options,
                    qtype=fingerprint["qtype"],
                )
            self._conn.commit()
            self._invalidate()
            return updated

    def _matching_qkeys(
        self, fingerprint: dict[str, str], *, strict_image: bool
    ) -> set[str]:
        rows = self._conn.execute(
            "SELECT qkey, img_key FROM question_memory "
            "WHERE qtype=? AND text_key=? AND options_key=?",
            (fingerprint["qtype"], fingerprint["text_key"], fingerprint["options_key"]),
        ).fetchall()
        return {
            str(row["qkey"]) for row in rows
            if _img_compatible(str(row["img_key"] or ""), fingerprint["img_key"],
                               strict=strict_image)
        }

    def _url_identity_qkeys(self, fingerprint: dict[str, str]) -> set[str]:
        """结果页的 u: 身份 ↔ 记忆行的题干/选项 URL 身份。

        请求带 ``image_hash`` 时记忆行的 ``img_key`` 是 ``h:<sha256>``，与结果页
        从题干 URL 算出的 ``u:…`` 天然不相等；但记忆行仍保存着原始题干，重新
        从题干/选项算一次 URL 指纹就能对上。只认完全相等的 URL 指纹。
        """
        target = str(fingerprint.get("img_key") or "")
        if not target.startswith("u:"):
            return set()
        rows = self._conn.execute(
            "SELECT qkey, title, options FROM question_memory "
            "WHERE qtype=? AND text_key=? AND options_key=?",
            (fingerprint["qtype"], fingerprint["text_key"], fingerprint["options_key"]),
        ).fetchall()
        matched = set()
        for row in rows:
            stored_options = _json_load(row["options"], [])
            if _image_identity(str(row["title"] or ""), stored_options, "") == target:
                matched.add(str(row["qkey"]))
        return matched

    def _unambiguous_qkeys(self, fingerprint: dict[str, str]) -> set[str]:
        """图片身份缺失时，只在「同题干/选项只有一种图片身份」的情况下合并结果。"""
        rows = self._conn.execute(
            "SELECT qkey, img_key FROM question_memory "
            "WHERE qtype=? AND text_key=? AND options_key=?",
            (fingerprint["qtype"], fingerprint["text_key"],
             fingerprint["options_key"]),
        ).fetchall()
        if not rows:
            return set()
        if len({str(row["img_key"] or "") for row in rows}) != 1:
            return set()
        return {str(row["qkey"]) for row in rows}

    def _has_episodes(self, qkey: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM episodes WHERE qkey=? LIMIT 1", (qkey,)
        ).fetchone()
        return row is not None

    def _memory_options(self, qkey: str) -> list[str]:
        """记忆行当前保存的选项顺序；答案字母只有放在这个顺序里才有意义。"""
        row = self._conn.execute(
            "SELECT options FROM question_memory WHERE qkey=?", (qkey,)
        ).fetchone()
        if row is None:
            return []
        return _json_load(row["options"], [])

    def _ensure_memory_row(
        self,
        fingerprint: dict[str, str],
        subject: str,
        title: str,
        options: Any,
        moment: float,
    ) -> None:
        self._conn.execute(
            "INSERT INTO question_memory "
            "(qkey, qtype, text_key, options_key, img_key, subject, title, options, "
            "last_seen, updated, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'observed') "
            "ON CONFLICT(qkey) DO UPDATE SET "
            "subject=CASE WHEN question_memory.subject='' THEN excluded.subject "
            "ELSE question_memory.subject END, "
            "title=CASE WHEN excluded.title != '' THEN excluded.title "
            "ELSE question_memory.title END, "
            "options=CASE WHEN excluded.options != '[]' THEN excluded.options "
            "ELSE question_memory.options END",
            (fingerprint["qkey"], fingerprint["qtype"], fingerprint["text_key"],
             fingerprint["options_key"], fingerprint["img_key"], str(subject or "")[:80],
             str(title or "")[:2000],
             json.dumps(parse_options(options), ensure_ascii=False), moment, moment),
        )

    def _refresh_memory(
        self,
        qkey: str,
        official: str,
        ok: bool,
        source: str,
        moment: float,
        *,
        subject: str = "",
        title: str = "",
        options: Any = None,
        qtype: str = "",
    ) -> None:
        title_value = str(title or "")[:2000]
        target_options = parse_options(options) if options is not None else []
        options_json = (
            json.dumps(target_options, ensure_ascii=False)
            if options is not None else ""
        )
        counts = self._conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN outcome='correct' THEN 1 ELSE 0 END) AS ok, "
            "SUM(CASE WHEN outcome='wrong' THEN 1 ELSE 0 END) AS bad "
            "FROM episodes WHERE qkey=?",
            (qkey,),
        ).fetchone()
        episode_count = int((counts["total"] if counts else 0) or 0)
        correct = int((counts["ok"] if counts else 0) or 0)
        wrong = int((counts["bad"] if counts else 0) or 0)
        # 每道 episode 的选项顺序可能不同；错答要翻译到记忆行的坐标系再聚合，
        # 否则 hints() 会把「乙」当成「甲」提示给下一道题。
        wrong_rows = self._conn.execute(
            "SELECT answer, options, outcome_at FROM episodes "
            "WHERE qkey=? AND outcome='wrong' AND answer != '' "
            "ORDER BY outcome_at DESC, id DESC LIMIT 500",
            (qkey,),
        ).fetchall()
        wrong_map: dict[str, dict[str, Any]] = {}
        for row in wrong_rows:
            answer = str(row["answer"] or "").strip()
            if not answer:
                continue
            mapped = answer
            if target_options and qtype in ("single", "multiple"):
                mapped = translate_answer_loose(
                    answer, _json_load(row["options"], []) or [], target_options, qtype
                )
                if mapped is None:
                    continue
            entry = wrong_map.setdefault(mapped, {"count": 0, "last_at": 0.0})
            entry["count"] += 1
            entry["last_at"] = max(entry["last_at"], float(row["outcome_at"] or 0))
        wrong_map = dict(sorted(
            wrong_map.items(),
            key=lambda pair: (pair[1]["count"], pair[1]["last_at"]),
            reverse=True,
        )[:10])
        existing = self._conn.execute(
            "SELECT best_answer, best_source, best_provider, confidence FROM question_memory "
            "WHERE qkey=?",
            (qkey,),
        ).fetchone()
        best_answer = str((existing["best_answer"] if existing else "") or "")
        best_source = str((existing["best_source"] if existing else "") or "")
        best_provider = str((existing["best_provider"] if existing else "") or "")
        confidence = float((existing["confidence"] if existing else 0) or 0)
        if official:
            best_answer = official
            best_source = source
            confidence = 0.98 if source in ("official", "feedback", "paper", "page") else 0.9
        elif correct > 0:
            correct_rows = self._conn.execute(
                "SELECT answer, options FROM episodes WHERE qkey=? AND outcome='correct' "
                "AND answer != '' ORDER BY id DESC LIMIT 500",
                (qkey,),
            ).fetchall()
            counts_by_answer: dict[str, int] = {}
            for row in correct_rows:
                answer = str(row["answer"] or "").strip()
                if not answer:
                    continue
                mapped = answer
                if target_options and qtype in ("single", "multiple"):
                    mapped = translate_answer_loose(
                        answer, _json_load(row["options"], []) or [], target_options, qtype
                    )
                    if mapped is None:
                        continue
                counts_by_answer[mapped] = counts_by_answer.get(mapped, 0) + 1
            if counts_by_answer:
                best_answer = max(counts_by_answer, key=counts_by_answer.get)
                best_source = source
                confidence = max(confidence, 0.85)
        status = (
            "trusted" if best_answer and (bool(official) or correct > 0)
            else "disputed" if wrong > 0
            else "observed"
        )
        self._conn.execute(
            "UPDATE question_memory SET best_answer=?, best_source=?, best_provider=?, "
            "confidence=?, wrong_answers=?, correct_count=?, wrong_count=?, episode_count=?, "
            "subject=COALESCE(NULLIF(?, ''), subject), "
            "title=COALESCE(NULLIF(?, ''), title), "
            "options=COALESCE(NULLIF(?, ''), options), "
            "last_outcome=?, last_seen=?, updated=?, status=? WHERE qkey=?",
            (best_answer, best_source, best_provider, confidence,
             json.dumps(wrong_map, ensure_ascii=False), correct, wrong, episode_count,
             str(subject or "")[:80], title_value, options_json,
             "correct" if ok else "wrong", moment, moment, status, qkey),
        )

    # ------------------------------------------------------------------ 召回

    def _subject_ok(self, row: sqlite3.Row, subject: str) -> bool:
        best_source = str(row["best_source"] or "")
        if best_source in CROSS_SUBJECT_SOURCES:
            return True
        left = str(row["subject"] or "").strip()
        right = str(subject or "").strip()
        # 「默认」是脚本没带专业时的兜底标签，不是真实学科；不能因为它挡住同一道题。
        return (
            not left or not right or left == right
            or left == DEFAULT_SUBJECT or right == DEFAULT_SUBJECT
        )

    def recall(
        self,
        *,
        qtype: Any,
        title: str,
        options: Any,
        subject: str = "",
        image_hash: str = "",
        max_age_days: float = 365.0,
    ) -> dict[str, Any] | None:
        """题库/缓存未命中时，返回一条已被确认的答案；没有就返回 None。"""
        fingerprint = question_fingerprint(qtype, title, options, image_hash)
        if not fingerprint["qtype"]:
            return None
        option_list = parse_options(options)
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM question_memory WHERE qkey=?", (fingerprint["qkey"],)
            ).fetchone()
            match_kind = "exact"
            if row is None:
                rows = self._conn.execute(
                    "SELECT * FROM question_memory WHERE qtype=? AND text_key=? "
                    "AND options_key=? ORDER BY last_seen DESC LIMIT 20",
                    (fingerprint["qtype"], fingerprint["text_key"],
                     fingerprint["options_key"]),
                ).fetchall()
                row = next(
                    (candidate for candidate in rows
                     if _img_compatible(str(candidate["img_key"] or ""),
                                        fingerprint["img_key"], strict=True)
                     and self._subject_ok(candidate, subject)),
                    None,
                )
                match_kind = "similar"
            if row is None:
                return None
            if str(row["status"] or "") != "trusted":
                return None
            if not self._subject_ok(row, subject):
                return None
            best = str(row["best_answer"] or "").strip()
            if not best:
                return None
            best_source = str(row["best_source"] or "")
            age = _age_days(now, float(row["last_seen"] or 0))
            if age > max_age_days and best_source not in CROSS_SUBJECT_SOURCES:
                return None
            confidence = float(row["confidence"] or 0)
            if best_source not in CROSS_SUBJECT_SOURCES:
                confidence *= math.exp(-age / 180.0)
            if confidence < 0.5:
                return None
            stored_options = _json_load(row["options"], [])
            translated = translate_answer(best, stored_options, option_list,
                                          fingerprint["qtype"])
            if translated is None:
                return None
            return {
                "answer": translated,
                "qkey": str(row["qkey"]),
                "match": f"memory-{match_kind}",
                "status": "trusted",
                "confidence": round(confidence, 4),
                "correct_count": int(row["correct_count"] or 0),
                "wrong_count": int(row["wrong_count"] or 0),
                "outcome_source": best_source,
                "subject": str(row["subject"] or ""),
                "last_seen": float(row["last_seen"] or 0),
                "age_days": round(age, 1),
            }

    def hints(
        self,
        *,
        qtype: Any,
        title: str,
        options: Any,
        subject: str = "",
        image_hash: str = "",
        limit: int = 2,
    ) -> str:
        """给模型的一段短提示：相似题历史上哪些答案被判错、哪条已确认。"""
        fingerprint = question_fingerprint(qtype, title, options, image_hash)
        qtype_norm = fingerprint["qtype"]
        # 填空题没有选项锚定，模糊匹配会把另一道题的错误答案扣过来。
        if qtype_norm not in ("single", "multiple", "judge"):
            return ""
        if not fingerprint["options_key"] or not fingerprint["text_key"]:
            return ""
        option_list = parse_options(options)
        target_negation = _negation_signature(title)
        target_bigrams = _bigrams(fingerprint["text_key"])
        now = time.time()
        scored: list[tuple[float, dict[str, Any]]] = []
        for candidate in self._hint_candidates(qtype_norm):
            if candidate["options_key"] != fingerprint["options_key"]:
                continue
            if not _img_compatible(candidate["img_key"], fingerprint["img_key"],
                                   strict=True):
                continue
            if not self._subject_ok(candidate["row"], subject):
                continue
            if candidate["negation"] != target_negation:
                continue
            if candidate["qkey"] == fingerprint["qkey"] and candidate["status"] == "trusted" \
                    and candidate["best_answer"]:
                # 完全同一道题且已有确认答案：recall 会直接返回，不需要提示。
                continue
            score = _dice_sets(target_bigrams, candidate["bigrams"])
            if score < FUZZY_THRESHOLD:
                continue
            scored.append((score, candidate))
        if not scored:
            return ""
        scored.sort(key=lambda pair: (pair[0], pair[1]["last_seen"]), reverse=True)
        lines: list[str] = []
        for score, candidate in scored[: max(1, min(int(limit or 2), 3))]:
            age = _age_text(_age_days(now, candidate["last_seen"]))
            line = f"- 相似题（相似度 {score:.2f}，{age}）"
            stored_options = _json_load(candidate["row"]["options"], [])
            wrong_answers = []
            wrong_map = candidate["wrong_answers"]
            for answer in sorted(wrong_map, key=lambda key: wrong_map[key].get("count", 0),
                                 reverse=True)[:3]:
                translated = translate_answer(answer, stored_options, option_list,
                                              qtype_norm)
                if translated:
                    wrong_answers.append(translated)
            if wrong_answers:
                line += "；曾判错：" + "、".join(wrong_answers)
            best = str(candidate["best_answer"] or "").strip()
            if best:
                translated = translate_answer(best, stored_options, option_list, qtype_norm)
                if translated:
                    line += "；已确认：" + translated
            lines.append(line)
        if not lines:
            return ""
        return (
            "[记忆提示] 历史上有相似题，以下经验仅供参考，不是当前题的标准答案：\n"
            + "\n".join(lines)
            + "\n请结合当前题干独立作答，不要重复上述已知错误。"
        )[:1000]

    def _hint_candidates(self, qtype: str) -> list[dict[str, Any]]:
        cached = self._hint_cache.get(qtype)
        if cached is not None:
            return cached
        rows = self._conn.execute(
            "SELECT * FROM question_memory WHERE qtype=? AND status IN ('trusted','disputed') "
            "ORDER BY last_seen DESC LIMIT ?",
            (qtype, MAX_HINT_CANDIDATES),
        ).fetchall()
        candidates: list[dict[str, Any]] = []
        for row in rows:
            wrong = _json_load(row["wrong_answers"], {})
            best = str(row["best_answer"] or "").strip()
            if not wrong and not best:
                continue
            title = str(row["title"] or "")
            candidates.append({
                "row": row,
                "qkey": str(row["qkey"]),
                "options_key": str(row["options_key"] or ""),
                "img_key": str(row["img_key"] or ""),
                "title": title,
                "text_key": str(row["text_key"] or ""),
                "bigrams": _bigrams(str(row["text_key"] or "")),
                "negation": _negation_signature(title),
                "wrong_answers": wrong,
                "best_answer": best,
                "status": str(row["status"] or ""),
                "last_seen": float(row["last_seen"] or 0),
            })
        self._hint_cache[qtype] = candidates
        return candidates

    # ------------------------------------------------------------------ 查询/维护

    def truth_state(
        self,
        *,
        qtype: Any,
        title: str,
        options: Any,
        answer: str = "",
    ) -> dict[str, Any]:
        """这道题的记忆里有没有吸收页面官方答案（管理台用，不参与召回）。

        只按「题型 + 文字骨架 + 选项集合」找同题记忆行，再把每行的
        ``best_answer`` 翻译到页面这次的选项顺序后比较。图片身份不参与
        过滤：结果页只有 URL，请求侧可能是 base64，拿它过滤只会让
        「明明已经同步」显示成未同步。
        """
        fingerprint = question_fingerprint(qtype, title, options, "")
        target_options = parse_options(options)
        official = str(answer or "").strip()
        state = {
            "rows": 0, "trusted": 0, "synced": None,
            "best": "", "source": "", "qkey": "",
        }
        if not fingerprint["qtype"]:
            return state
        with self._lock:
            rows = self._conn.execute(
                "SELECT qkey, options, best_answer, best_source, confidence, status "
                "FROM question_memory WHERE qtype=? AND text_key=? AND options_key=? "
                "ORDER BY last_seen DESC LIMIT 50",
                (fingerprint["qtype"], fingerprint["text_key"],
                 fingerprint["options_key"]),
            ).fetchall()
        # 精确 qkey 的行优先，其它图片变体排后面，只用来判断「同题是否已吸收」。
        ordered = sorted(
            rows, key=lambda row: 0 if str(row["qkey"]) == fingerprint["qkey"] else 1)
        state["rows"] = len(ordered)
        synced: tuple[str, sqlite3.Row] | None = None
        first: tuple[str, sqlite3.Row] | None = None
        for row in ordered:
            if str(row["status"] or "") == "trusted":
                state["trusted"] += 1
            stored = str(row["best_answer"] or "").strip()
            if not stored:
                continue
            stored_options = _json_load(row["options"], [])
            translated = stored
            if fingerprint["qtype"] in ("single", "multiple"):
                translated = translate_answer_loose(
                    stored, stored_options, target_options, fingerprint["qtype"])
                if translated is None:
                    continue
            if first is None:
                first = (translated, row)
            if official and synced is None:
                try:
                    same = _answers_match(
                        fingerprint["qtype"], translated, official, target_options)
                except Exception:  # noqa: BLE001 - 归一化失败退回精确比较
                    same = translated == official
                if same:
                    synced = (translated, row)
        if official:
            state["synced"] = synced is not None
        chosen = synced or first
        if chosen is not None:
            state["best"] = chosen[0]
            state["source"] = str(chosen[1]["best_source"] or "")
            state["qkey"] = str(chosen[1]["qkey"])
        return state

    def stats(self) -> dict[str, Any]:
        with self._lock:
            episode_row = self._conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN outcome='correct' THEN 1 ELSE 0 END) AS correct, "
                "SUM(CASE WHEN outcome='wrong' THEN 1 ELSE 0 END) AS wrong, "
                "SUM(CASE WHEN outcome='unknown' THEN 1 ELSE 0 END) AS unknown "
                "FROM episodes"
            ).fetchone()
            memory_row = self._conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN status='trusted' THEN 1 ELSE 0 END) AS trusted, "
                "SUM(CASE WHEN status='disputed' THEN 1 ELSE 0 END) AS disputed "
                "FROM question_memory"
            ).fetchone()
            provider_rows = self._conn.execute(
                "SELECT provider, strategy, COUNT(*) AS total, "
                "SUM(CASE WHEN outcome='correct' THEN 1 ELSE 0 END) AS ok, "
                "SUM(CASE WHEN outcome='wrong' THEN 1 ELSE 0 END) AS bad "
                "FROM episodes WHERE outcome IN ('correct','wrong') "
                "GROUP BY provider, strategy ORDER BY total DESC LIMIT 50"
            ).fetchall()
            wrong_rows = self._conn.execute(
                "SELECT qtype, title, wrong_answers, wrong_count, last_seen "
                "FROM question_memory WHERE wrong_count > 0 "
                "ORDER BY wrong_count DESC, last_seen DESC LIMIT 10"
            ).fetchall()
            recent_rows = self._conn.execute(
                "SELECT qtype, title, answer, source, provider, strategy, outcome, created "
                "FROM episodes ORDER BY id DESC LIMIT 10"
            ).fetchall()
        episodes = {
            "total": int((episode_row["total"] if episode_row else 0) or 0),
            "correct": int((episode_row["correct"] if episode_row else 0) or 0),
            "wrong": int((episode_row["wrong"] if episode_row else 0) or 0),
            "unknown": int((episode_row["unknown"] if episode_row else 0) or 0),
        }
        episodes["accuracy"] = (
            round(episodes["correct"] / (episodes["correct"] + episodes["wrong"]), 4)
            if episodes["correct"] + episodes["wrong"] else None
        )
        memories = {
            "total": int((memory_row["total"] if memory_row else 0) or 0),
            "trusted": int((memory_row["trusted"] if memory_row else 0) or 0),
            "disputed": int((memory_row["disputed"] if memory_row else 0) or 0),
        }
        providers = []
        for row in provider_rows:
            ok = int(row["ok"] or 0)
            bad = int(row["bad"] or 0)
            providers.append({
                "provider": str(row["provider"] or ""),
                "strategy": str(row["strategy"] or ""),
                "total": int(row["total"] or 0),
                "ok": ok,
                "bad": bad,
                "rate": round(ok / (ok + bad), 4) if ok + bad else None,
            })
        wrong_questions = [
            {
                "qtype": str(row["qtype"] or ""),
                "title": str(row["title"] or "")[:120],
                "wrong_answers": _json_load(row["wrong_answers"], {}),
                "wrong_count": int(row["wrong_count"] or 0),
                "last_seen": float(row["last_seen"] or 0),
            }
            for row in wrong_rows
        ]
        recent = [
            {
                "qtype": str(row["qtype"] or ""),
                "title": str(row["title"] or "")[:120],
                "answer": str(row["answer"] or ""),
                "source": str(row["source"] or ""),
                "provider": str(row["provider"] or ""),
                "strategy": str(row["strategy"] or ""),
                "outcome": str(row["outcome"] or ""),
                "created": float(row["created"] or 0),
            }
            for row in recent_rows
        ]
        return {
            "episodes": episodes,
            "memories": memories,
            "providers": providers,
            "wrong_questions": wrong_questions,
            "recent": recent,
        }

    def search(self, text: str, limit: int = 20) -> list[dict[str, Any]]:
        target = text_key(text)
        if not target:
            return []
        target_bigrams = _bigrams(target)
        with self._lock:
            rows = self._conn.execute(
                "SELECT qkey, qtype, subject, title, text_key, best_answer, best_source, "
                "confidence, wrong_answers, correct_count, wrong_count, status, last_seen "
                "FROM question_memory ORDER BY last_seen DESC LIMIT 5000"
            ).fetchall()
        scored = []
        for row in rows:
            score = _dice_sets(target_bigrams, _bigrams(str(row["text_key"] or "")))
            if score <= 0:
                continue
            scored.append((score, row))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        results = []
        for score, row in scored[: max(1, min(int(limit or 20), 50))]:
            results.append({
                "qkey": str(row["qkey"]),
                "qtype": str(row["qtype"] or ""),
                "subject": str(row["subject"] or ""),
                "title": str(row["title"] or "")[:160],
                "best_answer": str(row["best_answer"] or ""),
                "best_source": str(row["best_source"] or ""),
                "confidence": float(row["confidence"] or 0),
                "wrong_answers": _json_load(row["wrong_answers"], {}),
                "correct_count": int(row["correct_count"] or 0),
                "wrong_count": int(row["wrong_count"] or 0),
                "status": str(row["status"] or ""),
                "last_seen": float(row["last_seen"] or 0),
                "score": round(score, 3),
            })
        return results

    def timeline(self, qkey: str = "", limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            if qkey:
                rows = self._conn.execute(
                    "SELECT * FROM episodes WHERE qkey=? ORDER BY id DESC LIMIT ?",
                    (qkey, max(1, min(int(limit or 50), 200))),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM episodes ORDER BY id DESC LIMIT ?",
                    (max(1, min(int(limit or 50), 200)),),
                ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "qkey": str(row["qkey"]),
                "qtype": str(row["qtype"]),
                "title": str(row["title"])[:160],
                "answer": str(row["answer"]),
                "source": str(row["source"]),
                "provider": str(row["provider"]),
                "strategy": str(row["strategy"]),
                "confidence": row["confidence"],
                "risk": bool(row["risk"]),
                "outcome": str(row["outcome"]),
                "correct_answer": str(row["correct_answer"]),
                "outcome_source": str(row["outcome_source"]),
                "repeat": int(row["repeat"] or 1),
                "created": float(row["created"] or 0),
            }
            for row in rows
        ]

    def forget(self, qkey: str = "") -> int:
        with self._lock:
            if qkey:
                removed = self._conn.execute(
                    "DELETE FROM episodes WHERE qkey=?", (qkey,)
                ).rowcount
                self._conn.execute("DELETE FROM question_memory WHERE qkey=?", (qkey,))
            else:
                removed = self._conn.execute("DELETE FROM episodes").rowcount
                self._conn.execute("DELETE FROM question_memory")
            self._conn.commit()
            self._invalidate()
            return int(removed or 0)

    def prune(
        self,
        *,
        now: float | None = None,
        episode_days: float = 180.0,
        unknown_days: float = 30.0,
        max_episodes: int = 50000,
    ) -> dict[str, int]:
        """滚动清理：未确认的短期记忆先忘，全部 episode 有硬上限。"""
        moment = time.time() if now is None else float(now)
        with self._lock:
            removed_unknown = self._conn.execute(
                "DELETE FROM episodes WHERE outcome='unknown' AND created < ?",
                (moment - max(0.0, float(unknown_days)) * 86400,),
            ).rowcount
            removed_old = self._conn.execute(
                "DELETE FROM episodes WHERE created < ?",
                (moment - max(0.0, float(episode_days)) * 86400,),
            ).rowcount
            total = int(self._conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])
            removed_overflow = 0
            if total > max(1, int(max_episodes)):
                overflow = total - max(1, int(max_episodes))
                removed_overflow = self._conn.execute(
                    "DELETE FROM episodes WHERE id IN "
                    "(SELECT id FROM episodes ORDER BY id ASC LIMIT ?)", (overflow,)
                ).rowcount
            # episode 删掉后把聚合计数拉回真实值；题目记忆本身（best_answer）保留。
            self._conn.execute(
                "UPDATE question_memory SET "
                "episode_count=(SELECT COUNT(*) FROM episodes e "
                "WHERE e.qkey=question_memory.qkey), "
                "correct_count=(SELECT COUNT(*) FROM episodes e "
                "WHERE e.qkey=question_memory.qkey AND e.outcome='correct'), "
                "wrong_count=(SELECT COUNT(*) FROM episodes e "
                "WHERE e.qkey=question_memory.qkey AND e.outcome='wrong')"
            )
            self._conn.commit()
            self._invalidate()
        return {
            "unknown": int(removed_unknown or 0),
            "old": int(removed_old or 0),
            "overflow": int(removed_overflow or 0),
        }

    def checkpoint(self) -> None:
        try:
            with self._lock:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _dice_sets(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return 2.0 * len(left & right) / (len(left) + len(right))

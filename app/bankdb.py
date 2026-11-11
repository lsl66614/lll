"""题库 SQLite 持久层：增量写入，替代大题库时的全量 JSON 重写。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any, Iterable

JSON_COLUMNS = ("options", "options_norm", "history", "wrong_answers")
COLUMNS = (
    "id", "type", "title", "options", "answer", "subject", "image_hash", "sh", "img_key",
    "options_norm", "answer_norm",
    "source", "level", "via", "official_via", "status", "history", "wrong_answers", "agree", "conflict",
    "created", "updated", "origin", "canonical_id",
)


def _row(item: dict[str, Any]) -> tuple:
    values = []
    for column in COLUMNS:
        value = item.get(column)
        if column in JSON_COLUMNS:
            value = json.dumps(value if value is not None else ({} if column == "options" else []), ensure_ascii=False)
        elif column == "conflict":
            value = 1 if value else 0
        elif column in ("level", "agree"):
            value = int(value or 0)
        elif column == "updated":
            # 迁移脚本可能直接塞未归一化的原始行：缺 updated 时用 created 兜底，
            # 不要写 NULL —— 排序/增量同步都默认它是数字。
            value = float(value or item.get("created") or 0)
        elif column == "origin":
            value = str(value or item.get("via") or "")
        values.append(value)
    return tuple(values)


class BankDB:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._fts = False
        self.broken = False
        try:
            self._init_schema()
        except sqlite3.DatabaseError:
            # 库文件本身坏了（不是某张表坏）：``PRAGMA journal_mode=WAL`` 就会
            # 抛 "file is not a database"。这时不能让整个网关起不来 ——
            # 标记 broken，交由调用方决定是恢复还是重建。
            self.broken = True
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = sqlite3.connect(path, check_same_thread=False)

    def _init_schema(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS questions ("
            "id TEXT PRIMARY KEY, type TEXT, title TEXT, options TEXT, answer TEXT, subject TEXT,"
            "image_hash TEXT, sh TEXT, img_key TEXT DEFAULT '', options_norm TEXT DEFAULT '[]',"
            "answer_norm TEXT DEFAULT '', source TEXT, level INTEGER, via TEXT, status TEXT,"
            "history TEXT, wrong_answers TEXT, agree INTEGER, conflict INTEGER, created REAL, official_via TEXT DEFAULT '',"
            "updated REAL DEFAULT 0, origin TEXT DEFAULT '', canonical_id TEXT DEFAULT '')"
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_q_type_subject ON questions(type, subject)")
        try:
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(questions)")}
            if "official_via" not in columns:
                self._conn.execute("ALTER TABLE questions ADD COLUMN official_via TEXT DEFAULT ''")
            if "img_key" not in columns:
                # 2026-11-10 新增：题干图片指纹（按序 URL 指纹，'|' 连接）。
                # 派生列，只写不读，先把数据攒起来，匹配逻辑以后再接入。
                self._conn.execute("ALTER TABLE questions ADD COLUMN img_key TEXT DEFAULT ''")
            if "updated" not in columns:
                # 2026-11-10 P1：最后修改时间。老记录无从考证，用 created 当下界。
                self._conn.execute("ALTER TABLE questions ADD COLUMN updated REAL DEFAULT 0")
                self._conn.execute(
                    "UPDATE questions SET updated = created WHERE updated IS NULL OR updated = 0")
            if "origin" not in columns:
                # 2026-11-10 P1：原始出处。与 via 的区别是**只在入库时写一次**，
                # 后续 record_attempt 改 via 不会改它。老记录用当前 via 兜底。
                self._conn.execute("ALTER TABLE questions ADD COLUMN origin TEXT DEFAULT ''")
                self._conn.execute(
                    "UPDATE questions SET origin = COALESCE(via, '') WHERE origin IS NULL OR origin = ''")
            if "canonical_id" not in columns:
                # 2026-11-10 P2：规范记录 id。派生列，由 Bank.recompute_canonicals()
                # 在加载/落盘时计算；这里只加列，回填交给 scripts/backfill_canonical_id.py。
                self._conn.execute("ALTER TABLE questions ADD COLUMN canonical_id TEXT DEFAULT ''")
            if "options_norm" not in columns:
                # 2026-11-10 P3：选项顺序归一化（与 options 同序的规范化内容，JSON 数组）。
                # 派生列，由 Bank._normalize() 计算；回填交给 scripts/backfill_options_norm.py。
                self._conn.execute("ALTER TABLE questions ADD COLUMN options_norm TEXT DEFAULT '[]'")
            if "answer_norm" not in columns:
                # 2026-11-10 P3：答案语义（与选项顺序无关）。派生列，同上下。
                self._conn.execute("ALTER TABLE questions ADD COLUMN answer_norm TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS questions_fts USING fts5("
                "id UNINDEXED, title, options, tokenize='trigram')"
            )
            self._fts = True
        except sqlite3.DatabaseError:
            # 索引损坏时不算致命：主表还在，搜索可以退化成非 FTS 模式，
            # 或调 rebuild_fts() 重建（虚表是派生数据）。
            self._fts = False
        if self._fts:
            try:
                fts_count = self._conn.execute("SELECT COUNT(*) FROM questions_fts").fetchone()[0]
                if fts_count == 0:
                    self._conn.execute(
                        "INSERT INTO questions_fts (id, title, options) "
                        "SELECT id, title, COALESCE(options, '') FROM questions"
                    )
            except sqlite3.DatabaseError:
                self._fts = False
        self._conn.commit()

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0])

    def is_readable(self) -> bool:
        """主表能否正常读取（区分「库是空的」与「库坏了」）。

        库损坏时 ``COUNT(*)`` 可能仍然返回一个数，但读列会抛
        ``database disk image is malformed``；只有连 ``id`` 列都读得出来，
        才算主表完好。
        """
        if self.broken:
            return False
        with self._lock:
            try:
                self._conn.execute("SELECT id FROM questions LIMIT 1").fetchone()
                self._conn.execute("SELECT COUNT(*) FROM questions").fetchone()
            except sqlite3.DatabaseError:
                return False
            return True

    def load_all(self) -> list[dict[str, Any]]:
        if self.broken:
            return []
        with self._lock:
            try:
                rows = self._conn.execute(f"SELECT {', '.join(COLUMNS)} FROM questions").fetchall()
            except sqlite3.DatabaseError:
                return []
        items = []
        for row in rows:
            item = dict(zip(COLUMNS, row))
            for column in JSON_COLUMNS:
                try:
                    item[column] = json.loads(item[column] or ("{}" if column == "options" else "[]"))
                except (TypeError, json.JSONDecodeError):
                    item[column] = {} if column == "options" else []
            item["conflict"] = bool(item.get("conflict"))
            items.append(item)
        return items

    def upsert_many(self, items: Iterable[dict[str, Any]]) -> None:
        items = list(items)
        rows = [_row(item) for item in items]
        if not rows:
            return
        placeholders = ", ".join("?" for _ in COLUMNS)
        updates = ", ".join(f"{column}=excluded.{column}" for column in COLUMNS if column != "id")
        sql = f"INSERT INTO questions ({', '.join(COLUMNS)}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {updates}"
        with self._lock:
            with self._conn:
                self._conn.executemany(sql, rows)
                if self._fts:
                    ids = [item["id"] for item in items]
                    self._conn.executemany("DELETE FROM questions_fts WHERE id = ?", [(i,) for i in ids])
                    self._conn.executemany(
                        "INSERT INTO questions_fts (id, title, options) VALUES (?, ?, ?)",
                        [(item["id"], item.get("title") or "",
                          " ".join(str(o) for o in (item.get("options") or []))) for item in items],
                    )

    def delete_many(self, ids: Iterable[str]) -> None:
        ids = list(ids)
        if not ids:
            return
        with self._lock:
            with self._conn:
                self._conn.executemany("DELETE FROM questions WHERE id = ?", [(item_id,) for item_id in ids])
                if self._fts:
                    self._conn.executemany("DELETE FROM questions_fts WHERE id = ?", [(item_id,) for item_id in ids])

    def checkpoint(self) -> bool:
        """把 WAL 合并进主库并清空。

        WAL 长期不 checkpoint 会涨到几 MB；更麻烦的是**主库文件被整体替换后**
        旧 WAL 还在，它的 salt/页号跟新主库对不上，读主库会报
        ``database disk image is malformed``（2026-11-10 实测）。
        所以批量写入或替换主库后一定要调这个。
        """
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                return True
            except sqlite3.DatabaseError:
                return False

    def replace_all(self, items: list[dict[str, Any]]) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM questions")
                if self._fts:
                    self._conn.execute("DELETE FROM questions_fts")
                rows = [_row(item) for item in items]
                if self._fts:
                    self._conn.executemany(
                        "INSERT INTO questions_fts (id, title, options) VALUES (?, ?, ?)",
                        [(item["id"], item.get("title") or "",
                          " ".join(str(o) for o in (item.get("options") or []))) for item in items],
                    )
                if rows:
                    placeholders = ", ".join("?" for _ in COLUMNS)
                    self._conn.executemany(
                        f"INSERT INTO questions ({', '.join(COLUMNS)}) VALUES ({placeholders})", rows
                    )

    def backup(self, dest: str) -> None:
        """用 sqlite 在线备份 API 导出一致快照（WAL 下直接拷文件会丢最近写入）。"""
        with self._lock:
            target = sqlite3.connect(dest)
            try:
                self._conn.backup(target)
                target.commit()
            finally:
                target.close()

    def search_ids(self, keyword: str) -> list[str]:
        """FTS5 trigram 子串检索（中文 ≥3 字最可靠）；不可用时返回空。"""
        if not self._fts or not keyword:
            return []
        phrase = '"' + keyword.replace('"', '""') + '"'
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id FROM questions_fts WHERE questions_fts MATCH ?", (phrase,)
                ).fetchall()
            return [row[0] for row in rows]
        except sqlite3.OperationalError:
            return []

    def rebuild_fts(self) -> int:
        """重建 FTS5 虚表并从主表全量回灌。

        虚表是派生数据，主表才是源——索引损坏（database disk image is malformed）
        时可以随时重建。返回回灌的条数。
        """
        with self._lock:
            self._conn.execute("DROP TABLE IF EXISTS questions_fts")
            self._conn.execute(
                "CREATE VIRTUAL TABLE questions_fts USING fts5("
                "id UNINDEXED, title, options, tokenize='trigram')"
            )
            self._conn.execute(
                "INSERT INTO questions_fts (id, title, options) "
                "SELECT id, title, COALESCE(options, '') FROM questions"
            )
            self._conn.commit()
            self._fts = True
            return int(self._conn.execute("SELECT COUNT(*) FROM questions_fts").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()

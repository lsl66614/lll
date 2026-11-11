"""只做一件事：把完好备份 + 当前库独有记录，写成一个新库文件。

不碰 data/bank.db，只产出 data/_rebuilt_bank.db。
用法：
    .venv\\Scripts\\python.exe scripts/build_rebuilt_bank.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config  # noqa: E402
from app.bankdb import BankDB  # noqa: E402

GOOD_BACKUP = os.path.join(ROOT, "data", "bank.db.bak-recover-20261110-011711")
NEW_DB = os.path.join(ROOT, "data", "_rebuilt_bank.db")
COLUMNS = [
    "id", "type", "title", "options", "answer", "subject", "image_hash", "sh",
    "source", "level", "via", "status", "history", "wrong_answers", "agree",
    "conflict", "created", "official_via",
]
JSON_COLUMNS = ("options", "history", "wrong_answers")


def read_rows(path):
    rows = []
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    except sqlite3.Error:
        return rows
    try:
        cur = conn.execute("SELECT %s FROM questions" % ",".join(COLUMNS))
        while True:
            try:
                row = cur.fetchone()
            except sqlite3.DatabaseError:
                break
            if row is None:
                break
            rows.append(row)
    except sqlite3.DatabaseError:
        pass
    finally:
        conn.close()
    return rows


def to_items(rows):
    items = []
    for row in rows:
        item = dict(zip(COLUMNS, row))
        for column in JSON_COLUMNS:
            raw = item.get(column)
            if isinstance(raw, str):
                try:
                    item[column] = json.loads(raw)
                except Exception:  # noqa: BLE001
                    item[column] = []
        items.append(item)
    return items


def main():
    good = read_rows(GOOD_BACKUP)
    good_ids = {r[0] for r in good}
    extra = [r for r in read_rows(config.BANK_DB_PATH) if r[0] not in good_ids]
    print("完好备份 %d 条，当前库独有 %d 条" % (len(good), len(extra)))

    items = to_items(good + extra)
    db = BankDB(NEW_DB)
    db.replace_all(items)

    conn = sqlite3.connect("file:%s?mode=ro" % NEW_DB.replace("\\", "/"), uri=True)
    print("新库：题数 %d，FTS %d，integrity=%s" % (
        conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM questions_fts").fetchone()[0],
        conn.execute("PRAGMA integrity_check").fetchone()[0][:40]))
    conn.close()
    print("已生成", os.path.basename(NEW_DB))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

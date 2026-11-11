"""把两个题库合并成一份。

背景（2026-11-10）：
- ``data/bank.cleaned.db``（另一会话产出，1219 条）：条数更多，但缺了
  「从快照恢复的官方答案」（官方待确认还剩 52 条）。
- ``data/_rebuilt_bank.db``（本会话产出，925 条）：含 90 条恢复出来的
  官方答案（官方待确认只剩 13 条），但条数少。

策略：以 cleaned 为主库，逐条把 rebuilt 里「有官方答案而 cleaned 没有」的
补进去；官方答案冲突时以 rebuilt 为准（它是按快照真值核对过的）。
"""
from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.bankdb import BankDB  # noqa: E402

CLEANED = os.path.join(ROOT, "data", "bank.cleaned.db")
REBUILT = os.path.join(ROOT, "data", "_rebuilt_bank.db")
MERGED = os.path.join(ROOT, "data", "_merged_bank.db")
COLUMNS = [
    "id", "type", "title", "options", "answer", "subject", "image_hash", "sh",
    "source", "level", "via", "status", "history", "wrong_answers", "agree",
    "conflict", "created", "official_via",
]


def read(path):
    conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    rows = conn.execute("SELECT %s FROM questions" % ",".join(COLUMNS)).fetchall()
    conn.close()
    return rows


def as_dict(row):
    item = dict(zip(COLUMNS, row))
    for column in ("options", "history", "wrong_answers"):
        raw = item.get(column)
        if isinstance(raw, str):
            try:
                import json as _json
                item[column] = _json.loads(raw)
            except Exception:  # noqa: BLE001
                item[column] = []
    return item


def main():
    cleaned = {r[0]: as_dict(r) for r in read(CLEANED)}
    rebuilt = {r[0]: as_dict(r) for r in read(REBUILT)}
    print("cleaned %d 条，rebuilt %d 条" % (len(cleaned), len(rebuilt)))

    # rebuilt 独有的：直接并入
    added = 0
    for iid, item in rebuilt.items():
        if iid not in cleaned:
            cleaned[iid] = item
            added += 1
    print("并入 rebuilt 独有记录 %d 条" % added)

    # 交集：官方答案以 rebuilt 为准（它按快照真值核对过）
    #
    # ⚠️ 答案字母必须和它依据的**选项顺序**一起搬 —— 只搬 answer 会造出
    # 「悬空答案」：字母指向的选项在新记录里是空的，答案看着是官方的，
    # 实际什么也没答对（2026-11-10 实测 merged 库凭空多出 8 条）。
    fixed = 0
    dangling = 0
    for iid, item in rebuilt.items():
        old = cleaned.get(iid)
        if not old:
            continue
        if item.get("source") != "official":
            continue
        if not str(item.get("answer") or "").strip():
            continue
        if old.get("answer") == item["answer"] and old.get("status") == item.get("status"):
            continue
        # 选项也一起搬，保证字母与顺序自洽
        if item.get("options") not in (None, [], {}):
            old["options"] = item["options"]
        old["answer"] = item["answer"]
        old["status"] = item.get("status") or old.get("status")
        old["conflict"] = bool(item.get("conflict"))
        old["agree"] = int(item.get("agree") or 0)
        fixed += 1
    print("修正官方答案 %d 条" % fixed)

    # 自检：官方答案不能指向空选项
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    for item in cleaned.values():
        if item.get("source") != "official" or item.get("status") != "adopted":
            continue
        answer = str(item.get("answer") or "").strip()
        options = item.get("options") or []
        if not answer or answer not in letters or not options:
            continue
        index = letters.index(answer)
        if index >= len(options) or not str(options[index] or "").strip():
            dangling += 1
            if dangling <= 8:
                print("  ⚠ 悬空答案 %s ans=%r | %s"
                      % (item.get("id"), answer, (item.get("title") or "")[:50]))
    print("悬空官方答案 %d 条" % dangling)
    if dangling:
        print("（悬空答案看着是官方的、实际答不对，落库前应先修掉）")

    items = list(cleaned.values())
    for suffix in ("", "-wal", "-shm"):
        path = MERGED + suffix
        if os.path.exists(path):
            os.remove(path)
    db = BankDB(MERGED)
    db.replace_all(items)

    conn = sqlite3.connect("file:%s?mode=ro" % MERGED.replace("\\", "/"), uri=True)
    total = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    fts = conn.execute("SELECT COUNT(*) FROM questions_fts").fetchone()[0]
    ad = conn.execute("SELECT COUNT(*) FROM questions WHERE source='official' "
                      "AND status='adopted'").fetchone()[0]
    pe = conn.execute("SELECT COUNT(*) FROM questions WHERE source='official' "
                      "AND status='pending'").fetchone()[0]
    integ = conn.execute("PRAGMA integrity_check").fetchone()[0][:30]
    conn.close()
    print("合并库：题数 %d，FTS %d，官方已采纳 %d，官方待确认 %d，integrity=%s"
          % (total, fts, ad, pe, integ))
    print("已生成", os.path.basename(MERGED))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

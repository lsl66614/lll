"""题库 SQLite 主库与 JSON 快照的一致性检查。

SQLite 是事实源，``bank.json`` 只是每 10 分钟导出的回滚快照（见
``main._periodic_bank_export``）。两边漂移时，本模块只报告；修复方向
**永远是 DB → JSON**，不反向覆盖主库。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from typing import Any

# 只比较业务字段。sh / img_key / options_norm / answer_norm / canonical_id / level
# 都是加载时重算的派生列，比较它们只会制造噪声。
COMPARE_FIELDS = (
    "type", "title", "options", "answer", "subject", "source", "status",
    "conflict", "image_hash", "history", "wrong_answers",
)


def _load_db_rows(path: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM questions")]
    finally:
        conn.close()


def _norm_value(field: str, value: Any) -> Any:
    if field == "options":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = []
        return [str(item) for item in (value or [])]
    if field in ("history", "wrong_answers"):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = []
        if not isinstance(value, list):
            return []
        if field == "wrong_answers":
            return [str(item) for item in value]
        # 旧记录可能缺 wrong 字段；加载时 Bank._norm_history 会补齐，
        # 这里按同一口径规范化，避免把「已归一化」误报成漂移。
        return [
            {
                "answer": str(item.get("answer") or ""),
                "source": str(item.get("source") or ""),
                "at": float(item.get("at") or 0.0),
                "wrong": bool(item.get("wrong")),
            }
            for item in value[-20:]
            if isinstance(item, dict) and item.get("answer")
        ]
    if field == "conflict":
        return bool(value)
    return str(value or "")


def _norm_row(row: dict[str, Any]) -> dict[str, Any]:
    result = {"id": str(row.get("id") or "")}
    for field in COMPARE_FIELDS:
        result[field] = _norm_value(field, row.get(field, [] if field in ("options", "history", "wrong_answers") else ""))
    return result


def compare(db_path: str, json_path: str, limit: int = 20) -> dict[str, Any]:
    """比较主库与 JSON 快照，返回可序列化的差异报告。"""
    report: dict[str, Any] = {
        "db": db_path, "json": json_path,
        "db_count": 0, "json_count": 0,
        "missing_in_json": [], "missing_in_db": [], "duplicate_ids": [],
        "field_diffs": [], "ok": False, "error": "",
    }
    try:
        db_rows = _load_db_rows(db_path)
    except (sqlite3.Error, OSError) as exc:
        report["error"] = "读取 DB 失败：%s" % exc
        return report
    try:
        with open(json_path, encoding="utf-8") as fp:
            raw = json.load(fp)
    except (OSError, ValueError) as exc:
        report["error"] = "读取 JSON 失败：%s" % exc
        return report

    items = raw.get("items", []) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        report["error"] = "JSON 结构不正确：items 不是数组"
        return report

    db_map: dict[str, dict[str, Any]] = {}
    for row in db_rows:
        item = _norm_row(row)
        db_map[item["id"]] = item
    json_map: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        norm = _norm_row(item)
        if norm["id"] in json_map:
            duplicates.append(norm["id"])
            continue
        json_map[norm["id"]] = norm

    db_ids, json_ids = set(db_map), set(json_map)
    diffs: list[dict[str, Any]] = []
    for item_id in sorted(db_ids & json_ids):
        left, right = db_map[item_id], json_map[item_id]
        for field in COMPARE_FIELDS:
            if left[field] != right[field]:
                diffs.append({"id": item_id, "field": field,
                              "db": left[field], "json": right[field]})

    report.update({
        "db_count": len(db_map),
        "json_count": len(json_map),
        "missing_in_json": sorted(db_ids - json_ids)[:limit],
        "missing_in_db": sorted(json_ids - db_ids)[:limit],
        "duplicate_ids": duplicates[:limit],
        "field_diffs": diffs[:limit],
    })
    report["missing_in_json_total"] = len(db_ids - json_ids)
    report["missing_in_db_total"] = len(json_ids - db_ids)
    report["field_diffs_total"] = len(diffs)
    report["ok"] = not (
        report["missing_in_json_total"] or report["missing_in_db_total"]
        or duplicates or diffs
    )
    return report


def rewrite_json(db_path: str, json_path: str, backup: bool = True) -> dict[str, Any]:
    """以 SQLite 为准重写 JSON 快照；旧快照默认先备份。

    只写 JSON，不碰主库。写入走 ``tmp + os.replace``，不会留下半截文件。
    """
    from .bank import Bank

    if not os.path.exists(db_path):
        return {"ok": False, "error": "DB 不存在：%s" % db_path}
    if backup and os.path.exists(json_path):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(json_path, "%s.bak-consistency-%s" % (json_path, stamp))

    bank = Bank(json_path, db_path)
    try:
        payload = bank.export_json()
        count = len(bank.all())
    finally:
        bank.close()
    tmp = json_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        fp.write(payload)
    os.replace(tmp, json_path)
    return {"ok": True, "items": count, "json": json_path}

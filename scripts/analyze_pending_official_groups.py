"""只读统计：同一组（题型/专业/题干/选项）里，pending official 与 adopted 记录并存的冲突面。

用于判断 _source_level 修复（pending official 从 4 级降为 2 级）在真实题库里的影响范围。
不改库。
"""
from __future__ import annotations

import collections
import json
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402
from app.bank import _norm_text, _norm_option  # noqa: E402


def opts_of(raw) -> tuple[str, ...]:
    value = raw
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = []
    return tuple(sorted(_norm_option(str(o), None) for o in (value or [])))


def main() -> int:
    path = config.BANK_DB_PATH.replace("\\", "/")
    print("库：%s" % path)
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM questions")]

    groups = collections.defaultdict(list)
    for r in rows:
        key = (r["type"], str(r.get("subject") or ""), _norm_text(str(r.get("title") or "")),
               opts_of(r.get("options")))
        groups[key].append(r)

    by_type = collections.Counter()
    examples = []
    for key, recs in groups.items():
        pend_official = [r for r in recs if r["source"] == "official" and r["status"] == "pending"]
        adopted = [r for r in recs if r["status"] == "adopted" and str(r.get("answer") or "").strip()]
        if not pend_official or not adopted:
            continue
        answers = {str(r.get("answer") or "").strip() for r in adopted}
        if not answers - {str(r.get("answer") or "").strip() for r in pend_official}:
            continue  # 答案一致，降不降级不影响选择
        by_type[key[0]] += 1
        if len(examples) < 8:
            examples.append((key[0], str(pend_official[0]["title"])[:36],
                             str(pend_official[0].get("answer") or ""),
                             sorted(answers)[:3]))

    print("并存且答案不一致的组：%d" % sum(by_type.values()))
    for qtype, count in sorted(by_type.items()):
        print("  %-11s %d 组" % (qtype, count))
    print()
    for qtype, title, ans, adopted_answers in examples:
        print("  [%s] %s | pending official=%s | adopted=%s" % (qtype, title, ans, adopted_answers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

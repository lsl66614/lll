"""逐条拆解判断题错误：网关答了什么、题库里同题的记录各是什么状态。

只读。用法：.venv\\Scripts\\python.exe scripts/diagnose_judge_errors.py [端口]
"""
from __future__ import annotations

import collections
import importlib.util
import json
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402
from app.bank import _norm_text, _norm_option  # noqa: E402
from app.shape_key import image_urls, text_key  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "ve", os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_e2e_accuracy.py"))
ve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ve)


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8010
    conn = sqlite3.connect("file:%s?mode=ro" % config.BANK_DB_PATH.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM questions WHERE type='judge'")]

    def opts_of(raw):
        value = raw
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = []
        return tuple(sorted(_norm_option(str(o), "judge") for o in (value or [])))

    by_key = collections.defaultdict(list)
    for r in rows:
        by_key[(_norm_text(str(r.get("title") or "")), opts_of(r.get("options")))].append(r)

    cases = [c for c in ve.build_cases() if c["type"] == "judge"]
    fails = []
    for case in cases:
        answer, source = ve.query(port, case)
        if answer is None or ve.answer_matches(answer, case):
            continue
        fails.append((case, answer, source))

    print("判断题用例 %d，错 %d" % (len(cases), len(fails)))
    print()
    src_counter = collections.Counter()
    status_counter = collections.Counter()
    for case, answer, source in fails:
        key = (_norm_text(case["title"]), tuple(sorted(_norm_option(o, "judge") for o in case["options"])))
        recs = by_key.get(key, [])
        src_counter[source] += 1
        print("━" * 78)
        print("题干:", case["title"][:70])
        print("真值:", case["truth"], " 网关:", repr(answer), " source:", source)
        print("图片指纹:", tuple(u.rsplit("/", 1)[-1][:12] for u in image_urls(case["title"])))
        for r in sorted(recs, key=lambda r: (r.get("source") != "official", r.get("created") or 0)):
            mark = ""
            if r["source"] == "official" and r["status"] == "pending":
                mark = "  ← pending official"
            if r["source"] == "official" and r["status"] == "adopted":
                mark = "  ← adopted official"
            status_counter[(r["source"], r["status"])] += 1
            print("   id=%s %-11s %-8s answer=%-6r options=%r%s" % (
                str(r.get("id"))[:8], r["source"], r["status"],
                str(r.get("answer") or ""), json.loads(r["options"]) if isinstance(r["options"], str) else r["options"],
                mark))
        if not recs:
            print("   （库里没有同键记录）")
    print()
    print("网关来源分布:", dict(src_counter))
    print("命中记录状态分布:", dict(status_counter))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

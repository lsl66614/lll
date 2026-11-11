"""检查 bank.db 与 bank.json 是否一致；--fix 以 SQLite 为准重写 JSON。

用法：
    .venv\\Scripts\\python.exe scripts/check_bank_consistency.py
    .venv\\Scripts\\python.exe scripts/check_bank_consistency.py --fix
    .venv\\Scripts\\python.exe scripts/check_bank_consistency.py --db data/x.db --json data/x.json

退出码：0 一致或已修复；1 读取失败；2 存在漂移且未修复。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config  # noqa: E402
from app.bank_consistency import compare, rewrite_json  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="题库主库与 JSON 快照一致性检查")
    parser.add_argument("--db", default=config.BANK_DB_PATH, help="SQLite 主库路径")
    parser.add_argument("--json", default=config.BANK_PATH, help="JSON 快照路径")
    parser.add_argument("--fix", action="store_true", help="以主库为准重写 JSON 快照")
    parser.add_argument("--limit", type=int, default=20, help="每类差异最多展示条数")
    parser.add_argument("--quiet", action="store_true", help="只输出 JSON 结果")
    args = parser.parse_args()

    report = compare(args.db, args.json, limit=max(1, args.limit))
    fixed = False
    if args.fix and not report["ok"] and not report["error"]:
        result = rewrite_json(args.db, args.json)
        report["fix"] = result
        fixed = bool(result.get("ok"))
        if fixed:
            report = compare(args.db, args.json, limit=max(1, args.limit))

    if args.quiet:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("DB   :", report["db"], "items=%d" % report["db_count"])
        print("JSON :", report["json"], "items=%d" % report["json_count"])
        if report["error"]:
            print("ERROR:", report["error"])
        else:
            print("结论 :", "一致" if report["ok"] else "存在漂移")
            for key, label in (("missing_in_json", "仅在 DB"),
                               ("missing_in_db", "仅在 JSON"),
                               ("duplicate_ids", "JSON 重复 id")):
                if report[key]:
                    print("  %s: %s" % (label, ", ".join(report[key])))
            if report["field_diffs"]:
                print("  字段差异: %d 条" % report["field_diffs_total"])
                for row in report["field_diffs"]:
                    print("   - %s %s: DB=%r JSON=%r"
                          % (row["id"][:12], row["field"], row["db"], row["json"]))
            if args.fix:
                print("修复 :", report.get("fix"))

    if report["error"]:
        return 1
    if report["ok"]:
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())

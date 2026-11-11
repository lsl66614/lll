"""回填题库的 canonical_id 列（规范记录标注）。

背景：同一道题在库里往往有多条记录（官方 / 人工 / AI 各一份，不同批次又各存一份）。
去重会删记录，风险高；P2 改为**只挂不删**：给每条记录标一个 ``canonical_id``，
指向同一道题里保留优先级最高的那条。``GROUP BY canonical_id`` 就能拿到整组。

口径与 ``dedupe`` 的合并闸门一致：
- 同题型 + 同「保留图片指纹的题干」+ 同选项集
- 不跨具体专业；答案冲突且无唯一官方答案的组不挂；图片选项指纹不一致的组不挂

用法：
    .venv\\Scripts\\python.exe scripts/backfill_canonical_id.py          # 只统计
    .venv\\Scripts\\python.exe scripts/backfill_canonical_id.py --apply  # 实际写入

注意：网关的 ``Bank`` 在加载和每次落盘时都会自己重算，所以步骤应是
**先重启网关（新代码已带上重算），再跑本脚本落盘**，避免内存旧值覆盖。
"""
from __future__ import annotations

import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402
from app.bank import Bank  # noqa: E402


def main() -> int:
    apply = "--apply" in sys.argv
    bank = Bank(os.path.join(config.DATA_DIR, "bank.json"), config.BANK_DB_PATH)
    try:
        stats = bank.recompute_canonicals()
        items = list(bank._items)
        non_self = [i for i in items if i.get("canonical_id") != i["id"]]
        by_canon: dict[str, int] = {}
        for item in items:
            by_canon[item["canonical_id"]] = by_canon.get(item["canonical_id"], 0) + 1
        multi = {cid: n for cid, n in by_canon.items() if n > 1}

        print("库：%s" % os.path.abspath(config.BANK_DB_PATH))
        print("模式：%s\n" % ("写入" if apply else "只统计（加 --apply 才写）"))
        print("总记录            %d" % len(items))
        print("  非规范记录      %d（挂在别的记录下）" % len(non_self))
        print("  规范记录        %d（自己指向自己）" % (len(items) - len(non_self)))
        print("  重复组          %d（一组 ≥2 条）" % len(multi))
        print("  最大组          %d 条" % (max(multi.values()) if multi else 0))
        print("  未挂组（闸门拦下）%d" % stats["skipped"])
        print()

        preview = sorted(multi.items(), key=lambda kv: -kv[1])[:5]
        for cid, count in preview:
            canon = bank.find_by_id(cid) or {}
            print("  %s × %d  %s" % (cid[:8], count, str(canon.get("title"))[:56]))

        if not apply:
            print("\n未写入。确认无误后加 --apply。")
            return 0
        pending = len(bank._dirty_ids)
        bank.flush()
        print("\n已写入（脏行 %d）。" % pending)
        return 0
    finally:
        bank.db.close()


if __name__ == "__main__":
    raise SystemExit(main())

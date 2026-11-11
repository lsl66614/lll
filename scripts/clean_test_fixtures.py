"""清掉题库里的自检/测试数据。

这些是开发期手动造出来的验证题，混进了生产题库（2026-11-10 实测 8 条）：
「统计落盘自检」「填空自检」「自动分类自检」「提速验证」等。

判据：题干里出现自检/测试类关键词。宁可漏删也不要误删真题 ——
所以关键词表要窄，且只在**题干短**（自检题都是一句话）时才判。

用法：
    .venv\\Scripts\\python.exe scripts/clean_test_fixtures.py            # 只报告
    .venv\\Scripts\\python.exe scripts/clean_test_fixtures.py --apply    # 落库
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config  # noqa: E402
from app.bank import Bank  # noqa: E402

# 自检/测试题的特征词
FIXTURE_MARKERS = (
    "自检", "提速验证", "提速验证题", "普通文字题测试", "不在题库",
    "填空自检", "自动分类自检", "长选项验证题",
)
# 只在这些词出现时才删，且题干不能太长（真题也可能提到"自检"）
MAX_TITLE_LENGTH = 60


def find_fixtures(bank: Bank) -> list[dict]:
    hits = []
    for item in bank.all():
        title = str(item.get("title") or "")
        if len(title) > MAX_TITLE_LENGTH:
            continue
        if any(marker in title for marker in FIXTURE_MARKERS):
            hits.append(item)
        elif item.get("subject") == "工程自检":
            hits.append(item)
    return hits


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    bank = Bank(config.BANK_PATH, config.BANK_DB_PATH)
    hits = find_fixtures(bank)
    print(f"题库 {len(bank.all())} 条，疑似自检/测试数据 {len(hits)} 条")
    for item in hits:
        print(f"  {item['id']} [{item.get('source')}/{item.get('status')}/{item.get('subject')}] "
              f"{item['title'][:60]}")
    if not args.apply:
        print("\n（dry-run，加 --apply 删除）")
        return 0

    for item in hits:
        bank.delete(item["id"])
    bank.flush()
    bank.checkpoint()
    print(f"\n已删除 {len(hits)} 条，剩余 {len(bank.all())} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

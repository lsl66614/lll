"""模拟：给「库存图被压成空括号」的兜底匹配加最短骨架长度，得失如何。

背景：same_question_truncated 的例外分支（请求有图、库存无图）只比文字骨架，
不设下限。``若 ，则 。（ ）`` 这类记录的骨架只有「若则」，会把大量不同的
公式判断题合并成一道，官方答案互相冒充。正常分支用的是 SKELETON_MIN_LEN=10。

只读：题库经 sqlite3 backup API 复制后加载。
用法：... simulate_blank_image_strictness.py [阈值,默认10]
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import bank as bank_mod  # noqa: E402
from app import config  # noqa: E402
from app.bank import Bank  # noqa: E402
from app.shape_key import (  # noqa: E402
    SKELETON_MIN_LEN, has_image, image_urls, text_key,
)

spec = importlib.util.spec_from_file_location(
    "ve", os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_e2e_accuracy.py"))
ve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ve)

_original = bank_mod.same_question_truncated


def make_strict(threshold: int):
    def same_question_truncated_strict(request: str, stored: str) -> bool:
        req_has_image = has_image(request)
        stored_has_image = has_image(stored)
        if not (req_has_image and stored_has_image):
            if not req_has_image or stored_has_image:
                return False
            req_skeleton = text_key(request)
            stored_skeleton = text_key(stored)
            # ← 新增：骨架太短不认（正常分支早就有这条）
            if len(req_skeleton) < threshold:
                return False
            return bool(req_skeleton) and req_skeleton == stored_skeleton
        return _original(request, stored)

    return same_question_truncated_strict


def backup_bank() -> str:
    src = sqlite3.connect(config.BANK_DB_PATH)
    dst_path = os.path.join(tempfile.mkdtemp(prefix="bankcopy-"), "bank.db")
    dst = sqlite3.connect(dst_path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return dst_path


def build_bank() -> Bank:
    db_copy = backup_bank()
    bank = Bank(os.path.join(os.path.dirname(db_copy), "bank.json"), db_copy)
    from app.formula_index import ImageTextStore
    bank.set_image_text(
        ImageTextStore(os.path.join(config.DATA_DIR, "image_text.json")).mapping())
    return bank


def official_answer(bank: Bank, case: dict) -> str | None:
    hit = bank.find_official(case["type"], case["title"], case["options"], "")
    return str(hit.get("answer") or "") if hit else None


def judge(answer: str | None, case: dict) -> str:
    if answer is None:
        return "none"
    return "right" if ve.answer_matches(answer, case) else "wrong"


def main() -> int:
    threshold = int(sys.argv[1]) if len(sys.argv) > 1 else SKELETON_MIN_LEN
    cases = [c for c in ve.build_cases() if c["type"] == "judge"]
    print("判断题用例 %d，阈值 %d（当前正常分支 SKELETON_MIN_LEN=%d）"
          % (len(cases), threshold, SKELETON_MIN_LEN))

    bank_mod.same_question_truncated = _original
    bank_of = build_bank()
    before = {id(c): official_answer(bank_of, c) for c in cases}
    bank_of2 = build_bank()
    bank_mod.same_question_truncated = make_strict(threshold)
    try:
        after = {id(c): official_answer(bank_of2, c) for c in cases}
    finally:
        bank_mod.same_question_truncated = _original

    cross = {}
    for c in cases:
        key = (judge(before[id(c)], c), judge(after[id(c)], c))
        cross[key] = cross.get(key, 0) + 1

    print()
    print("find_official 命中口径（before 现状 → after 收紧）：")
    for key in sorted(cross):
        print("  %-6s → %-6s  %d 条" % (key[0], key[1], cross[key]))

    changed = [c for c in cases if before[id(c)] != after[id(c)]]
    print()
    print("答案会变的题：%d 条" % len(changed))
    for c in changed[:20]:
        print("  真值=%r  现状官方=%r→%r  %s" % (
            c["truth"], before[id(c)], after[id(c)], c["title"][:60]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

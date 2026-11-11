"""回填题库的 options_norm / answer_norm 两列（P3）。

- ``options_norm``：与 ``options`` 同序的规范化内容（JSON 数组）；
- ``answer_norm``：与选项顺序无关的答案语义（单选/多选 = 所选选项内容，
  判断题 = 正确/错误，填空 = 原文）。字母越界/指向空选项时为空串。

两列都是**派生列**，由 ``Bank._normalize()`` 从 (type, options, answer) 纯计算得来。
本脚本按同一口径逐行计算并写库，顺便做一次「存量清理」体检：

- 字母答案里有没有换算不出语义的（越界 / 指向空选项）—— 这就是真正过期的答案；
- 判断题有没有把答案存成字母的（应为 0，语义词才是正确形态）；
- canonical 分组内成员的语义答案是否一致（不一致多半是历史答案冲突，
  只报告、不自动改写 —— 改答案属于数据决策，脚本不越权）。

用法：
    .venv\\Scripts\\python.exe scripts/backfill_options_norm.py          # 只统计
    .venv\\Scripts\\python.exe scripts/backfill_options_norm.py --apply  # 实际写入

⚠️ 写完后重启网关，让运行中的实例带着新列重新加载。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import checkup, config  # noqa: E402
from app.bank import Bank, _answer_semantic, _options_norm_list  # noqa: E402
from app.bankdb import BankDB  # noqa: E402


def main() -> int:
    apply = "--apply" in sys.argv
    path = os.path.abspath(config.BANK_DB_PATH)
    print("库：%s" % path)
    print("模式：%s\n" % ("写入" if apply else "只统计（加 --apply 才写）"))

    # 先走一遍真实迁移，保证两列存在（迁移只在 app/bankdb.py 一处实现）
    BankDB(path).close()

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT id, type, answer, options, status, canonical_id, options_norm, answer_norm "
            "FROM questions"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        print("读不了库：%s" % exc)
        return 1

    total = len(rows)
    changed = 0
    same = 0
    with_options = 0
    updates: list[tuple[str, str, str]] = []

    for qid, qtype, answer, options_raw, status, canonical_id, old_on, old_an in rows:
        try:
            options = json.loads(options_raw or "[]")
        except (TypeError, json.JSONDecodeError):
            options = []
        if not isinstance(options, list):
            options = []
        options = [str(o) for o in options]
        answer = str(answer or "").strip()
        if options:
            with_options += 1
        norm_options = _options_norm_list(options, qtype)
        norm_answer = _answer_semantic(answer, options, qtype)
        new_on = json.dumps(norm_options, ensure_ascii=False)
        try:
            stored_on = json.dumps(json.loads(old_on or "[]"), ensure_ascii=False)
        except (TypeError, json.JSONDecodeError):
            stored_on = ""
        if stored_on == new_on and str(old_an or "") == norm_answer:
            same += 1
        else:
            changed += 1
            updates.append((new_on, norm_answer, qid))

    print("总记录                    %d" % total)
    print("  有选项                  %d" % with_options)
    print("  已经是目标值            %d" % same)
    print("  需要更新                %d" % changed)
    print()

    # 存量体检统一走 app.checkup.light_metrics（Bank 加载时会按同一口径
    # 现算 answer_norm，所以 dry-run 拿到的就是写入后的体检结果）。
    bank = Bank(os.path.join(os.path.dirname(path), ".backfill-audit.json"), path)
    try:
        metrics = checkup.light_metrics(bank)
    finally:
        bank.db.close()
    gap = metrics["letter_unexplained"]
    judge_letters = metrics["judge_letter_answers"]
    conflicts = metrics["canonical_semantic_conflicts"]
    print("存量体检（app.checkup.light_metrics）：")
    print("  字母答案换算不出语义    %d（越界/指向空选项 = 过期答案）" % gap["count"])
    for sample in gap["samples"][:10]:
        print("    · %s [%s] answer=%r" % (sample.get("id"), sample.get("type"), sample.get("answer")))
    print("  判断题把答案存成字母    %d（应为 0：语义答案才是正确形态）" % judge_letters["count"])
    for sample in judge_letters["samples"][:10]:
        print("    · %s answer=%r" % (sample.get("id"), sample.get("answer")))
    print("  canonical 组内语义不一致 %d 组（只报告，不自动改答案）" % conflicts["count"])
    for row in conflicts["samples"][:6]:
        print("    · 组 %s [%s] %s — %s" % (
            str(row.get("canonical_id") or "")[:8], row.get("type"),
            str(row.get("title") or "")[:40],
            " ≠ ".join(map(str, row.get("answers") or []))))

    if not apply:
        print("\n未写入。确认无误后加 --apply。")
        return 0
    if not updates:
        print("\n没有需要更新的行。")
        return 0

    with conn:
        conn.executemany(
            "UPDATE questions SET options_norm = ?, answer_norm = ? WHERE id = ?", updates)
    left = conn.execute(
        "SELECT COUNT(*) FROM questions WHERE canonical_id != '' AND answer_norm = ''"
    ).fetchone()[0]
    conn.close()
    print("\n已写入 %d 行。canonical 组内 answer_norm 为空的行：%d" % (len(updates), left))
    print("⚠️ 记得重启网关，让运行中的实例重新加载。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

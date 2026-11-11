"""从快照恢复被清空的官方答案。

背景（2026-11-10 实测）：题库里 103 条 ``source=official`` 的记录状态是
``pending``、答案为空 —— 它们当初是被平台反馈判错的（旧版结果页解析器把
得分图标当成选项内容，采集到错的答案），于是被 ``mark_wrong`` 清空。

但**正确答案就躺在快照里**（结果页原始 HTML）。快照能匹配上 1220 次，
足够把正确答案补回去。

判据（避免补错）：
1. 题型一致、文字骨架一致；
2. 快照里的正确答案（按选项内容算）唯一 —— 自相矛盾的不补；
3. 补之前先确认库里的记录确实没有答案。

用法：
    .venv\\Scripts\\python.exe scripts/recover_official_answers.py          # 看清单
    .venv\\Scripts\\python.exe scripts/recover_official_answers.py --apply  # 落库
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config  # noqa: E402
from app.bank import Bank  # noqa: E402
from app.mooc_result import parse_result_html  # noqa: E402
from app.shape_key import text_key  # noqa: E402

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def snapshot_truth():
    """快照 → {(题型, 题干骨架): {正确选项内容集合}}，同时记录该题的选项列表。"""
    truth: dict[tuple[str, str], set] = {}
    options_map: dict[tuple[str, str], list] = {}
    snap_dir = config.SNAPSHOT_DIR
    if not os.path.isdir(snap_dir):
        return truth, options_map
    for name in sorted(os.listdir(snap_dir)):
        if not name.endswith(".html"):
            continue
        try:
            with open(os.path.join(snap_dir, name), "r", encoding="utf-8", errors="replace") as fp:
                html = fp.read()
        except OSError:
            continue
        if "u-questionItem" not in html:
            continue
        for item in parse_result_html(html):
            letters = item.get("correct_letters") or []
            options = item.get("options") or []
            if not letters or not options:
                continue
            good = set()
            for letter in letters:
                index = LETTERS.find(letter.upper())
                if 0 <= index < len(options):
                    good.add(options[index])
            if not good:
                continue
            key = (item["type"], text_key(item["title"]))
            truth.setdefault(key, set()).add(tuple(sorted(good)))
            options_map.setdefault(key, list(options))
    return truth, options_map


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    bank = Bank(config.BANK_PATH, config.BANK_DB_PATH)
    truth, snapshot_options_map = snapshot_truth()
    print("快照真值 %d 条" % len(truth))

    targets = [
        i for i in bank.all()
        if i.get("source") == "official" and i.get("status") == "pending"
        and not str(i.get("answer") or "").strip()
    ]
    print("待恢复的官方记录 %d 条\n" % len(targets))

    plan = []
    for item in targets:
        key = (item["type"], text_key(item["title"]))
        candidates = truth.get(key)
        if not candidates or len(candidates) != 1:
            continue
        new_options = None
        answer = ""
        good = list(candidates)[0]
        snap_options = list(snapshot_options_map.get(key) or [])
        if item["type"] == "judge":
            semantic = next((g for g in good if g in ("正确", "错误")), "")
            if not semantic:
                continue
            answer = semantic
        else:
            # 库里选项内容不可用（空白/图标文字）时，字母无从定位 ——
            # 直接用快照的选项重建，答案按新选项算。
            lib_options = [str(o or "").strip() for o in item["options"]]
            usable = all(lib_options) and len(lib_options) == len(snap_options)
            if usable:
                for index, opt in enumerate(item["options"]):
                    if any((opt or "").strip() in (g or "").strip()
                           or (g or "").strip() in (opt or "").strip() for g in good):
                        answer = LETTERS[index] if index < len(LETTERS) else ""
                        break
                if answer:
                    continue
            if not snap_options:
                continue
            # 用快照选项定位
            for index, opt in enumerate(snap_options):
                if any((opt or "").strip() in (g or "").strip()
                       or (g or "").strip() in (opt or "").strip() for g in good):
                    answer = LETTERS[index] if index < len(LETTERS) else ""
                    new_options = snap_options
                    break
            if not answer:
                continue
        row = {"id": item["id"], "answer": answer, "title": item["title"][:70]}
        if new_options:
            row["options"] = new_options
        plan.append(row)

    print("可恢复 %d 条：" % len(plan))
    for row in plan[:40]:
        print("  %s -> %-4s | %s" % (row["id"], row["answer"], row["title"]))
    if len(plan) > 40:
        print("  ... 还有 %d 条" % (len(plan) - 40))
    if not args.apply:
        print("\n（dry-run，加 --apply 落库）")
        return 0

    for row in plan:
        payload = {"answer": row["answer"], "status": "adopted",
                   "conflict": False, "agree": 2}
        if row.get("options"):
            payload["options"] = row["options"]
        bank.update(row["id"], payload)
    print("\n已恢复 %d 条官方答案" % len(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

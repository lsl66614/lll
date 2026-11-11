"""核对并修正「paper 通道」写错的判断题答案。

背景（2026-11-10）：paper 通道（官方卷 DTO）对判断题的答案不可靠 ——
判断题的两个选项是 ✓/✗ 图标，DTO 的 ``optionDtos[].answer`` 标记有时没带上，
于是取到了错误的那一项。实测「若事件…相互独立，则…」平台答案是「正确」，
库里却存成「错误」。

修正办法：拿快照（结果页原始 HTML）里的正确选项语义作基准，逐条核对
「official + via=paper + judge」的记录，不一致的按快照改。

快照自相矛盾的（同一题在不同页面给出不同语义）不修 —— 那是真歧义。
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config  # noqa: E402
from app.bank import Bank  # noqa: E402
from app.mooc_result import parse_result_html  # noqa: E402
from app.shape_key import text_key  # noqa: E402

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def snapshot_judge_truth():
    """快照 → {(题干骨架): {正确语义集合}}（判断题专用）。"""
    truth: dict[str, set] = {}
    snap_dir = config.SNAPSHOT_DIR
    if not os.path.isdir(snap_dir):
        return truth
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
            if item.get("type") != "judge":
                continue
            letters = item.get("correct_letters") or []
            options = item.get("options") or []
            if len(letters) != 1 or len(options) != 2:
                continue
            index = LETTERS.find(letters[0].upper())
            if not (0 <= index < len(options)):
                continue
            semantic = options[index]
            if semantic not in ("正确", "错误"):
                continue
            truth.setdefault(text_key(item["title"]), set()).add(semantic)
    return truth


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    bank = Bank(config.BANK_PATH, config.BANK_DB_PATH)
    truth = snapshot_judge_truth()
    print("快照里的判断题真值 %d 条" % len(truth))

    targets = [
        i for i in bank.all()
        if i.get("type") == "judge" and i.get("source") == "official"
        and i.get("status") == "adopted" and str(i.get("answer") or "").strip()
    ]
    plan = []
    ambiguous = 0
    for item in targets:
        candidates = truth.get(text_key(item["title"]))
        if not candidates or len(candidates) != 1:
            if candidates and len(candidates) > 1:
                ambiguous += 1
            continue
        right = next(iter(candidates))
        if right != item["answer"]:
            plan.append({"id": item["id"], "from": item["answer"], "to": right,
                         "title": item["title"][:70]})

    print("发现答案不符 %d 条（快照自相矛盾跳过 %d 条）\n" % (len(plan), ambiguous))
    for row in plan[:30]:
        print("  %s %r -> %r | %s" % (row["id"], row["from"], row["to"], row["title"]))
    if not args.apply:
        print("\n（dry-run，加 --apply 落库）")
        return 0

    for row in plan:
        bank.update(row["id"], {"answer": row["to"], "conflict": False})
    print("\n已修正 %d 条判断题官方答案" % len(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

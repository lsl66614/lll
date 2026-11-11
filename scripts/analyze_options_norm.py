"""P3 体检：字母答案 / 选项顺序 / 语义答案可推导性的存量摸底（只读）。

回答三个问题：
1. 库里 single/multiple 的答案有多少是字母、多少已经是语义文本、多少无法解释；
2. 字母答案里有多少「指不到自己选项」（越界 / 指向空选项）——这才是真正过期的答案；
3. canonical 分组内，按「字母 → 选项内容」换算后有多少成员与规范记录的语义答案不一致。

用法：
    .venv\\Scripts\\python.exe scripts/analyze_options_norm.py
"""
from __future__ import annotations

import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402
from app.bank import (  # noqa: E402
    Bank, _LETTERS, _is_positional_options, _norm_option,
)

LETTERS = set(_LETTERS)


def letter_parts(answer: str) -> list[str] | None:
    """答案是否纯字母形式（A / A#C）；不是返回 None。"""
    parts = [p.strip().upper() for p in str(answer or "").split("#") if p.strip()]
    if not parts or any(p not in LETTERS for p in parts):
        return None
    return parts


def semantic_of(item: dict) -> str | None:
    """把字母答案换算成（有序）选项内容的规范形式；无法换算返回 None。"""
    parts = letter_parts(item.get("answer"))
    if parts is None:
        # 非字母答案：判断题的「正确/错误」、填空题的文本都直接算语义答案
        text = str(item.get("answer") or "").strip()
        return text or None
    options = item.get("options") or []
    norm = [_norm_option(o, item["type"]) for o in options]
    picked = []
    for part in parts:
        index = _LETTERS.index(part)
        if index >= len(norm) or not norm[index]:
            return None
        picked.append(norm[index])
    return "#".join(sorted(picked))


def main() -> int:
    bank = Bank(os.path.join(config.DATA_DIR, "bank.json"), config.BANK_DB_PATH)
    try:
        items = list(bank._items)
        print("库：%s" % os.path.abspath(config.BANK_DB_PATH))
        print("总记录 %d\n" % len(items))

        by_type: dict[str, int] = {}
        for item in items:
            by_type[item["type"]] = by_type.get(item["type"], 0) + 1
        print("按题型：" + "，".join("%s %d" % kv for kv in sorted(by_type.items())))

        choice = [i for i in items if i["type"] in ("single", "multiple")]
        positional = [i for i in choice if _is_positional_options(i.get("options") or [])]
        letter = [i for i in choice if letter_parts(i.get("answer")) is not None]
        nonletter = [i for i in choice if letter_parts(i.get("answer")) is None]
        print("\n[单选题/多选题] %d 条" % len(choice))
        print("  选项是纯字母（图片题，字母即位置） %d" % len(positional))
        print("  答案是纯字母                      %d" % len(letter))
        print("  答案不是字母                      %d" % len(nonletter))
        for item in nonletter[:8]:
            print("    · %s [%s] answer=%r options=%s" % (
                item["id"], item["type"], item.get("answer"),
                [str(o)[:24] for o in (item.get("options") or [])]))

        # 字母答案指不到自己选项
        out_of_range, empty_target, order_scrambled = [], [], []
        for item in letter:
            options = item.get("options") or []
            norm = [_norm_option(o, item["type"]) for o in options]
            for part in letter_parts(item.get("answer")) or []:
                index = _LETTERS.index(part)
                if index >= len(norm):
                    out_of_range.append(item)
                    break
                if not norm[index]:
                    empty_target.append(item)
                    break
            else:
                keys = [k for k in (letter_parts(item.get("answer")) or [])]
                # 字母不是升序：旧数据里出现过 C#A 这种写法
                if keys != sorted(keys):
                    order_scrambled.append(item)
        print("\n  字母越界（答案指向不存在的选项） %d" % len(out_of_range))
        for item in out_of_range[:9]:
            print("    · %s answer=%r options=%s" % (
                item["id"], item.get("answer"),
                [str(o)[:24] for o in (item.get("options") or [])]))
        print("  字母指向空选项                    %d" % len(empty_target))
        for item in empty_target[:9]:
            print("    · %s answer=%r options=%s" % (
                item["id"], item.get("answer"),
                [str(o)[:24] for o in (item.get("options") or [])]))
        print("  字母未升序（C#A 这种）            %d" % len(order_scrambled))
        for item in order_scrambled[:9]:
            print("    · %s answer=%r" % (item["id"], item.get("answer")))

        # 选项顺序在库里是否真的出现过差异
        groups: dict[tuple, list[dict]] = {}
        for item in items:
            if item["type"] not in ("single", "multiple", "judge"):
                continue
            key = (item["type"], Bank._norm_options_key(item.get("options") or [], item["type"]))
            if not key[1]:
                continue
            groups.setdefault(key, []).append(item)
        reordered = 0
        reorder_examples = []
        for members in groups.values():
            orders = {tuple(item.get("options") or []) for item in members}
            if len(orders) > 1:
                reordered += 1
                if len(reorder_examples) < 6:
                    reorder_examples.append(members)
        print("\n[同选项集分组] %d 组" % len(groups))
        print("  组内出现不同选项顺序的            %d 组" % reordered)
        for members in reorder_examples:
            print("    · %s" % str(members[0]["title"])[:48])
            for item in members[:3]:
                print("        %s answer=%-4r options=%s" % (
                    item["id"], item.get("answer"),
                    [str(o)[:18] for o in (item.get("options") or [])]))

        # canonical 组内语义答案是否一致
        disagree, unresolvable = [], []
        canon_groups: dict[str, list[dict]] = {}
        for item in items:
            cid = item.get("canonical_id") or item["id"]
            canon_groups.setdefault(cid, []).append(item)
        multi = {k: v for k, v in canon_groups.items() if len(v) > 1}
        for cid, members in multi.items():
            canon = next((m for m in members if m["id"] == cid), members[0])
            canon_sem = semantic_of(canon)
            if canon_sem is None:
                continue
            for item in members:
                if item is canon:
                    continue
                sem = semantic_of(item)
                if sem is None:
                    unresolvable.append(item)
                elif sem != canon_sem and item["type"] in ("single", "multiple", "judge"):
                    disagree.append((canon, item, canon_sem, sem))
        print("\n[规范分组] %d 组（≥2 条）" % len(multi))
        print("  组内成员无法换算语义答案          %d" % len(unresolvable))
        for item in unresolvable[:9]:
            print("    · %s answer=%r options=%s" % (
                item["id"], item.get("answer"),
                [str(o)[:20] for o in (item.get("options") or [])]))
        print("  组内成员与规范记录语义答案不一致  %d" % len(disagree))
        for canon, item, a, b in disagree[:12]:
            print("    · 规范 %s=%r  vs  %s=%r  %s" % (
                canon["id"], a, item["id"], b, str(canon["title"])[:40]))
        return 0
    finally:
        bank.db.close()


if __name__ == "__main__":
    raise SystemExit(main())

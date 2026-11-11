"""修正「按字母存答案」导致的陈旧字母（单选/多选）。

背景（2026-11-10）：题库把单选/多选答案存成「字母」，字母只在**当时的选项顺序**里
才有意义。OCS 同一道题在不同场次会换选项顺序（实测 46/135 题选项集变过，正确答案
的字母变了 190 次），于是库里那个字母可能指向另一个选项 —— 题库看着「有官方答案」，
实际给出去的是错的。

症状高度集中：**判断题和填空题全对，错的全是单选**。
判断题存的是语义词（正确/错误）、填空题存的是原文，都不受换序影响。

判定办法（不看字母，看**内容**）：
1. 从快照（结果页原始 HTML）取每道题「平台正确答案对应的选项内容」。
2. 取题库记录当前存的字母 + 它自己的选项顺序，算出这个字母指向哪个内容。
3. 两者不一致 → 字母已陈旧，按快照重写字母，并**把选项顺序一并改成快照的顺序**，
   让字母与顺序永远自洽（否则下次换序又会错）。

只处理「选项集相同」的题：选项内容都换了的题是另一道题，不能套用。
快照自相矛盾（同一题在不同页面给出不同内容）的不修 —— 那是真歧义。

用法：先 dry-run 看计划，确认后加 --apply 落库。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:  # 控制台是 GBK 时打印中文会抛 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):  # pragma: no cover - 非标准输出流
    pass

from app import config  # noqa: E402
from app.bank import Bank, _LETTERS, _norm_option  # noqa: E402
from app.mooc_result import parse_result_html  # noqa: E402
from app.shape_key import text_key  # noqa: E402


def _option_key(option: str) -> str:
    """选项的身份键。

    图片选项在题库里和在快照里可能来自不同的图床（edu-image / mooc-image），
    完整 URL 不同但文件名（图片的 uuid）相同——那才是「同一个选项」。
    所以图片按文件名比对，文字按归一化文本比对。
    """
    text = str(option or "").strip()
    if text.startswith("http"):
        name = text.split("?")[0].rstrip("/").split("/")[-1]
        return "img:" + name.lower()
    return "txt:" + _norm_option(text, "single")


def snapshot_truth():
    """快照 → {(题干骨架): {(正确内容, 全部选项)}}。

    只收「选项集唯一」的结果：同一题在不同快照里正确答案内容不同 → 记为歧义，丢弃。
    """
    truth: dict[str, tuple[str, list[str]]] = {}
    ambiguous: set[str] = set()
    snap_dir = config.SNAPSHOT_DIR
    if not os.path.isdir(snap_dir):
        return truth, ambiguous
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
        try:
            items = parse_result_html(html)
        except Exception:  # noqa: BLE001 - 单个快照解析失败不影响整体
            continue
        for item in items:
            if item.get("type") not in ("single", "multiple"):
                continue
            letters = [str(x).upper() for x in (item.get("correct_letters") or [])]
            options = list(item.get("options") or [])
            if not letters or len(options) < 2:
                continue
            picked = []
            for letter in letters:
                index = _LETTERS.find(letter)
                if not (0 <= index < len(options)):
                    picked = []
                    break
                picked.append(options[index])
            if not picked:
                continue
            # 用「选项集」做键，而不是题干：题干里的图片 URL 在题库和快照里
            # 可能来自不同图床，text_key 对不上；选项集（按图片文件名归一）才稳定。
            key = tuple(sorted(_option_key(o) for o in options))
            fingerprint = (key, tuple(sorted(_option_key(p) for p in picked)))
            previous = truth.get(key)
            if previous is None:
                truth[key] = fingerprint
            elif previous != fingerprint:
                # 同题不同快照给出不同内容 → 歧义，不修
                ambiguous.add(key)
    for key in ambiguous:
        truth.pop(key, None)
    return truth, ambiguous


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    bank = Bank(config.BANK_PATH, config.BANK_DB_PATH)
    truth, ambiguous = snapshot_truth()
    print("快照里的单选/多选真值 %d 条（歧义跳过 %d 条）" % (len(truth), len(ambiguous)))

    targets = [
        i for i in bank.all()
        if i.get("type") in ("single", "multiple")
        and i.get("source") == "official"
        and i.get("status") == "adopted"
        and str(i.get("answer") or "").strip()
    ]

    plan = []
    skipped_no_truth = 0
    for item in targets:
        bank_options = list(item.get("options") or [])
        if len(bank_options) < 2:
            continue
        option_keys = tuple(sorted(_option_key(o) for o in bank_options))
        fingerprint = truth.get(option_keys)
        if not fingerprint:
            skipped_no_truth += 1
            continue
        _, answer_keys = fingerprint
        answer = str(item["answer"]).strip().upper()
        parts = [p for p in answer.split("#") if p]
        if not parts or any(p not in _LETTERS for p in parts):
            continue
        # 库里这个字母指向哪个内容
        pointed = []
        for part in parts:
            index = _LETTERS.index(part)
            if index >= len(bank_options):
                pointed = []
                break
            pointed.append(_option_key(bank_options[index]))
        if not pointed:
            continue
        if tuple(sorted(pointed)) == tuple(sorted(answer_keys)):
            continue  # 已经自洽，不用修
        # 把正确答案内容换算成「基于题库当前选项顺序」的字母。
        # 字母与题库自己的选项顺序绑定 → 永远自洽，查询时 translate_answer
        # 能按内容重映射到当次请求的顺序。
        fixed = []
        for content in answer_keys:
            index = next((k for k, o in enumerate(bank_options)
                          if _option_key(o) == content), None)
            if index is None:
                fixed = []
                break
            fixed.append(_LETTERS[index])
        if not fixed:
            continue
        plan.append({
            "id": item["id"],
            "from": answer,
            "to": "#".join(sorted(fixed)),
            "title": item["title"][:70],
        })

    print("发现陈旧字母 %d 条（快照无真值跳过 %d 条）\n" % (len(plan), skipped_no_truth))
    for row in plan[:30]:
        print("  %s %r -> %s | %s" % (row["id"], row["from"], row["to"], row["title"]))
    if not args.apply:
        print("\n（dry-run，加 --apply 落库）")
        return 0

    for row in plan:
        bank.update(row["id"], {"answer": row["to"], "conflict": False})
    print("\n已修正 %d 条单选/多选的陈旧字母" % len(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""把「同骨架多题」那批无图判断题补成带图版本（从快照重建）。

背景：库里早期结果页解析器把图片抹成空括号，题干只剩 `设 ，则 。（ ）`。
`same_question_truncated` 的空图兜底只比文字骨架，`若则`/`设` 这类骨架被
大量不同公式题共享，于是给出别的题的官方答案。

修法（不改匹配规则）：`find_official` 里精确命中优先于空图兜底
（`if not hits: hits = truncated_hits`）。给 12 个「真值不唯一」的骨架，
每个变体补一条**题干与快照完全一致**的 official+adopted 记录（图片 URL 保留、
image_hash 留空），新记录进 `hits`，旧的空壳记录自动退成 `truncated_hits`。

默认空跑；加 --apply 才写入运行中的网关（POST /api/bank，走内存+落盘）。
"""
from __future__ import annotations

import collections
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402
from app.bank import Bank, _norm_text, DEFAULT_SUBJECT  # noqa: E402
from app.formula_index import ImageTextStore  # noqa: E402
from app.shape_key import text_key  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "ve", os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_e2e_accuracy.py"))
ve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ve)


def load_bank() -> Bank:
    src = sqlite3.connect(config.BANK_DB_PATH)
    tmp = tempfile.mkdtemp(prefix="bank-ro-")
    path = os.path.join(tmp, "bank.db")
    dst = sqlite3.connect(path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    bank = Bank(os.path.join(tmp, "bank.json"), path)
    bank.set_image_text(ImageTextStore(os.path.join(config.DATA_DIR, "image_text.json")).mapping())
    return bank


def build_records(bank: Bank):
    cases = [c for c in ve.build_cases() if c["type"] == "judge"]
    groups = collections.defaultdict(set)
    for c in cases:
        groups[text_key(c["title"])].add(c["truth"][0])
    ambiguous = {k for k, v in groups.items() if len(v) > 1}

    # 现有精确记录（题干 _norm_text 相同 + 选项等价）→ 已覆盖的变体
    existing = set()
    for item in bank._items:
        if item["type"] != "judge":
            continue
        existing.add((_norm_text(item["title"]),
                      tuple(sorted(str(o) for o in (item["options"] or [])))))
    with_answer = collections.defaultdict(set)
    for item in bank._items:
        if item["type"] == "judge":
            with_answer[(_norm_text(item["title"]),
                         tuple(sorted(str(o) for o in (item["options"] or []))))].add(
                (item.get("source"), item.get("status"), str(item.get("answer") or "")))

    todo, covered, conflicts = [], [], []
    seen = set()
    for c in cases:
        if text_key(c["title"]) not in ambiguous:
            continue
        key = (_norm_text(c["title"]), tuple(sorted(c["options"])))
        if key in seen:
            continue
        seen.add(key)
        if key in existing:
            answers = {a for _, status, a in with_answer.get(key, set()) if status == "adopted"}
            if c["truth"][0] in answers:
                covered.append(c["title"])
                continue
            conflicts.append((c["title"], sorted(answers), c["truth"][0]))
        todo.append(c)

    return ambiguous, cases, todo, covered, conflicts


def post_bank(item: dict) -> dict:
    token = os.getenv("GATEWAY_TOKEN", "").strip()
    payload = json.dumps(item, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d/api/bank" % config.PORT, data=payload,
        headers={"Content-Type": "application/json", "X-Gateway-Token": token},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    apply = "--apply" in sys.argv
    bank = load_bank()
    ambiguous, cases, todo, covered, conflicts = build_records(bank)

    print("判断题用例 %d 条，歧义骨架 %d 个" % (len(cases), len(ambiguous)))
    print("需要补录的变体：%d 条" % len(todo))
    print("已有精确记录且答案一致（跳过）：%d 条" % len(covered))
    print("已有精确记录但答案不一致（会被新记录按新旧压过）：%d 条" % len(conflicts))
    for title, answers, truth in conflicts[:10]:
        print("   冲突 %-40s 现有=%s 真值=%r" % (str(title)[:40], answers, truth))
    print()
    for c in todo:
        print("  + [%s] %s | 真值=%s" % (
            "空骨架" if not text_key(c["title"]) else "文字骨架",
            str(c["title"])[:72], c["truth"][0]))

    if not apply:
        print("\n空跑结束（加 --apply 写入）")
        return 0

    # GATEWAY_TOKEN 为空 = 服务端不校验（require_token 直接返回），不需要传
    ok = 0
    for c in todo:
        item = {
            "type": "judge",
            "title": c["title"],
            "options": list(c["options"]),
            "answer": c["truth"][0],
            "subject": DEFAULT_SUBJECT,
            "source": "official",
            "status": "adopted",
            "image_hash": "",
            "via": "snapshot-restore",
        }
        try:
            saved = post_bank(item)
        except Exception as exc:  # noqa: BLE001 - 逐条汇报，不中断
            print("  写入失败：%s | %s" % (str(c["title"])[:50], exc))
            continue
        ok += 1
        print("  已写入 %s | %s" % (str(saved.get("id"))[:8], str(c["title"])[:50]))
    print("\n写入完成：%d/%d" % (ok, len(todo)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

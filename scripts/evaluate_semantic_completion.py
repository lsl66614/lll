"""评估语义召回对答题的影响（只读，不写真实题库）。

背景：真实网关里，填空题「题库无记录」时经常被 ``match(semantic=True)`` 命中
一条语义相近但不同题的记录（实测 5/5 全错）。本脚本用 711 条快照用例复算：

1. 关闭语义（semantic=False）时，题库能命中多少、对多少；
2. 开启语义后，语义分支额外命中多少、其中对/错各多少；
3. 几种收紧策略分别能挡掉多少错、误伤多少对。

用法：
    .venv\\Scripts\\python.exe scripts/evaluate_semantic_completion.py
"""
from __future__ import annotations

import collections
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config  # noqa: E402
from app.bank import Bank, DEFAULT_SUBJECT  # noqa: E402
from app.formula_index import ImageTextStore  # noqa: E402
from app.semantic import SemanticStore  # noqa: E402
from app.shape_key import text_key  # noqa: E402
from app.subjects import SubjectRules  # noqa: E402
from app.bank import _dice, _norm_text  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "ve", os.path.join(ROOT, "scripts", "verify_e2e_accuracy.py"))
ve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ve)


def copy_sqlite(src_path: str, dst_path: str) -> None:
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()


def load_all(tmp: str) -> tuple[Bank, SemanticStore]:
    copy_sqlite(config.BANK_DB_PATH, os.path.join(tmp, "bank.db"))
    copy_sqlite(config.SEMANTIC_DB, os.path.join(tmp, "semantic.db"))
    bank = Bank(os.path.join(tmp, "bank.json"), os.path.join(tmp, "bank.db"))
    bank.set_image_text(
        ImageTextStore(os.path.join(config.DATA_DIR, "image_text.json")).mapping())
    store = SemanticStore(config.SEMANTIC_DIR, os.path.join(tmp, "semantic.db"))
    bank.set_semantic(store)
    return bank, store


def strict_ok(got: str, case: dict) -> bool:
    """严格判定：填空比原文；判断题比选项内容；单选/多选按字母映射回内容。"""
    if not got:
        return False
    if case["type"] == "completion":
        return ve.norm_text(got) == ve.norm_text(case["truth"][0])
    return ve.answer_matches(got, case)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="sem-eval-")
    bank = None
    try:
        bank, _store = load_all(tmp)
        rules = SubjectRules(config.SUBJECTS_PATH)
        cases = ve.build_cases()
        rows = []
        for case in cases:
            subject, _ = rules.infer(case["title"], case["options"])
            subject = subject or DEFAULT_SUBJECT
            base = bank.match(case["type"], case["title"], case["options"], subject,
                              None, semantic=False)
            sem = bank.match(case["type"], case["title"], case["options"], subject,
                             None, semantic=True)
            base_answer = str((base or {}).get("answer") or "")
            sem_answer = str((sem or {}).get("answer") or "")
            base_ok = ve.answer_matches(base_answer, case) if base else False
            sem_ok = ve.answer_matches(sem_answer, case) if sem else False
            semantic_hit = bool(sem and sem.get("_match") == "semantic")
            rows.append({
                "case": case,
                "base": base,
                "sem": sem,
                "base_ok": base_ok,
                "sem_ok": sem_ok,
                "semantic_hit": semantic_hit,
                "sem_strict": strict_ok(sem_answer, case),
            })

        by_type = collections.defaultdict(lambda: {
            "total": 0, "base_hit": 0, "base_ok": 0, "base_bad": 0,
            "sem_hit": 0, "sem_ok": 0, "sem_bad": 0, "sem_strict": 0,
        })
        sem_hits = []
        for row in rows:
            case = row["case"]
            slot = by_type[case["type"]]
            slot["total"] += 1
            if row["base"]:
                slot["base_hit"] += 1
                slot["base_ok" if row["base_ok"] else "base_bad"] += 1
            if row["semantic_hit"]:
                slot["sem_hit"] += 1
                slot["sem_ok" if row["sem_ok"] else "sem_bad"] += 1
                slot["sem_strict"] += 1 if row["sem_strict"] else 0
                sem_hits.append(row)

        print("快照用例 %d；有语义命中 %d 条" % (len(rows), len(sem_hits)))
        print("\n按题型：")
        print("%-10s %5s %8s %8s %8s %8s %8s %8s" % (
            "type", "total", "base命中", "base对", "base错", "语义命中", "语义对", "语义错"))
        for qtype, slot in sorted(by_type.items()):
            print("%-10s %5d %8d %8d %8d %8d %8d %8d" % (
                qtype, slot["total"], slot["base_hit"], slot["base_ok"], slot["base_bad"],
                slot["sem_hit"], slot["sem_ok"], slot["sem_bad"]))

        print("\n语义命中明细（严格文本判定）：")
        for row in sem_hits:
            case = row["case"]
            sem = row["sem"] or {}
            truth = (case["truth"][0] if case["type"] == "completion"
                     else " / ".join(str(x) for x in case["truth"]))
            got = str(sem.get("answer") or "")
            score = sem.get("_score")
            cand_title = str(sem.get("title") or "")
            lex = _dice(_norm_text(case["title"]), _norm_text(cand_title)) if cand_title else 0.0
            print("  [%s] %-6s score=%.3f lex=%.2f src=%-9s status=%-7s 真值=%-12r 返回=%-12r %s"
                  % (case["type"], "OK" if row["sem_strict"] else "NG", score or 0.0, lex,
                     sem.get("source"), sem.get("status"), truth[:12], got[:12],
                     "STRICT" if row["sem_strict"] else ""))
            print("        请求：%s" % str(case["title"])[:76].replace("\n", " "))
            print("        命中：%s" % cand_title[:76].replace("\n", " "))

        # 各种收紧策略：在语义命中集合上统计「挡掉错 / 误伤对」
        def guard_report(name: str, predicate) -> None:
            blocked_ok = sum(1 for r in sem_hits if r["sem_strict"] and predicate(r))
            blocked_bad = sum(1 for r in sem_hits if not r["sem_strict"] and predicate(r))
            kept_ok = sum(1 for r in sem_hits if r["sem_strict"] and not predicate(r))
            kept_bad = sum(1 for r in sem_hits if not r["sem_strict"] and not predicate(r))
            print("  %-28s 挡掉：对 %2d / 错 %2d   保留：对 %2d / 错 %2d"
                  % (name, blocked_ok, blocked_bad, kept_ok, kept_bad))

        print("\n收紧策略（每行：该策略会挡掉哪些语义命中）：")
        guard_report("仅关闭填空题语义", lambda r: r["case"]["type"] == "completion")
        for n in (4, 6, 8, 10, 12):
            guard_report("题干骨架长度 < %d" % n,
                         lambda r, n=n: len(text_key(r["case"]["title"])) < n)
        for t in (0.92, 0.94, 0.96, 0.98):
            guard_report("语义分数 < %.2f" % t,
                         lambda r, t=t: float((r["sem"] or {}).get("_score") or 0) < t)
        guard_report("命中来源非官方/人工/导入",
                     lambda r: (str((r["sem"] or {}).get("source") or "") not in
                                ("official", "manual", "import")
                                or str((r["sem"] or {}).get("status") or "") != "adopted"))
        for t in (0.4, 0.5, 0.6, 0.7):
            guard_report("题干字面相似 < %.1f" % t,
                         lambda r, t=t: _dice(
                             _norm_text(r["case"]["title"]),
                             _norm_text(str((r["sem"] or {}).get("title") or ""))) < t)
        return 0
    finally:
        if bank is not None and getattr(bank, "db", None) is not None:
            try:
                bank.db.close()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

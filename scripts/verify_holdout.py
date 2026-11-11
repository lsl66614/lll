"""hold-out 验证：只在「没有参与补录」的用例上测端到端准确率。

背景：2026-11-10 补录的 38 条 official 记录，是从 46 条判断题用例的快照直接建的；
再用同一批用例去测，等于训练集=测试集 —— 99.3% 只能证明机制生效，不能证明泛化。

本脚本把「参与补录的用例」精确剔除，在剩下的用例上重测：
  · 用例身份 = (题型, 文字骨架, 题干图片 URL 序列, 选项集) —— 与 verify_e2e 完全一致
  · 补录用例 = 题库里 ``via='snapshot-restore'`` 的记录，按
    (题型, ``_norm_text`` 题干, 排序后的选项) 与用例对齐
  · 剔除后剩下的就是 hold-out：**没有任何一条记录是为它们专门写进去的**

另外按「这条题在快照里出现过几次」分组：只出现一次的题，是真正的新题；
出现多次的题，补录机制本来就有机会帮到。两组分开报，避免把重复题的效果
算成泛化。

用法：
    .venv\\Scripts\\python.exe scripts/verify_holdout.py [端口]
"""
from __future__ import annotations

import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402
from app.bank import _norm_text  # noqa: E402
from app.evaluation import collect_cases, imported_ids  # noqa: E402
from app.shape_key import text_key  # noqa: E402

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "ve", os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_e2e_accuracy.py"))
ve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ve)


def collect() -> tuple[dict[tuple, dict], dict[tuple, int]]:
    """兼容旧名：用例收集统一走 ``app/evaluation.collect_cases``。"""
    return collect_cases()


def bank_diff(rows) -> int:
    """只在 hold-out 上比对「补录前 vs 补录后」的题库直答，证明补录没有弄坏别的题。

    直接用 Bank.match（不走 HTTP、不喂 AI）：这里要回答的是「新增 38 条记录
    会不会改变 hold-out 用例的命中结果」，AI 兜底会把差异掩盖掉。
    """
    import shutil
    import sqlite3
    import tempfile

    from app.bank import Bank
    from app.formula_index import ImageTextStore

    backups = sorted(
        os.path.join(config.DATA_DIR, "backups", f)
        for f in os.listdir(os.path.join(config.DATA_DIR, "backups"))
        # 必须排除 .db-wal / .db-shm：按字符串排序时 "-" 排在 "." 前面，
        # 只按前缀挑会正好选中 WAL 文件，备份 API 打出来是个空库。
        if f.startswith("bank-before-judge-restore-") and f.endswith(".db")
    )
    if not backups:
        print("找不到补录前备份 bank-before-judge-restore-*.db")
        return 1
    before_path = backups[-1]
    print("补录前备份：%s" % os.path.basename(before_path))

    tmpdir = tempfile.mkdtemp(prefix="holdout-diff-")
    banks = []
    try:
        for label, src_path in (("before", before_path), ("after", config.BANK_DB_PATH)):
            src = sqlite3.connect(
                "file:%s?mode=ro" % os.path.abspath(src_path).replace("\\", "/"), uri=True)
            dst = sqlite3.connect(os.path.join(tmpdir, label + ".db"))
            src.backup(dst)
            src.close()
            dst.close()
            bank = Bank(os.path.join(tmpdir, label + ".json"), os.path.join(tmpdir, label + ".db"))
            bank.set_image_text(
                ImageTextStore(os.path.join(config.DATA_DIR, "image_text.json")).mapping())
            banks.append(bank)
        before_bank, after_bank = banks

        before_ok = after_ok = 0
        rescued = broken = 0
        for _, case in rows:
            answers = []
            for bank in (before_bank, after_bank):
                hit = bank.match(case["type"], case["title"], case["options"], None, None)
                answers.append(str((hit or {}).get("answer") or ""))
            was = ve.answer_matches(answers[0], case)
            now = ve.answer_matches(answers[1], case)
            before_ok += 1 if was else 0
            after_ok += 1 if now else 0
            if not was and now:
                rescued += 1
            elif was and not now:
                broken += 1
                print("  弄坏：%s | 前=%r 后=%r 真值=%r"
                      % (str(case["title"])[:40], answers[0], answers[1], case["truth"]))
        total = len(rows)
        print("补录前 %d/%d（%.1f%%） → 补录后 %d/%d（%.1f%%）；救回 %d 弄坏 %d"
              % (before_ok, total, before_ok / total * 100 if total else 0,
                 after_ok, total, after_ok / total * 100 if total else 0, rescued, broken))
        return 0
    finally:
        for bank in banks:
            try:
                bank.db.close()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = int(args[0]) if args else 8000
    cases, times = collect()
    imported = imported_ids()

    # 参与补录的判定取**保守口径**：补录是从「真值不唯一」的歧义骨架挑的，
    # 只要一条用例落在歧义骨架上，就算它「被这次补录摸过」，一律踢出 hold-out。
    # （按精确身份匹配只能对上 38 条；剩下 8 条变体同样参与了歧义判定，
    #   算进来才是真正的 out-of-sample。）
    # 只在判断题里算歧义骨架 —— 补录只写 judge 记录，别的题型没被动过。
    # （跨题型按 text_key 分组会虚增歧义：单选和判断共用一段文字并不矛盾。）
    groups: dict[str, set] = {}
    for _, case in cases.items():
        if case["type"] != "judge":
            continue
        groups.setdefault(text_key(case["title"]), set()).add(case["truth"][0])
    ambiguous = {k for k, v in groups.items() if len(v) > 1}

    holdout, in_sample = [], []
    exact_hit = 0
    for key, case in cases.items():
        identity = (case["type"], _norm_text(case["title"]),
                    tuple(sorted(str(o) for o in case["options"])))
        if identity in imported:
            exact_hit += 1
        if case["type"] == "judge" and text_key(case["title"]) in ambiguous:
            in_sample.append((key, case))
        else:
            holdout.append((key, case))

    print("全部用例 %d 条；补录记录 %d 条（精确身份对上的用例 %d 条）；"
          "歧义骨架 %d 个" % (len(cases), len(imported), exact_hit, len(ambiguous)))
    print("hold-out %d 条 / 参与补录判定的用例 %d 条\n" % (len(holdout), len(in_sample)))
    if "--dry" in sys.argv:
        return 0
    if "--bank-diff" in sys.argv:
        return bank_diff(holdout)

    def run(rows, label):
        ok = bad = err = 0
        by_type: dict[str, list[int]] = {}
        once = [0, 0]
        repeat = [0, 0]
        for key, case in rows:
            answer, source = ve.query(port, case)
            if answer is None:
                err += 1
                continue
            good = ve.answer_matches(answer, case)
            slot = by_type.setdefault(case["type"], [0, 0])
            slot[0 if good else 1] += 1
            bucket = once if times.get(key, 1) <= 1 else repeat
            bucket[0 if good else 1] += 1
            ok, bad = (ok + 1, bad) if good else (ok, bad + 1)
        total = ok + bad
        print("%s：%d 题，对 %d 错 %d 异常 %d（准确率 %.1f%%）"
              % (label, total, ok, bad, err, (ok / total * 100) if total else 0))
        for qtype in sorted(by_type):
            hit, miss = by_type[qtype]
            print("  %-11s 用例 %3d 对 %3d 错 %3d" % (qtype, hit + miss, hit, miss))
        print("  只出现一次的题：%d 题 对 %d（%.1f%%）"
              % (once[0] + once[1], once[0],
                 (once[0] / (once[0] + once[1]) * 100) if (once[0] + once[1]) else 0))
        print("  跨快照重复的题：%d 题 对 %d（%.1f%%）"
              % (repeat[0] + repeat[1], repeat[0],
                 (repeat[0] / (repeat[0] + repeat[1]) * 100) if (repeat[0] + repeat[1]) else 0))
        return total, ok, bad

    run(holdout, "hold-out")
    print()
    run(in_sample, "补录用例（in-sample，仅供参考）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

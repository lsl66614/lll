"""模拟：给「同骨架多题」那批无图记录补上带图版本，能救回多少判断题。

思路（不改任何匹配规则）：
  ``find_official`` 里是 ``if not hits: hits = truncated_hits`` ——
  **精确命中优先于空括号兜底**。所以只要库里存在一条题干与请求**完全一致**
  （图片 URL 都在）的记录，它就会进 ``hits``，把靠骨架猜的那条压下去。

  这批带图记录可以直接从快照重建：快照里有完整题干（含图片 URL）和平台真值。

模拟做法：
  1. 把 ``data/*.db`` 复制到临时目录（**绝不写真实题库**）；
  2. 基线：逐条打 ``Bank.match`` 拿答案，与快照真值比；
  3. 给 12 个「真值不唯一」的骨架，每个变体造一条 official+adopted 记录；
  4. 再跑一遍，比较救回 / 弄坏。

用法：
    .venv\\Scripts\\python.exe scripts/simulate_image_restore.py
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
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import bank as bank_mod  # noqa: E402
from app import config  # noqa: E402
from app.formula_index import ImageTextStore  # noqa: E402
from app.shape_key import image_urls, text_key  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "ve", os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_e2e_accuracy.py"))
ve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ve)


def answer_of(bank, case) -> str:
    """只取题库匹配结果。

    ⚠️ 这里**没有** AI / 缓存兜底，所以「对 X 错 Y」的绝对值**不能**和端到端
    准确率直接比。这个脚本只用来看**增量**：补图救回几条、弄坏几条。
    """
    hit = bank.match("judge", case["title"], case["options"], None, None)
    return str((hit or {}).get("answer") or "")


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="bank-sim-")
    bank = None
    try:
        # 一致副本：WAL 模式下只拷主文件会读到过期数据，必须走 backup API
        src = sqlite3.connect(
            "file:%s?mode=ro" % os.path.abspath(config.BANK_DB_PATH).replace("\\", "/"), uri=True)
        dst = sqlite3.connect(os.path.join(tmpdir, "sim.db"))
        src.backup(dst)
        src.close()
        dst.close()

        bank = bank_mod.Bank(os.path.join(tmpdir, "bank.json"), os.path.join(tmpdir, "sim.db"))
        store = ImageTextStore(os.path.join(config.DATA_DIR, "image_text.json"))
        bank.set_image_text(store.mapping())
        print("临时库 %s（真实题库未触碰）" % os.path.join(tmpdir, "sim.db"))

        cases = [c for c in ve.build_cases() if c["type"] == "judge"]
        groups: dict[str, set[str]] = collections.defaultdict(set)
        for c in cases:
            groups[text_key(c["title"])].add(c["truth"][0])
        ambiguous = {k for k, v in groups.items() if len(v) > 1}
        print("判断题用例 %d 条；歧义骨架 %d 个；落在上面的用例 %d 条\n"
              % (len(cases), len(ambiguous), sum(1 for c in cases if text_key(c["title"]) in ambiguous)))

        before = {id(c): answer_of(bank, c) for c in cases}
        ok_before = sum(1 for c in cases if ve.answer_matches(before[id(c)], c))

        # 补图：每个歧义骨架的每个变体造一条带完整题干（含图片 URL）的官方记录
        added = 0
        for c in cases:
            if text_key(c["title"]) not in ambiguous:
                continue
            added += 1
            bank._items.append({
                "id": "sim-%d" % added,
                "type": "judge",
                "title": c["title"],
                "options": list(c["options"]),
                "answer": c["truth"][0],
                "source": "official",
                "status": "adopted",
                "subject": bank_mod.DEFAULT_SUBJECT,
                "image_hash": "",
                "level": 4,
                "via": "sim-restore",
                "history": [],
                "wrong_answers": [],
                "created": 9_999_999_999.0 + added,
                "conflict": False,
            })
        bank._index_dirty = True
        bank._canon_index = None
        print("补入带图记录 %d 条\n" % added)

        ok_after = 0
        rescued, broken = [], []
        for c in cases:
            after = answer_of(bank, c)
            good = ve.answer_matches(after, c)
            if good:
                ok_after += 1
            was = ve.answer_matches(before[id(c)], c)
            if not was and good:
                rescued.append((c, before[id(c)], after))
            elif was and not good:
                broken.append((c, before[id(c)], after))

        n = len(cases)
        print("基线  ：题库直接命中 %d 条（其余无答案，线上会掉 AI 兜底）" % ok_before)
        print("补图后：题库直接命中 %d 条" % ok_after)
        print("救回 %d 条   弄坏 %d 条" % (len(rescued), len(broken)))
        # 空骨架（题干只有图、一个字都没有）和「若则」这类短骨架性质不同，分开数
        empty = sum(1 for c, _, _ in rescued if not text_key(c["title"]))
        print("  其中空骨架（题干只有图）%d 条，有文字的歧义骨架 %d 条\n"
              % (empty, len(rescued) - empty))

        for c, old, new in rescued:
            print("  救回  %-34s 原 %r → %r  真值 %r"
                  % (str(c["title"])[:34], old, new, c["truth"][0]))
        for c, old, new in broken:
            print("  弄坏  %-34s 原 %r → %r  真值 %r"
                  % (str(c["title"])[:34], old, new, c["truth"][0]))
        return 0
    finally:
        # Windows 下文件被打开就删不掉：必须先关掉 Bank 的 sqlite 连接，
        # 否则 rmtree 会静默失败（ignore_errors=True），临时库越堆越多。
        if bank is not None and getattr(bank, "db", None) is not None:
            try:
                bank.db.close()
            except Exception:  # noqa: BLE001 - 清理失败不该盖住模拟结果
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)
        if os.path.isdir(tmpdir):
            print("⚠ 临时库未能删除：%s" % tmpdir)


if __name__ == "__main__":
    raise SystemExit(main())

"""拆解判断题错误里「source=official」的那些题：find_official 到底命中了哪条记录、走的哪个分支。

只读：先把题库用 sqlite3 backup API 复制到临时文件（WAL 模式下直接 cp 会读到旧数据），
再用 Bank 加载副本复算匹配逻辑。用法：... diagnose_judge_official_hits.py [端口]
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402
from app.bank import (  # noqa: E402
    Bank, _image_compatible, _norm_text, _options_broken,
)
from app.shape_key import same_question_truncated  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "ve", os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_e2e_accuracy.py"))
ve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ve)


def backup_bank() -> str:
    """用 sqlite3 backup API 复制主库 + WAL 内容，避免只读主文件读到旧数据。"""
    src = sqlite3.connect(config.BANK_DB_PATH)
    dst_path = os.path.join(tempfile.mkdtemp(prefix="bankcopy-"), "bank.db")
    dst = sqlite3.connect(dst_path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return dst_path


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8010
    db_copy = backup_bank()
    # Bank(path, db_path)：第二个参数才是 SQLite 库。只传一个会被当成 JSON 快照路径，
    # 离线题库变空、什么匹配都复现不出来（2026-11-10 踩过）。
    bank = Bank(os.path.join(os.path.dirname(db_copy), "bank.json"), db_copy)
    # 线上实例启动时会 set_image_text（公式图 OCR 映射），离线复算必须一致，
    # 否则 canon 兜底会漏掉一批本该命中的记录。
    from app.formula_index import ImageTextStore
    store = ImageTextStore(os.path.join(config.DATA_DIR, "image_text.json"))
    bank.set_image_text(store.mapping())
    print("image_text 条数:", store.count())

    cases = [c for c in ve.build_cases() if c["type"] == "judge"]
    official_fails = []
    for case in cases:
        answer, source = ve.query(port, case)
        if answer is None or ve.answer_matches(answer, case):
            continue
        if source == "official":
            official_fails.append((case, answer))

    print("判断题错误且 source=official：%d 条" % len(official_fails))
    print()
    for case, answer in official_fails:
        title = case["title"]
        options = case["options"]
        target_title = _norm_text(title)
        target_options = bank._norm_options_key(options, "judge")
        print("━" * 78)
        print("题干:", title[:90])
        print("真值: %r   网关: %r" % (case["truth"], answer))
        hits = []
        for item in bank._items:
            if item["type"] != "judge" or item.get("source") != "official":
                continue
            if item.get("status") != "adopted":
                continue
            if not _image_compatible(item, ""):
                continue
            if bank._norm_options_key(item["options"], "judge") != target_options:
                continue
            if _options_broken(item["options"], "judge"):
                continue
            exact = _norm_text(item["title"]) == target_title
            trunc = same_question_truncated(title, item["title"])
            if exact or trunc:
                hits.append((item, exact, trunc))
        if not hits:
            print("   find_official 主循环无命中 → 走 _find_by_canon（图/LaTeX 规范化）")
            canon = bank._find_by_canon("judge", title, options, official_only=True)
            if canon is None:
                print("      canon 兜底也未命中（答案可能来自其它路径）")
            else:
                from app.bank import canon_text
                print("      canon 命中 id=%s answer=%r" % (str(canon["id"])[:8], canon.get("answer")))
                print("      库存题干:", str(canon["title"])[:110])
                same_key = canon_text(title, bank._image_text) == canon_text(
                    str(canon["title"]), bank._image_text)
                print("      双方 canon 键相同:", same_key,
                      "| 请求 canon 前 60:", canon_text(title, bank._image_text)[:60])
                print("      库存 canon 前 60:", canon_text(str(canon["title"]), bank._image_text)[:60])
        for item, exact, trunc in hits:
            branch = "exact" if exact else "truncated"
            print("   命中[%s] id=%s answer=%r" % (branch, str(item["id"])[:8], item.get("answer")))
            print("      库存题干:", str(item["title"])[:110])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

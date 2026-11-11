"""判定：判断题里 11 条 `official-truncated` 到底是不是同一道题。

两种可能决定修法完全不同：
  A. 是同一道题（题干只差图片表示）→ **库存的官方答案是错的**，要修数据。
  B. 不是同一道题 → **匹配器认错了**，要修 find_official / same_question_truncated。

逐条把判定依据打出来：完整请求题干、命中记录题干、两边的 has_image、
文字骨架、骨架是否相等、same_question_truncated 的返回值、canon 是否相等。

只读。用法：
    .venv\\Scripts\\python.exe scripts/diagnose_judge_truncated.py [端口]
"""
from __future__ import annotations

import importlib.util
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import bank as bank_mod  # noqa: E402
from app import config  # noqa: E402
from app.formula_index import ImageTextStore  # noqa: E402
from app.shape_key import has_image, image_urls, same_question_truncated, text_key  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "ve", os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_e2e_accuracy.py"))
ve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ve)


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    bank = bank_mod.Bank(config.BANK_PATH, config.BANK_DB_PATH)
    # 服务端启动时会灌 OCR 索引；不灌的话 _find_by_canon 的行为和线上不一致
    store = ImageTextStore(os.path.join(config.DATA_DIR, "image_text.json"))
    bank.set_image_text(store.mapping())
    print("OCR 索引 %d 条\n" % len(store.mapping()))

    same = diff = 0
    for case in ve.build_cases():
        if case["type"] != "judge":
            continue
        answer, source = ve.query(port, case)
        if answer is None or ve.answer_matches(answer, case):
            continue
        hit = bank.match("judge", case["title"], case["options"], None, None)
        if hit is None or hit.get("_match") != "official-truncated":
            continue

        req, stored = case["title"], str(hit.get("title") or "")
        req_key, stored_key = text_key(req), text_key(stored)
        tr = same_question_truncated(req, stored)
        can_req = bank_mod.canon_text(req, bank._image_text)
        can_stored = bank_mod.canon_text(stored, bank._image_text)

        # 同题判据：骨架相等（图被压成空括号那一支）；或 canon 相等（OCR 还原后相等）
        is_same = (req_key == stored_key) or (can_req == can_stored and bool(can_req))
        if is_same:
            same += 1
            verdict = "同一道题 → 库存官方答案是错的"
        else:
            diff += 1
            verdict = "不是同一道题 → 匹配器认错了"

        print("=" * 72)
        print("请求题干 : %r" % req)
        print("命中记录 : %r  (id=%s source=%s status=%s answer=%r)"
              % (stored, hit.get("id"), hit.get("source"), hit.get("status"), hit.get("answer")))
        print("网关给   : %r   快照真值 %r" % (answer, case["truth"][0]))
        print("has_image: 请求=%s 记录=%s   图数: 请求=%d 记录=%d"
              % (has_image(req), has_image(stored), len(image_urls(req)), len(image_urls(stored))))
        print("文字骨架 : 请求=%r" % req_key)
        print("           记录=%r" % stored_key)
        print("骨架相等 : %s   same_question_truncated=%s   canon相等=%s"
              % (req_key == stored_key, tr, can_req == can_stored))
        print("canon    : 请求=%r" % can_req)
        print("           记录=%r" % can_stored)
        print("判定     : %s" % verdict)

    print("=" * 72)
    print("同题(数据错) %d 条   不同题(匹配错) %d 条" % (same, diff))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

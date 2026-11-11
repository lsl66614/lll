"""复现：status=pending 的 official 记录冒充官方答案。

两条路径（都不涉及 AI 调用，纯本地）：
  A. find_official 的 canon 兜底 ``_find_by_canon(official_only=True)``
     只过滤 source，不过滤 status → pending official 被当官方答案直接返回。
  B. match() 的全表扫描用 ``_source_level(source, status)`` 排序，
     该函数对 official 无条件返回 4 → pending official 压过 adopted 的校准/AI 答案，
     于是「已采纳的好答案」被挤掉，请求转去重新问 AI（问不到就报错）。

修好之前每条都应打印 BUG；修好后打印 OK。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bank import Bank, _source_level  # noqa: E402

failures = 0


def check(name: str, ok: bool, detail: str) -> None:
    global failures
    if not ok:
        failures += 1
    print("%-4s %-42s %s" % ("OK" if ok else "BUG", name, detail))


def main() -> int:
    # ---- 路径 B：match() 排序把 pending official 排到 adopted 前面 ----
    with tempfile.TemporaryDirectory() as tmp:
        bank = Bank(os.path.join(tmp, "bank.json"))
        pending_official = bank.add({
            "type": "judge", "title": "同一道判断题", "options": ["对", "错"],
            "answer": "正确", "subject": "默认",
            "source": "official", "status": "pending",
        })
        adopted_ai = bank.add({
            "type": "judge", "title": "同一道判断题", "options": ["对", "错"],
            "answer": "错误", "subject": "默认",
            "source": "calibrated", "status": "adopted",
        })
        hit = bank.match("judge", "同一道判断题", ["对", "错"], "默认")
        picked = hit["id"] if hit else ""
        check(
            "B match 选中 adopted 而非 pending official",
            picked == adopted_ai["id"],
            "选中=%s (%s/%s/%s)" % (
                "pending-official" if picked == pending_official["id"]
                else "adopted-calibrated" if picked == adopted_ai["id"] else repr(picked),
                hit.get("source") if hit else "-",
                hit.get("status") if hit else "-",
                hit.get("answer") if hit else "-",
            ),
        )
        check(
            "B2 _source_level(official,pending) < 4",
            _source_level("official", "pending") < 4,
            "level=%d" % _source_level("official", "pending"),
        )
        check(
            "B3 _source_level(official,adopted) == 4",
            _source_level("official", "adopted") == 4,
            "level=%d" % _source_level("official", "adopted"),
        )
    # 题干略有差异 → _fast_lookup 哈希未命中，走全表扫描的相似度分支
    with tempfile.TemporaryDirectory() as tmp:
        bank = Bank(os.path.join(tmp, "bank.json"))
        stored = "判断下列说法是否正确并说明理由这是一道测试题"
        asked = stored + "啊"
        bank.add({
            "type": "judge", "title": stored, "options": ["对", "错"],
            "answer": "正确", "subject": "默认",
            "source": "official", "status": "pending",
        })
        adopted_ai = bank.add({
            "type": "judge", "title": stored, "options": ["对", "错"],
            "answer": "错误", "subject": "默认",
            "source": "calibrated", "status": "adopted",
        })
        hit = bank.match("judge", asked, ["对", "错"], "默认")
        check(
            "B4 相似匹配同样不选 pending official",
            hit is not None and hit["id"] == adopted_ai["id"],
            "选中=%s/%s" % (hit.get("source") if hit else None, hit.get("status") if hit else None),
        )

    # ---- 路径 A：find_official 的 canon 兜底返回 pending official ----
    # 两个题干 _norm_text 不同、canon_text 相同，只能靠 canon 兜底命中。
    with tempfile.TemporaryDirectory() as tmp:
        bank = Bank(os.path.join(tmp, "bank.json"))
        latex_title = r"设 $\frac{1}{2}$ 等于"
        plain_title = "设 1/2 等于"
        bank.add({
            "type": "judge", "title": latex_title, "options": ["对", "错"],
            "answer": "正确", "subject": "默认",
            "source": "official", "status": "pending",
        })
        bank.add({
            "type": "judge", "title": plain_title, "options": ["对", "错"],
            "answer": "错误", "subject": "默认",
            "source": "ai", "status": "adopted",
        })
        # canon 索引只收 adopted（_ensure_canon_index 已过滤），pending 进不去。
        canon_hit = bank._find_by_canon("judge", plain_title, ["对", "错"], official_only=True)
        check(
            "A0 canon 索引不含 pending official",
            canon_hit is None,
            "命中=%s" % (canon_hit.get("status") if canon_hit else None),
        )
        hit = bank.find_official("judge", plain_title, ["对", "错"])
        check(
            "A find_official 不返回 pending official",
            not hit or hit.get("status") == "adopted",
            "返回=%s/%s/%s" % (
                hit.get("source") if hit else None,
                hit.get("status") if hit else None,
                hit.get("answer") if hit else None,
            ),
        )

    # ---- 路径 C：find_by_position 用 pending 记录抢走 adopted ----
    with tempfile.TemporaryDirectory() as tmp:
        bank = Bank(os.path.join(tmp, "bank.json"))
        title = "选项是图片的题"
        # 先加 adopted，再加 pending official —— 模拟「老答案被降级」的真实顺序：
        # 现有排序只看 created，降级后的新记录会反超老记录。
        adopted = bank.add({
            "type": "single", "title": title,
            "options": ["http://x/a.png", "http://x/b.png"],
            "answer": "B", "subject": "默认",
            "source": "manual", "status": "adopted",
        })
        bank.add({
            "type": "single", "title": title,
            "options": ["http://x/a.png", "http://x/b.png"],
            "answer": "A", "subject": "默认",
            "source": "official", "status": "pending",
        })
        hit = bank.find_by_position("single", title, ["A", "B"], "默认")
        picked = hit["id"] if hit else ""
        check(
            "C find_by_position 选中 adopted",
            picked == adopted["id"],
            "选中=%s/%s" % (hit.get("source") if hit else None, hit.get("status") if hit else None),
        )

    print()
    print("失败项：%d" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

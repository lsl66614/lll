"""只读排查：判断题的错误答案到底来自哪条题库记录。

背景：verify_e2e_accuracy.py 报出「judge 错题来源 official=13」，
而 docs/ocs-upgrade-plan.md 写着「那 19 条没有可用的官方记录」。
两者直接冲突，必须查清楚是谁错，再决定改哪一边。

只打印，不改库。用法：
    .venv\\Scripts\\python.exe scripts/inspect_judge_errors.py [端口]
"""
from __future__ import annotations

import importlib.util
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import bank as bank_mod  # noqa: E402
from app import config  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "ve", os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_e2e_accuracy.py"))
ve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ve)


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    bank = bank_mod.Bank(config.BANK_PATH, config.BANK_DB_PATH)

    cases = [c for c in ve.build_cases() if c["type"] == "judge"]
    print("判断题用例 %d 条\n" % len(cases))

    n_ok = n_bad = 0
    for case in cases:
        answer, source = ve.query(port, case)
        if answer is None:
            continue
        if ve.answer_matches(answer, case):
            n_ok += 1
            continue
        n_bad += 1
        truth = case["truth"][0]
        # 复刻服务端的调用：验证脚本不传 subject、不传 image_hash
        hit = bank.match("judge", case["title"], case["options"], None, None)
        print("错 %2d  %s" % (n_bad, case["title"][:56]))
        print("    网关给 %-6r 真值 %r  来源 %s" % (answer, truth, source or "(空)"))
        if hit is None:
            print("    bank.match → None（真没记录）")
        else:
            print("    bank.match → id=%s source=%s status=%s answer=%r match=%s score=%s"
                  % (hit.get("id"), hit.get("source"), hit.get("status"),
                     str(hit.get("answer"))[:20], hit.get("_match"), hit.get("_score")))
            print("    记录题干 %s" % str(hit.get("title"))[:70])
            print("    记录选项 %r" % (hit.get("options"),))
        print()
    print("合计：对 %d  错 %d" % (n_ok, n_bad))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

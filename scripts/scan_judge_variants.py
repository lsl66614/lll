"""同一段题干的判断题，在快照里到底有几种真值？

用途：判定「库存里那条 official 答案是不是真的错」。
如果某个文字骨架在快照里对应多种真值（题干文字一样、公式图不同 → 是**不同的题**），
那么库里那条没有图的记录本来就不可能对每一道都对 —— 属数据缺口，不是答反。
如果某个文字骨架只有一种真值，那库里给相反答案就是**真的答反了**。

只读快照，不打网关。用法：
    .venv\\Scripts\\python.exe scripts/scan_judge_variants.py [--list]
"""
from __future__ import annotations

import collections
import importlib.util
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.shape_key import image_urls, text_key  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "ve", os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_e2e_accuracy.py"))
ve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ve)


def main() -> int:
    cases = [c for c in ve.build_cases() if c["type"] == "judge"]
    print("判断题用例 %d 条\n" % len(cases))

    # 骨架 → {(图片指纹, 真值)}
    groups: dict[str, set[tuple[tuple[str, ...], str]]] = collections.defaultdict(set)
    for c in cases:
        key = text_key(c["title"])
        fp = tuple(u.rsplit("/", 1)[-1] for u in image_urls(c["title"]))
        groups[key].add((fp, c["truth"][0]))

    multi_truth = {k: v for k, v in groups.items() if len({t for _, t in v}) > 1}
    single_truth = {k: v for k, v in groups.items() if len({t for _, t in v}) == 1}

    print("骨架总数 %d" % len(groups))
    print("  真值唯一   %d 个骨架" % len(single_truth))
    print("  真值不唯一 %d 个骨架（同一段文字对应多道不同的题）" % len(multi_truth))
    print()

    if "--list" in sys.argv:
        for key, variants in sorted(multi_truth.items(), key=lambda kv: -len(kv[1])):
            print("骨架 %r —— %d 种变体" % (key[:40], len(variants)))
            for fp, truth in sorted(variants):
                print("    真值 %-3s  图 %s" % (truth, ",".join(f[:12] for f in fp) or "(无图)"))
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

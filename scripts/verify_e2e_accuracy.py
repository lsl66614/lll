"""端到端准确率复核 CLI（按「题干 + 选项集」去重，选项与真值成对取用）。

评估口径全部在 ``app/evaluation.py``（快照用例收集 / 答案比对 / 官方候选），
本脚本只保留 HTTP 查询与结果打印；其它脚本仍按 ``ve.answer_matches`` 等旧名
引用，因此这里把内核函数原样再导出。

用法：
    .venv\\Scripts\\python.exe scripts/verify_e2e_accuracy.py [端口]
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.evaluation import (  # noqa: E402,F401  （再导出供旧脚本使用）
    L,
    answer_matches,
    as_number,
    build_cases,
    judge_completion,
    norm_option,
    norm_text,
    snapshot_fingerprint,
)


def query(port: int, case: dict) -> tuple[str | None, str]:
    payload = json.dumps({
        "title": case["title"], "type": case["type"], "options": case["options"],
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d/query" % port, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return None, "ERR:%s" % str(exc)[:40]
    data = body.get("data") or {}
    return str(data.get("answer") or ""), str(data.get("source") or "")


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    cases = build_cases()
    print("快照指纹 %s  用例数 %d" % (snapshot_fingerprint(), len(cases)))

    ok = bad = err = 0
    # 放行档要分开数：numeric 可以理直气壮（1/12 = 0.08），
    # prefix 是宽松档（随机 vs 随机事件），混在一起会让人以为全是数值等价。
    loose: dict[str, int] = {}
    wrong = []
    by_type: dict[str, list[int]] = {}
    # 错题按 (题型, 答案来源) 分布：哪一路的数据在拖后腿。
    # 必须分题型 —— 全量混在一起时 official 的 22 条里有一半其实是判断题，会误导。
    by_source: dict[tuple[str, str], int] = {}
    for case in cases:
        answer, source = query(port, case)
        if answer is None:
            err += 1
            continue
        # 分类计数必须在分支**外面**累加：放进 else 会变成「只统计错题、且错题算两遍」
        # （2026-11-10 实测到的 bug —— judge 434 例被报成 38 例，正好是 19 个错的 ×2）。
        slot = by_type.setdefault(case["type"], [0, 0])
        if answer_matches(answer, case):
            ok += 1
            slot[0] += 1
            if case["type"] == "completion":
                reason = judge_completion(answer, case["truth"][0])[1]
                if reason != "exact":
                    loose[reason] = loose.get(reason, 0) + 1
        else:
            bad += 1
            slot[1] += 1
            tag = source or "(空)"
            key = (case["type"], tag)
            by_source[key] = by_source.get(key, 0) + 1
            if len(wrong) < 10:
                wrong.append((case["title"][:40], answer, case["truth"], source))

    total = ok + bad
    print("端口 %d：%d 题，对 %d 错 %d 异常 %d（准确率 %.1f%%）"
          % (port, total, ok, bad, err, (ok / total * 100) if total else 0))
    for reason in sorted(loose):
        print("  其中填空题靠 %s 放行 %d 条" % (reason, loose[reason]))
    for qtype in sorted(by_type):
        hit, miss = by_type[qtype]
        print("  %-11s 用例 %3d  对 %3d  错 %3d" % (qtype, hit + miss, hit, miss))
    for qtype in sorted({k[0] for k in by_source}):
        pairs = [(s, n) for (t, s), n in by_source.items() if t == qtype]
        pairs.sort(key=lambda kv: -kv[1])
        print("  %-11s 错题来源: " % qtype + "  ".join("%s=%d" % kv for kv in pairs))
    for w in wrong:
        print("    错: %s | 网关给 %s / 应为 %s (%s)" % w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

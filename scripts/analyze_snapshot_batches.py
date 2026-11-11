"""盘点快照批次：哪些文件是同一场测验、落在哪天、题目重叠多少。

为 hold-out 验证服务：需要把「参与补录的批次」与「没参与过的批次」分开。
只读，不改任何数据。

用法：
    .venv\\Scripts\\python.exe scripts/analyze_snapshot_batches.py
"""
from __future__ import annotations

import collections
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402
from app.mooc_result import parse_result_html  # noqa: E402
from app.shape_key import image_urls, text_key  # noqa: E402


def batch_of(fname: str, meta: dict) -> str:
    """批次 = 日期 + 测验 id（取不到就用日期）。"""
    day = fname[:8]
    url = str(meta.get("url") or "")
    match = re.search(r"[?&]id=(\d+)", url)
    return "%s/%s" % (day, match.group(1) if match else "other")


def case_key(item: dict) -> tuple:
    return (item.get("type"), text_key(item.get("title")), tuple(image_urls(item.get("title"))))


def main() -> int:
    files = sorted(f for f in os.listdir(config.SNAPSHOT_DIR) if f.endswith(".html"))
    batches: dict[str, list[str]] = collections.defaultdict(list)
    batch_cases: dict[str, set] = collections.defaultdict(set)
    no_meta = 0
    for fname in files:
        meta_path = os.path.join(config.SNAPSHOT_DIR, fname + ".json")
        meta = {}
        if os.path.exists(meta_path):
            try:
                meta = json.load(open(meta_path, encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                no_meta += 1
        else:
            no_meta += 1
        batch = batch_of(fname, meta)
        batches[batch].append(fname)
        html = open(os.path.join(config.SNAPSHOT_DIR, fname), encoding="utf-8", errors="replace").read()
        if "u-questionItem" not in html:
            continue
        for item in parse_result_html(html):
            if item.get("title"):
                batch_cases[batch].add(case_key(item))

    all_cases: set = set()
    for cases in batch_cases.values():
        all_cases |= cases
    print("快照文件 %d 个；无 sidecar %d；批次 %d 个；去重题目 %d 条\n"
          % (len(files), no_meta, len(batches), len(all_cases)))

    # 按时间顺序看「新题 / 旧题」：hold-out 能不能成立，取决于题目是否跨批次重复
    ordered = sorted(batches, key=lambda b: min(batches[b]))
    seen: set = set()
    print("%-22s %5s %5s %5s %5s" % ("批次", "文件", "题目", "新题", "旧题"))
    total_new = total_seen = 0
    for batch in ordered:
        cases = batch_cases.get(batch, set())
        new = cases - seen
        old = cases & seen
        total_new += len(new)
        total_seen += len(old)
        print("%-22s %5d %5d %5d %5d" % (batch, len(batches[batch]), len(cases), len(new), len(old)))
        seen |= cases
    print("\n累计：新题 %d，重复题 %d" % (total_new, total_seen))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

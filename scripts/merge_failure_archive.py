"""把从自动备份包里抢救出来的失败记录合并回 data/failures.json。

背景（2026-11-10 实测）：
``data/backups/auto-*.zip`` 里有一批「自动投票题」测试包，是测试进程写进生产目录的。
清掉它们之前，先把它们里的 ``failures.json`` 抢救出来——那些包里含有线上
``data/failures.json`` 已经没有的失败记录（滚动窗口 200 条会挤掉旧的）。

抢救规则（scripts/rebuild_failure_titles.py 的同款判据）：
  - 只保留「题干在快照里能对上」的记录，和不含测试标记的记录；
  - 题干恰好 300 字（老版截断特征）的，用快照反向重建完整题干；
  - 测试夹具（「自检」「提速验证」「自动投票」等）且快照里没有的，丢弃。

用法：
    .venv\\Scripts\\python.exe scripts/merge_failure_archive.py          # 只看清单
    .venv\\Scripts\\python.exe scripts/merge_failure_archive.py --apply  # 落库

落库前自动备份 data/failures.json。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
import time
import zipfile
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import config  # noqa: E402
from app.bank import _norm_title_for_dedupe  # noqa: E402
from app.mooc_result import parse_result_html  # noqa: E402

# 测试夹具的题干特征：这些且快照里没有的记录直接丢
TEST_MARKERS = ("自检", "测试", "验证", "提速", "复用", "不在题库", "普通文字题", "长选项", "自动投票")
# 失败记录日志的条数上限（与 app/failures.py 的 limit 一致）
LIMIT = 200


def snapshot_titles() -> set[str]:
    """快照里出现过的所有题干（归一化后）。"""
    titles: set[str] = set()
    snap_dir = config.SNAPSHOT_DIR
    if not os.path.isdir(snap_dir):
        return titles
    for path in sorted(glob.glob(os.path.join(snap_dir, "*.html"))):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fp:
                html = fp.read()
        except OSError:
            continue
        if "u-questionItem" not in html:
            continue
        try:
            items = parse_result_html(html)
        except Exception:  # noqa: BLE001 - 单个快照坏掉不影响整体
            continue
        for item in items:
            title = str(item.get("title") or "")
            if title:
                titles.add(_norm_title_for_dedupe(title))
    return titles


def collect_from_backups() -> list[dict[str, Any]]:
    """从自动备份包里捞出线上还没有的失败记录。"""
    live_path = config.FAILURES_PATH
    live_at: set[float] = set()
    if os.path.exists(live_path):
        try:
            with open(live_path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            items = data.get("items") if isinstance(data, dict) else data
            live_at = {i.get("at") for i in items or [] if isinstance(i, dict)}
        except (OSError, json.JSONDecodeError, ValueError):
            live_at = set()

    found: dict[float, dict[str, Any]] = {}
    backup_dir = os.path.join(os.path.dirname(os.path.abspath(live_path)), "backups")
    for path in sorted(glob.glob(os.path.join(backup_dir, "auto-*.zip"))):
        try:
            with zipfile.ZipFile(path) as zf:
                if "failures.json" not in zf.namelist():
                    continue
                with zf.open("failures.json") as fp:
                    payload = json.loads(fp.read().decode("utf-8"))
        except (OSError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError):
            continue
        items = payload.get("items") if isinstance(payload, dict) else payload
        for item in items or []:
            if not isinstance(item, dict):
                continue
            at = item.get("at")
            if at is None or at in live_at or at in found:
                continue
            found[at] = item
    return list(found.values())


def is_keep(item: dict[str, Any], snapshot: set[str]) -> bool:
    """测试夹具且快照里没有 → 丢；其余留。"""
    title = str(item.get("title") or "")
    if any(marker in title for marker in TEST_MARKERS):
        return _norm_title_for_dedupe(title) in snapshot
    return True


def rebuild_truncated(items: list[dict[str, Any]], snapshot: set[str]) -> tuple[int, list[dict[str, Any]]]:
    """把题干恰好 300 字（老版截断）的记录用快照反向重建。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "rebuild_failure_titles", os.path.join(ROOT, "scripts", "rebuild_failure_titles.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    index, pages = module.build_index()
    ordered = sorted(items, key=lambda x: float(x.get("at") or 0.0))
    prev_norm = ""
    fixed = 0
    misses: list[dict[str, Any]] = []
    for item in ordered:
        title = str(item.get("title") or "")
        if len(title) != 300:
            prev_norm = module._norm_text(title)
            continue
        full, why = module.find_full_title(title, index, None, prev_norm, pages)
        if full:
            item["title"] = full
            item["title_rebuilt"] = True
            fixed += 1
            prev_norm = module._norm_text(full)
        else:
            misses.append({"title": title[:60], "reason": why})
            prev_norm = module._norm_text(title)
    return fixed, misses


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正写文件（默认只打印清单）")
    args = parser.parse_args()

    snapshot = snapshot_titles()
    raw = collect_from_backups()
    keep = [i for i in raw if is_keep(i, snapshot)]
    dropped = len(raw) - len(keep)
    fixed, misses = rebuild_truncated(keep, snapshot)

    print(f"快照题干 {len(snapshot)} 条")
    print(f"备份包里线上没有的失败记录 {len(raw)} 条 → 保留 {len(keep)} 条，丢弃测试夹具 {dropped} 条")
    print(f"其中题干 300 字（老版截断）重建 {fixed} 条，仍无法确定 {len(misses)} 条")

    if not args.apply:
        print("\n（dry-run，加 --apply 落库）")
        return 0

    live_path = config.FAILURES_PATH
    with open(live_path, "r", encoding="utf-8") as fp:
        payload = json.load(fp)
    live_items = payload.get("items") if isinstance(payload, dict) else payload
    live_items = [i for i in live_items or [] if isinstance(i, dict)]

    seen: set[float] = set()
    merged: list[dict[str, Any]] = []
    for item in sorted(live_items + keep, key=lambda x: float(x.get("at") or 0), reverse=True):
        at = item.get("at")
        if at in seen:
            continue
        seen.add(at)
        merged.append(item)
    trimmed = merged[:LIMIT]
    dropped_by_limit = len(merged) - len(trimmed)

    backup = f"{live_path}.bak-merge-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(live_path, backup)
    with open(live_path, "w", encoding="utf-8") as fp:
        json.dump({"items": trimmed}, fp, ensure_ascii=False, indent=2)
    print(f"已备份 → {os.path.basename(backup)}")
    print(f"合并后 {len(trimmed)} 条（上限 {LIMIT}，因上限丢掉最旧的 {dropped_by_limit} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""用结果页快照反向重建失败记录里被截断的题干。

背景：`_record_failure` 早期把题干截到 300 字，而图片题的题干是一串图片 URL
拼出来的，常常有 300~480 字。截断后：
  - 失败记录里的题干与题库里的官方题对不上，回放核对全部落空；
  - 同一道题的失败记录互相之间也对不上，无法按题干聚合。

本脚本从 `data/snapshots/` 的结果页 HTML 里重建完整题干，填回
`data/failures.json` 里那些**恰好 300 字**的记录（正好是老版截断的特征）。

匹配办法（由严到松，全部基于归一化题干的前缀关系）：
  1. 归一化后取前 180/200/240/260/280/300 字做前缀匹配快照里的题干；
  2. 要求候选唯一；
  3. 校验：截断题干的归一化结果必须是完整题干归一化结果的**前缀**——
     宁可漏配，绝不错配（错配会把一道题的题干安到另一道题上）。

候选不唯一时的消歧：两道题的前缀完全一样、只有**最后一张图片的 URL** 不同，
这是同一道题在不同批次里换了图。此时按题库里有没有对应的官方条目来定：
题干里出现的最后一张图片 URL 在题库中对应哪条官方题，就用哪条候选。

用法：
    .venv\\Scripts\\python.exe scripts/rebuild_failure_titles.py          # 只看清单
    .venv\\Scripts\\python.exe scripts/rebuild_failure_titles.py --apply  # 落库

落库前会自动备份 data/failures.json。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sys
import time
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import config  # noqa: E402
from app.bank import Bank, _norm_text  # noqa: E402
from app.mooc_result import parse_result_html  # noqa: E402

# 老版 _record_failure 的截断长度
LEGACY_TRUNCATED = 300
# 前缀匹配用到的长度梯度（越长越严）
PREFIX_STEPS = (180, 200, 240, 260, 280, 300)
_URL_RE = re.compile(r"https?://\S+")


def build_index() -> tuple[dict[str, str], dict[str, set[str]]]:
    """归一化题干 → 完整题干原文，以及归一化题干 → 出现过的快照页集合。

    第二个返回值用于「同页共现」消歧：同一场次的题总在同一个结果页里，
    拿已确定的前一题就能定位到候选。
    """
    index: dict[str, str] = {}
    pages: dict[str, set[str]] = {}
    snap_dir = config.SNAPSHOT_DIR
    if not os.path.isdir(snap_dir):
        return index, pages
    for path in sorted(glob.glob(os.path.join(snap_dir, "*.html"))):
        page = os.path.basename(path)
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
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            norm = _norm_text(title)
            index.setdefault(norm, title)
            pages.setdefault(norm, set()).add(page)
    return index, pages


def _last_image_url(text: str) -> str:
    """题干里最后一张图片的 URL——多候选时用来区分「同一道题换了图」。"""
    urls = _URL_RE.findall(str(text or ""))
    return urls[-1] if urls else ""


def _disambiguate_by_bank(candidates: list[str], bank) -> tuple[str | None, str]:
    """候选唯一性不足时，用题库里的官方条目消歧。

    同一道题在不同批次里换了最后一张图，前缀就完全一样。此时看候选最后一张图
    的 URL 在题库里对应哪条官方题——命中官方条目的那个才是对的。
    """
    if not candidates or bank is None:
        return None, ""
    hits = []
    for candidate in candidates:
        url = _last_image_url(candidate)
        if not url:
            continue
        for item in bank.all():
            if item.get("source") != "official":
                continue
            if url in str(item.get("title") or ""):
                hits.append((candidate, item))
                break
    if len(hits) == 1:
        candidate, item = hits[0]
        return candidate, f"题库官方条目消歧（{item['id']}）"
    return None, ""


def _disambiguate_by_page(candidates: list[str], prev_norm: str,
                          pages: dict[str, set[str]]) -> tuple[str | None, str]:
    """候选唯一性不足时，用「同页共现」消歧。

    同一场次的题都出现在同一个结果页里。已知前一条失败题的题干后，
    能在包含它的那几页里找到的候选才是对的。
    """
    if not candidates or not prev_norm:
        return None, ""
    shared = set.intersection(*[pages.get(_norm_text(c), set()) for c in candidates])
    # 候选各自的页面集合
    cand_pages = {c: pages.get(_norm_text(c), set()) for c in candidates}
    prev_pages = set()
    for norm_key, page_set in pages.items():
        if norm_key.startswith(prev_norm[:240]):
            prev_pages |= page_set
    if not prev_pages:
        return None, ""
    # 在「含前一条题」和「含候选」都成立的页面里，只有这一个候选
    narrowed = [c for c in candidates if cand_pages[c] & prev_pages]
    if len(narrowed) == 1:
        return narrowed[0], f"同页共现消歧（{len(prev_pages)} 页）"
    return None, ""


def find_full_title(truncated: str, index: dict[str, str], bank=None,
                    prev_norm: str = "", pages: dict[str, set[str]] | None = None
                    ) -> tuple[str | None, str]:
    """返回 (完整题干, 匹配方式)；找不到返回 (None, 原因)。"""
    norm = _norm_text(truncated)
    if not norm:
        return None, "题干归一化后为空"
    for step in PREFIX_STEPS:
        key = norm[:step]
        if not key:
            continue
        candidates = sorted({full for norm_full, full in index.items() if norm_full.startswith(key)})
        if len(candidates) == 1:
            full = candidates[0]
            # 严校验：截断题干必须是完整题干的前缀
            if _norm_text(full).startswith(norm):
                return full, f"前缀匹配 {step} 字"
            return None, f"前缀不吻合（{step} 字）"
        if len(candidates) > 1:
            picked, why = _disambiguate_by_bank(candidates, bank)
            if picked:
                return picked, f"{step} 字前缀多候选，{why}"
            if pages:
                picked, why = _disambiguate_by_page(candidates, prev_norm, pages)
                if picked:
                    return picked, f"{step} 字前缀多候选，{why}"
            return None, f"{step} 字前缀有 {len(candidates)} 个候选，无法确定"
    return None, "快照里没有匹配的题干"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正写文件（默认只打印清单）")
    args = parser.parse_args()

    path = config.FAILURES_PATH
    if not os.path.isfile(path):
        print(f"找不到 {path}")
        return 1
    with open(path, "r", encoding="utf-8") as fp:
        data = json.load(fp)
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        print("failures.json 结构异常")
        return 1

    index, pages = build_index()
    print(f"快照索引题干 {len(index)} 条；失败记录 {len(items)} 条")

    bank = None
    try:
        bank = Bank(config.BANK_PATH, config.BANK_DB_PATH)
    except Exception:  # noqa: BLE001 - 题库读不了就不做消歧，不影响主流程
        print("（题库不可读，多候选将无法消歧）")

    # 按时间排序，逐条处理：前一条的题干用来做「同页共现」消歧
    ordered = sorted(items, key=lambda x: float(x.get("at") or 0.0))
    prev_norm = ""
    plans: list[dict[str, Any]] = []
    misses: list[dict[str, Any]] = []
    for item in ordered:
        title = str(item.get("title") or "")
        if len(title) == LEGACY_TRUNCATED:
            full, reason = find_full_title(title, index, bank, prev_norm, pages)
            if full:
                plans.append({"id": item.get("at"), "old": title, "new": full, "reason": reason})
                prev_norm = _norm_text(full)
            else:
                misses.append({"title": title[:60], "reason": reason})
                prev_norm = _norm_text(title)
        else:
            # 已重建过的记录直接拿完整题干当下一条的消歧依据
            prev_norm = _norm_text(title)

    print(f"可重建 {len(plans)} 条；无法重建 {len(misses)} 条")
    for plan in plans[:15]:
        print(f"  [{plan['reason']}] {plan['old'][:40]}… ({len(plan['old'])}) -> "
              f"{plan['new'][:40]}… ({len(plan['new'])})")
    if len(plans) > 15:
        print(f"  … 还有 {len(plans) - 15} 条")
    if misses:
        print("  无法重建的原因分布：")
        reasons: dict[str, int] = {}
        for miss in misses:
            reasons[miss["reason"]] = reasons.get(miss["reason"], 0) + 1
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"    {count} 条：{reason}")

    if not args.apply:
        print("\n（dry-run，加 --apply 落库）")
        return 0

    if not plans:
        print("没有需要重建的记录")
        return 0

    backup = f"{path}.bak-rebuild-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, backup)
    print(f"已备份 → {os.path.basename(backup)}")

    by_at = {plan["id"]: plan["new"] for plan in plans}
    changed = 0
    for item in items:
        new = by_at.get(item.get("at"))
        if new and item.get("title") != new:
            item["title"] = new
            item["title_rebuilt"] = True
            changed += 1
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump({"items": items}, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    print(f"已重建 {changed} 条题干，写回 {os.path.basename(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""只读 dry-run：官方记录重复/矛盾的实时分组报告。

三种口径，回答三个不同问题：

A. **查询命中口径**（``find_official``）：题干按 ``_norm_text``、选项按规范化集合
   比较 —— 这是「同一次查询会同时命中的官方记录」，也是用户真正会遇到
   「改了一条、下次却读到另一条」的范围。
B. **统一写入口径**（``align_official`` / ``_canon_key_cached``）：题干保留图片
   URL 指纹、选项集合相同的组 —— 新反馈触发自动统一的作用范围。
C. **文字骨架口径**：题干与选项**去掉图片 URL 后**相同，但图片实例不同。
   这类组查询不会互相命中（URL 不同），属于数据形态分叉，需要人工核对。

安全说明：先用 sqlite backup API 把线上库快照到临时目录（含 WAL 已提交内容），
再让 ``Bank`` 打开副本；全程不写 ``data/_merged_bank.db``，可与运行中的网关并存。

输出：reports/official-duplicates-dryrun-<时间戳>.md
"""
from __future__ import annotations

import collections
import json
import os
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.bank import (  # noqa: E402
    DEFAULT_SUBJECT,
    _IMAGE_URL_RE,
    Bank,
    _answer_semantic,
    _norm_text,
    _options_broken,
)

LIVE = os.path.join(ROOT, "data", "_merged_bank.db")
OUT_DIR = os.path.join(ROOT, "reports")


def snapshot() -> str:
    dst_path = os.path.join(tempfile.mkdtemp(prefix="bank-dryrun-"), "bank.db")
    src = sqlite3.connect(f"file:{LIVE}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dst_path


def semantic(item: dict) -> str:
    """与生产一致：优先入库派生的 answer_norm，缺失时按当前选项现算。"""
    return str(item.get("answer_norm") or "").strip() or _answer_semantic(
        str(item.get("answer") or ""), item.get("options") or [],
        str(item.get("type") or ""))


def fmt_ts(value) -> str:
    try:
        ts = float(value or 0)
    except (TypeError, ValueError):
        return "-"
    if ts <= 0:
        return "-"
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def options_preview(options) -> str:
    parts = [str(o) for o in (options or [])]
    text = " / ".join(parts[:4])
    if len(parts) > 4:
        text += f" / …(+{len(parts) - 4})"
    return text.replace("|", "\\|")[:140] or "-"


def strip_urls(text) -> str:
    return _IMAGE_URL_RE.sub(" ", str(text or ""))


def ocr_signature(item: dict, ocr_map: dict) -> str | None:
    """该题全部图片 URL 的 OCR 文本拼接；任一图片缺 OCR 则无法判定。

    题干文字相同、图片不同的记录可能只是「同一句式、不同公式」的不同题
    （答案本来就该不同）。用网关已有的 ``data/image_text.json`` 判断公式是否
    真的一样，避免把不同题误报成「官方答案矛盾」。
    """
    urls = _IMAGE_URL_RE.findall(str(item.get("title") or ""))
    for option in item.get("options") or []:
        urls += _IMAGE_URL_RE.findall(str(option))
    if not urls:
        return None
    texts = []
    for url in urls:
        entry = ocr_map.get(url) or {}
        text = str(entry.get("text") or "").strip()
        if str(entry.get("status") or "") != "ok" or not text or "无法辨认" in text:
            return None
        texts.append(_norm_text(text))
    return " ".join(texts)


def skeleton_key(bank: Bank, item: dict) -> tuple:
    options = [strip_urls(o) for o in item.get("options") or []]
    return (
        item["type"],
        _norm_text(strip_urls(item.get("title"))),
        bank._norm_options_key(options, item["type"]),
    )


def main() -> int:
    path = snapshot()
    bank = Bank(os.path.join(os.path.dirname(path), "bank.json"), path)
    try:
        items = list(bank._items)
        officials = [
            i for i in items
            if i.get("source") == "official" and str(i.get("answer") or "").strip()
        ]

        # A. 查询命中口径（find_official 会同时命中的记录）
        group_a: dict[tuple, list[dict]] = collections.defaultdict(list)
        for item in officials:
            if item.get("status") != "adopted":
                continue
            if _options_broken(item.get("options") or [], item.get("type")):
                continue
            key = (
                item["type"],
                _norm_text(item["title"]),
                bank._norm_options_key(item.get("options") or [], item["type"]),
            )
            group_a[key].append(item)

        # B. 统一写入口径（align_official 的 canon key，含图片指纹维度）
        group_b: dict[tuple, list[dict]] = collections.defaultdict(list)
        for item in officials:
            group_b[bank._canon_key_cached(item)].append(item)

        # C. 文字骨架口径（去掉图片 URL 后相同）
        group_c: dict[tuple, list[dict]] = collections.defaultdict(list)
        for item in officials:
            group_c[skeleton_key(bank, item)].append(item)

        def split(groups, min_size=2):
            contradictory, consistent = [], []
            for key, members in groups.items():
                if len(members) < min_size:
                    continue
                sems = {semantic(i) for i in members if semantic(i)}
                if len(sems) > 1:
                    contradictory.append((key, members, sems))
                else:
                    consistent.append((key, members))
            return contradictory, consistent

        a_conf, _ = split(group_a)
        b_conf, _ = split(group_b)
        c_conf, c_consistent = split(group_c)

        # C 里只保留「图片实例确实不同」的组：同一 URL 的组是正常重复，A/B 口径会覆盖。
        with open(os.path.join(ROOT, "data", "image_text.json"), "r", encoding="utf-8") as fh:
            ocr_map = json.load(fh)
        c_same, c_unknown, c_diff = [], [], []
        for key, members, sems in c_conf:
            shapes = {
                (_norm_text(i["title"]),
                 tuple(bank._norm_options_key(i.get("options") or [], i["type"])))
                for i in members
            }
            if len(shapes) <= 1:
                continue
            sigs = {i["id"]: ocr_signature(i, ocr_map) for i in members}
            ready = [s for s in sigs.values() if s]
            if len(set(ready)) >= 2:
                c_diff.append((key, members, sems, sigs))
            elif len(ready) >= 2:
                c_same.append((key, members, sems, sigs))
            else:
                c_unknown.append((key, members, sems, sigs))
        c_conf = c_same + c_unknown

        broken = [
            i for i in officials
            if _options_broken(i.get("options") or [], i.get("type"))
        ]

        os.makedirs(OUT_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_path = os.path.join(OUT_DIR, f"official-duplicates-dryrun-{stamp}.md")

        lines: list[str] = []
        lines.append("# 官方记录重复/矛盾 dry-run 报告")
        lines.append("")
        lines.append(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"- 线上库快照：`{LIVE}`（sqlite backup，只读）")
        lines.append(f"- 题库记录：{len(items)} 条；官方答案非空：{len(officials)} 条")
        lines.append("")
        lines.append("## 结论摘要")
        lines.append("")
        lines.append("| 口径 | 含义 | 多记录组 | 答案矛盾组 |")
        lines.append("|---|---|---|---|")
        lines.append(f"| A 查询命中 | 同一次查询会同时命中（find_official） | "
                     f"{sum(1 for v in group_a.values() if len(v) >= 2)} | {len(a_conf)} |")
        lines.append(f"| B 统一写入 | align_official 的作用范围 | "
                     f"{sum(1 for v in group_b.values() if len(v) >= 2)} | {len(b_conf)} |")
        lines.append(f"| C 文字骨架 | 题干+选项去掉图片 URL 后相同 | "
                     f"{sum(1 for v in group_c.values() if len(v) >= 2)} | "
                     f"{len(c_same) + len(c_unknown)}（真同题 {len(c_same)} / "
                     f"无法判断 {len(c_unknown)} / OCR 证明不同题 {len(c_diff)}） |")
        lines.append("")
        lines.append(f"- 选项损坏（重复/空项）的官方非空记录：{len(broken)} 条")
        lines.append("")

        def dump_group(index: int, members, sems=None, ocr_sigs=None) -> None:
            head = members[0]
            lines.append(f"### {index}. [{head.get('type')}] {str(head.get('title') or '')[:70]}")
            lines.append("")
            if sems:
                lines.append(f"- 答案语义分叉：{' / '.join(sorted(sems))}")
            lines.append(f"- 专业：{' / '.join(sorted({str(i.get('subject') or '') for i in members}))}")
            hashes = sorted({str(i.get('image_hash') or '') for i in members})
            lines.append(f"- 图片指纹：{', '.join(h or '(空)' for h in hashes)}")
            lines.append("")
            lines.append("| id | subject | answer | 语义 | via | 状态 | 更新时间 | 选项 |")
            lines.append("|---|---|---|---|---|---|---|---|")
            for item in sorted(members, key=lambda i: -(i.get("updated") or i.get("created") or 0)):
                lines.append(
                    f"| {item.get('id')} | {item.get('subject') or '-'} "
                    f"| {str(item.get('answer') or '-').replace('|', chr(92) + '|')} "
                    f"| {semantic(item) or '(无法解释)'} "
                    f"| {item.get('official_via') or '-'} "
                    f"| {item.get('status') or '-'} "
                    f"| {fmt_ts(item.get('updated') or item.get('created'))} "
                    f"| {options_preview(item.get('options'))} |")
            if ocr_sigs:
                lines.append("")
                for item in sorted(members, key=lambda i: -(i.get("updated") or i.get("created") or 0)):
                    sig = ocr_sigs.get(item.get("id"))
                    shown = (sig[:70] + "…") if sig and len(sig) > 70 else sig
                    lines.append(f"- OCR {item.get('id')}: {shown or 'NONE'}")
            lines.append("")

        for label, groups in (("A 查询命中口径", a_conf), ("B 统一写入口径", b_conf)):
            lines.append(f"## {label} — 矛盾组 {len(groups)} 个")
            lines.append("")
            if not groups:
                lines.append("无。")
                lines.append("")
                continue
            for index, (key, members, sems) in enumerate(groups, 1):
                dump_group(index, members, sems)

        lines.append(f"## C 文字骨架口径 — 文字相同、图片不同的组 "
                     f"{len(c_same) + len(c_unknown) + len(c_diff)} 个")
        lines.append("")
        lines.append(f"- OCR 证明是不同公式（不同题，答案不同正常）：{len(c_diff)} 组")
        lines.append(f"- OCR 证明公式相同、答案矛盾（真同题分叉）：{len(c_same)} 组")
        lines.append(f"- OCR 不足、无法判断：{len(c_unknown)} 组")
        lines.append("")
        if c_same:
            lines.append("### 需处理：公式相同、答案矛盾")
            lines.append("")
            for index, (key, members, sems, sigs) in enumerate(c_same, 1):
                dump_group(index, members, sems, sigs)
        if c_unknown:
            lines.append("### 无法判断：缺少 OCR")
            lines.append("")
            for index, (key, members, sems, sigs) in enumerate(c_unknown, 1):
                dump_group(index, members, sems, sigs)

        if broken:
            lines.append("## 附：选项损坏的官方记录")
            lines.append("")
            for item in broken:
                lines.append(f"- {item.get('id')} [{item.get('type')}] {str(item.get('title') or '')[:50]} "
                             f"answer={item.get('answer')!r} options={options_preview(item.get('options'))}")
            lines.append("")

        lines.append("## 执行说明（尚未执行）")
        lines.append("")
        lines.append("- 本报告只读生成，线上库与答题记忆均未改动。")
        lines.append("- A/B 口径的矛盾组 = 新代码 `align_official` 的修复对象：")
        lines.append("  把组内全部官方记录统一成同一份「答案 + 选项顺序」（page 优先）。")
        lines.append("- C 口径的组不会在查询时互相顶替（题干图片 URL 不同）。")
        lines.append("  报告用 `data/image_text.json` 的 OCR 文本判断公式是否相同：")
        lines.append("  OCR 不同 = 本来就是不同的题，答案不同正常，不需要处理；")
        lines.append("  只有 OCR 证明公式相同而答案矛盾的组才需要人工确认。")

        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

        print(f"A_query_groups={sum(1 for v in group_a.values() if len(v) >= 2)} "
              f"A_conflicts={len(a_conf)}")
        print(f"B_align_groups={sum(1 for v in group_b.values() if len(v) >= 2)} "
              f"B_conflicts={len(b_conf)}")
        print(f"C_skeleton_groups={sum(1 for v in group_c.values() if len(v) >= 2)} "
              f"C_same_formula={len(c_same)} C_unknown={len(c_unknown)} "
              f"C_different_formula={len(c_diff)}")
        print(f"broken_official={len(broken)}")
        print(f"report={out_path}")
        return 0
    finally:
        bank.close()


if __name__ == "__main__":
    raise SystemExit(main())

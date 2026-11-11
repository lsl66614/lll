"""核对 canonical 组内的答案冲突：拿快照真值判谁对谁错（只读）。

背景：P3 的存量体检报告了「18 组 canonical 分组内语义答案不一致」，只报告未改写。
本脚本回答两个问题：

1. 每组里**规范记录**（匹配时实际会返回的那条）的答案，和平台真值一致吗？
2. 被挂靠的成员里，答错的是哪几条？（它们只是历史证据，不影响出答案）

真值来源：``data/snapshots/*.html`` 结果页。解析复用 ``app.mooc_result.parse_result_html``，
选项身份按「图片取文件名（跨图床稳定）、文字取按题型归一的内容」比较 —— 不看字母位置。

⚠️ 2026-11-10 14:03 管理台执行过一次去重，线上库 1273 → 931 条，
这 18 组的非规范成员已被合并删除。所以默认从**去重前备份**还原分组，
同时检查规范记录是否仍在线上库里、答案是否一致。

用法：
    .venv\\Scripts\\python.exe scripts/check_canonical_conflicts.py
    .venv\\Scripts\\python.exe scripts/check_canonical_conflicts.py --groups-db <备份.db>
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config  # noqa: E402
from app.bank import Bank, _LETTERS, _dice, _norm_option  # noqa: E402
from app.mooc_result import parse_result_html  # noqa: E402
from app.shape_key import text_key  # noqa: E402

DEFAULT_GROUPS_DB = os.path.join(
    config.DATA_DIR, "backups", "bank-before-optionsnorm-20261110-135119.db")

_IMG_PREFIX = re.compile(r"^_?(?:photon?uploadutils|uploadutils)_?", re.IGNORECASE)
_IMG_EXT = re.compile(r"\.(?:png|jpe?g|gif|bmp|webp)$", re.IGNORECASE)


def option_identity(option, qtype: str) -> str:
    """选项身份：图片按文件名（跨图床稳定），文字按按题型归一后的内容。"""
    text = str(option or "").strip()
    if text.lower().startswith("http"):
        name = text.split("?", 1)[0].rstrip("/").split("/")[-1]
        name = _IMG_PREFIX.sub("", name)
        name = _IMG_EXT.sub("", name)
        return "img:" + name.lower()
    return "txt:" + _norm_option(text, qtype)


def letters_identity(options: list[str], letters: list[str], qtype: str) -> tuple[str, ...]:
    picked = []
    for ch in letters:
        index = _LETTERS.index(ch.upper())
        if index < len(options):
            picked.append(option_identity(options[index], qtype))
    return tuple(sorted(set(picked)))


def item_identity(item: dict) -> tuple[str, ...]:
    """题库记录的答案 → 选项身份集合；无法解释返回空元组。"""
    qtype = item["type"]
    answer = str(item.get("answer") or "").strip()
    options = item.get("options") or []
    if not answer:
        return ()
    if qtype in ("single", "multiple"):
        parts = [p.strip().upper() for p in answer.split("#") if p.strip()]
        if parts and all(p in _LETTERS for p in parts):
            return letters_identity(options, parts, qtype)
        if parts:
            return tuple(sorted(set(option_identity(p, qtype) for p in parts)))
        return ()
    if qtype == "judge":
        semantic = _norm_option(answer, "judge")
        if semantic in ("正确", "错误"):
            return ("txt:" + semantic,)
        parts = [p.strip().upper() for p in answer.split("#") if p.strip()]
        if parts and parts[0] in _LETTERS:
            return letters_identity(options, parts, qtype)
        return ()
    return (("txt:" + answer,) if answer else ())


def option_set_key(item: dict) -> tuple[str, ...]:
    return tuple(sorted(option_identity(o, item["type"]) for o in item.get("options") or []))


def build_truth() -> tuple[dict, dict]:
    """快照真值索引：{(type, 骨架, 选项集)} / {(type, 骨架)} → set(答案身份元组)。"""
    exact: dict[tuple, set] = {}
    skeleton: dict[tuple, set] = {}
    snapshot_dir = config.SNAPSHOT_DIR
    files = sorted(f for f in os.listdir(snapshot_dir) if f.endswith(".html"))
    for name in files:
        path = os.path.join(snapshot_dir, name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fp:
                html = fp.read()
        except OSError:
            continue
        if "u-questionItem" not in html:
            continue
        for case in parse_result_html(html):
            letters = case.get("correct_letters") or []
            options = case.get("options") or []
            qtype = case.get("type") or "single"
            if not letters or not options:
                continue
            truth = letters_identity(options, letters, qtype)
            if not truth:
                continue
            key = (qtype, text_key(case.get("title") or ""))
            exact.setdefault(key + (tuple(sorted(option_identity(o, qtype) for o in options)),), set()).add(truth)
            skeleton.setdefault(key, set()).add(truth)
    return exact, skeleton


def lookup_truth(item: dict, exact: dict, skeleton: dict) -> tuple[str, set]:
    """返回 (匹配方式, 真值集合)。匹配方式：exact / skeleton / fuzzy / miss。"""
    key = (item["type"], text_key(item.get("title") or ""))
    hit = exact.get(key + (option_set_key(item),))
    if hit:
        return "exact", hit
    hit = skeleton.get(key)
    if hit:
        return "skeleton", hit
    # 模糊兜底：同题型 + 同选项集 + 题干骨架 Dice ≥ 0.9
    target = text_key(item.get("title") or "")
    wanted = option_set_key(item)
    collected: set = set()
    for (qtype, skel), truths in skeleton.items():
        if qtype != item["type"] or not skel:
            continue
        if _dice(target, skel) >= 0.9:
            collected |= truths
    if collected:
        return "fuzzy", collected
    return "miss", set()


def _verdict(identity: tuple[str, ...], truth: set) -> str:
    if not identity:
        return "空答案"
    if len(truth) != 1:
        return "真值歧义"
    if identity == next(iter(truth)):
        return "对"
    return "错"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--groups-db", default=DEFAULT_GROUPS_DB)
    parser.add_argument("--live-db", default=config.BANK_DB_PATH)
    args = parser.parse_args()

    exact, skeleton = build_truth()
    print("快照真值：%d 个精确键 / %d 个骨架键\n" % (len(exact), len(skeleton)))

    live: dict[str, tuple] = {}
    if os.path.exists(args.live_db):
        conn = sqlite3.connect(args.live_db)
        live = {
            str(r[0]): (str(r[1] or "").strip(), r[2], str(r[3] or ""), str(r[4] or ""))
            for r in conn.execute("SELECT id, answer, options, source, status FROM questions")
        }
        conn.close()
    print("线上库 %d 条（与分组库 %s）\n" % (len(live), os.path.basename(args.groups_db)))

    tmp = tempfile.mkdtemp()
    copy_path = os.path.join(tmp, "groups.db")
    shutil.copyfile(args.groups_db, copy_path)
    bank = Bank(os.path.join(tmp, "bank.json"), copy_path)
    try:
        conn = sqlite3.connect(copy_path)
        stored = {str(r[0]): str(r[1] or "") for r in
                  conn.execute("SELECT id, canonical_id FROM questions")}
        conn.close()
        items = list(bank._items)
        for item in items:  # 用库里存的 canonical_id 分组（不要用加载时重算的结果）
            item["canonical_id"] = stored.get(item["id"], "")
    finally:
        bank.db.close()

    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(item.get("canonical_id") or item["id"], []).append(item)

    conflict_groups = []
    for cid, members in groups.items():
        if len(members) < 2:
            continue
        semantics = {
            str(m.get("answer_norm") or "").strip()
            for m in members if str(m.get("answer_norm") or "").strip()
        }
        if len(semantics) > 1:
            conflict_groups.append((cid, members))
    conflict_groups.sort(key=lambda pair: (pair[1][0]["type"], pair[1][0]["title"]))

    stats = {"groups": len(conflict_groups), "truth": 0, "ambiguous": 0, "miss": 0,
             "canon_ok": 0, "canon_bad": 0, "canon_unknown": 0,
             "member_ok": 0, "member_bad": 0, "live_gone": 0}
    bad_canon = []

    for index, (cid, members) in enumerate(conflict_groups, 1):
        canon = next((m for m in members if m["id"] == cid), members[0])
        mode, truth = lookup_truth(canon, exact, skeleton)
        if mode == "miss":
            stats["miss"] += 1
        elif len(truth) == 1:
            stats["truth"] += 1
        else:
            stats["ambiguous"] += 1
        print("=" * 92)
        print("组 %d/%d  %s  规范 %s  %s/%s  answer=%r" % (
            index, len(conflict_groups), canon["type"], cid,
            canon.get("source"), canon.get("status"), canon.get("answer")))
        print("  title: %s" % str(canon.get("title"))[:96])
        if mode == "miss":
            print("  真值: 未找到（快照里没有这道题）")
        elif len(truth) == 1:
            print("  真值: %s   [匹配方式 %s]" % (list(truth)[0], mode))
        else:
            print("  真值: 歧义（%d 种）%s   [匹配方式 %s]" % (
                len(truth), list(truth)[:3], mode))
        for member in members:
            identity = item_identity(member)
            verdict = _verdict(identity, truth) if truth else ""
            mark = "*" if member["id"] == cid else " "
            live_row = live.get(member["id"])
            live_note = ""
            if live_row is not None:
                live_identity = item_identity({
                    "type": canon["type"], "answer": live_row[0],
                    "options": _json_options(live_row[1]),
                })
                live_note = " | 线上:%s" % (_verdict(live_identity, truth) if truth else "?")
            print("  %s %s %-9s %-8s ans=%-6r 身份=%-28s %s%s" % (
                mark, member["id"], member.get("source"), member.get("status"),
                member.get("answer"), identity, verdict, live_note))
            if truth and len(truth) == 1:
                if member["id"] == cid:
                    if verdict == "对":
                        stats["canon_ok"] += 1
                    else:
                        stats["canon_bad"] += 1
                        bad_canon.append((cid, canon, truth))
                elif verdict == "对":
                    stats["member_ok"] += 1
                else:
                    stats["member_bad"] += 1
            elif member["id"] == cid:
                stats["canon_unknown"] += 1
        if canon["id"] not in live:
            stats["live_gone"] += 1

    print("\n" + "=" * 92)
    print("汇总：%d 组" % stats["groups"])
    print("  真值：唯一 %d 组，歧义 %d 组，未找到 %d 组" % (
        stats["truth"], stats["ambiguous"], stats["miss"]))
    print("  规范记录：对 %d，错 %d，无法判定 %d" % (
        stats["canon_ok"], stats["canon_bad"], stats["canon_unknown"]))
    print("  非规范成员：对 %d，错 %d" % (stats["member_ok"], stats["member_bad"]))
    print("  线上库：规范记录已不在（被合并删掉）%d 条" % stats["live_gone"])
    if bad_canon:
        print("\n⚠️ 规范答案与快照真值不符（这些是实际会答出去的）：")
        for cid, canon, truth in bad_canon:
            print("  · %s answer=%r 真值=%s  %s" % (
                cid, canon.get("answer"), list(truth)[0][:60], str(canon.get("title"))[:60]))
    return 0


def _json_options(raw) -> list[str]:
    import json
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in value] if isinstance(value, list) else []


if __name__ == "__main__":
    raise SystemExit(main())

"""从多个来源重建题库，产出干净的 db。

背景（2026-11-10 实测）：
- ``data/bank.db`` 主表损坏（``database disk image is malformed``），
  ``title`` 列读不出来，FTS 索引也没了。
- 同时 ``data/bank.json`` 从 1224 条掉到 777 条 —— 这是
  ``dedupe_near`` 误删造成的（题干里的公式图 URL 被归一化抹掉，
  不同公式图被判成同一道题，见 app/bank.py 的 ``_norm_title_for_dedupe``）。

恢复来源（按可信度从高到低）：
1. ``data/bank.db.bak-before-clean-*``（1224 条，qc=ok）—— 清洗前的完整快照；
2. ``data/backups/auto-*.zip`` 里条数最多的 ``bank.json``（实测 1052 条）——
   网关内存快照，能按题干找回部分被删记录；
3. ``data/snapshots/*.html`` —— 平台结果页原始 HTML，能从官方反馈里
   找回被清空的官方答案。

用法：
    .venv\\Scripts\\python.exe scripts/recover_bank.py            # 只报告，不写
    .venv\\Scripts\\python.exe scripts/recover_bank.py --apply    # 写 data/bank.rebuilt.db
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config  # noqa: E402
from app.bankdb import BankDB  # noqa: E402
from app.mooc_result import parse_result_html  # noqa: E402
from app.shape_key import text_key  # noqa: E402

COLUMNS = [
    "id", "type", "title", "options", "answer", "subject", "image_hash", "sh",
    "source", "level", "via", "status", "history", "wrong_answers", "agree",
    "conflict", "created", "official_via",
]
JSON_COLUMNS = ("options", "history", "wrong_answers")
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
OUT_DB = os.path.join(config.DATA_DIR, "bank.rebuilt.db")


def _decode(item: dict) -> dict:
    for column in JSON_COLUMNS:
        raw = item.get(column)
        if isinstance(raw, str):
            try:
                item[column] = json.loads(raw or ("[]" if column != "options" else "{}"))
            except (TypeError, ValueError):
                item[column] = [] if column != "options" else {}
    return item


def read_backup_db(path: str) -> list[dict]:
    """从完好的 db 备份里读出全部记录。"""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    except sqlite3.Error:
        return []
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute("SELECT %s FROM questions" % ",".join(COLUMNS))]
    except sqlite3.DatabaseError:
        rows = []
    finally:
        conn.close()
    return [_decode(r) for r in rows]


def best_auto_backup() -> tuple[str, list[dict]]:
    """自动备份 zip 里条数最多的那个 bank.json（网关内存快照）。"""
    best_name, best_items = "", []
    for path in glob.glob(os.path.join(config.DATA_DIR, "backups", "auto-*.zip")):
        try:
            with zipfile.ZipFile(path) as zf:
                if "bank.json" not in zf.namelist():
                    continue
                payload = json.loads(zf.read("bank.json"))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            continue
        items = payload.get("items") if isinstance(payload, dict) else payload
        items = items if isinstance(items, list) else []
        if len(items) > len(best_items):
            best_name, best_items = os.path.basename(path), items
    return best_name, [_decode(dict(i)) for i in best_items]


def snapshot_truth() -> dict[tuple[str, str], set]:
    """平台结果页 → {(题型, 题干骨架): {正确选项内容元组}}。"""
    truth: dict[tuple[str, str], set] = {}
    snap_dir = config.SNAPSHOT_DIR
    if not os.path.isdir(snap_dir):
        return truth
    for name in sorted(os.listdir(snap_dir)):
        if not name.endswith(".html"):
            continue
        try:
            with open(os.path.join(snap_dir, name), "r", encoding="utf-8", errors="replace") as fp:
                html = fp.read()
        except OSError:
            continue
        if "u-questionItem" not in html:
            continue
        for item in parse_result_html(html):
            letters = item.get("correct_letters") or []
            options = item.get("options") or []
            if not letters or not options:
                continue
            good = set()
            for letter in letters:
                index = LETTERS.find(letter.upper())
                if 0 <= index < len(options):
                    good.add(options[index])
            if not good:
                continue
            key = (item["type"], text_key(item["title"]))
            truth.setdefault(key, set()).add(tuple(sorted(good)))
    return truth


def snapshot_options() -> dict[tuple[str, str], list[str]]:
    """平台结果页 → {(题型, 题干骨架): 选项列表}（只要有内容的选项）。

    用来修复「答案悬空」的记录：公式题的选项是图片，入库时有时只存了
    ``["","","",""]``，答案字母指向一个空选项 —— 看起来是官方答案，
    实际上什么也没答对。结果页里有完整的选项内容，可以补回来。
    """
    options_map: dict[tuple[str, str], list[str]] = {}
    snap_dir = config.SNAPSHOT_DIR
    if not os.path.isdir(snap_dir):
        return options_map
    for name in sorted(os.listdir(snap_dir)):
        if not name.endswith(".html"):
            continue
        try:
            with open(os.path.join(snap_dir, name), "r", encoding="utf-8", errors="replace") as fp:
                html = fp.read()
        except OSError:
            continue
        if "u-questionItem" not in html:
            continue
        for item in parse_result_html(html):
            options = [str(o or "") for o in (item.get("options") or [])]
            if not options or not any(o.strip() for o in options):
                continue
            key = (item["type"], text_key(item["title"]))
            options_map.setdefault(key, options)
    return options_map


def _title_key(item: dict) -> tuple[str, str]:
    return (str(item.get("type") or ""), text_key(item.get("title")))


def _remap_answer(answer: str, old_options: list, new_options: list, qtype: str) -> str:
    """选项被替换后，按「选项内容」把答案字母重新定位到新选项上。

    定位不到就返回空 —— 宁可没有答案，也不要一个指向错误选项的答案。
    """
    from app.bank import _norm_option  # noqa: PLC0415

    old = [_norm_option(o, qtype) for o in (old_options or [])]
    new = [_norm_option(o, qtype) for o in (new_options or [])]
    parts = [p.strip().upper() for p in re.split(r"[#、,，;；/|]+", answer) if p.strip()]
    if not parts or any(p not in LETTERS for p in parts):
        return ""
    mapped: list[int] = []
    for part in parts:
        index = LETTERS.index(part)
        if index >= len(old):
            return ""
        content = old[index]
        if not content:
            return ""  # 旧选项本来就是空的，内容无从判断
        if content not in new:
            return ""
        target = new.index(content)
        if target not in mapped:
            mapped.append(target)
    if not mapped:
        return ""
    return "#".join(LETTERS[i] for i in sorted(mapped))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    # ---- 来源 1：完好的 db 备份 ----
    candidates = sorted(glob.glob(os.path.join(config.DATA_DIR, "bank.db.bak-*")))
    base_items: list[dict] = []
    base_name = ""
    for path in reversed(candidates):
        items = read_backup_db(path)
        if not items:
            continue
        # 只接受能读全列的备份
        probe = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
        try:
            ok = probe.execute("SELECT COUNT(title) FROM questions").fetchone()[0] == len(items)
        except sqlite3.DatabaseError:
            ok = False
        finally:
            probe.close()
        if ok and len(items) > len(base_items):
            base_items, base_name = items, os.path.basename(path)
    print("来源1 db 备份：%s（%d 条）" % (base_name or "无", len(base_items)))

    by_id = {i["id"]: i for i in base_items}

    # ---- 来源 2：自动备份 zip ----
    zip_name, zip_items = best_auto_backup()
    print("来源2 自动备份：%s（%d 条）" % (zip_name or "无", len(zip_items)))
    zip_by_title: dict[tuple[str, str], dict] = {}
    for item in zip_items:
        zip_by_title.setdefault(_title_key(item), item)

    # ---- 来源 3：平台结果页快照 ----
    truth = snapshot_truth()
    snap_options = snapshot_options()
    print("来源3 结果页快照：%d 条官方答案，%d 组选项" % (len(truth), len(snap_options)))

    # ---- 合并：补回被 dedupe 误删的记录 ----
    #
    # 事故形态（2026-11-10 实测）：dedupe 把 452 条记录合并掉了，其中
    # 325 条有「同题干+同选项」的存活孪生（题还在，只是合并到一条），
    # 但有 127 条**连孪生都没有** —— 题面在当前库里彻底消失了，
    # 其中 110 条是官方答案。
    #
    # 恢复策略：把「当前库里已经完全没有这道题」的记录补回来。
    #   - 自动备份 zip 里同题目的 → 直接补（连答案一起带回来）
    #   - 只有结果页快照里有答案的 → 补题干+选项，答案用快照的官方答案
    from app.bank import _norm_title_for_dedupe  # noqa: PLC0415

    def fingerprint(item: dict) -> tuple:
        """题目指纹：题型 + 题干（保留图片 URL 区分度）+ 选项内容。

        选项也要用 ``_norm_title_for_dedupe`` 归一 —— 选项常是图片 URL，
        不同来源的同一题 URL 可能不同，用裸字符串比对会判成不同题。
        """
        return (
            str(item.get("type") or ""),
            _norm_title_for_dedupe(item.get("title")),
            tuple(sorted(_norm_title_for_dedupe(o) for o in (item.get("options") or []))),
        )

    live_path = os.path.join(config.DATA_DIR, "bank.json")
    live_fp: set[tuple] = set()
    live_items: list[dict] = []
    if os.path.exists(live_path):
        try:
            with open(live_path, "r", encoding="utf-8") as fp:
                payload = json.load(fp)
            live_items = payload.get("items", []) if isinstance(payload, dict) else (payload or [])
            live_fp = {fingerprint(i) for i in live_items if isinstance(i, dict)}
        except (OSError, ValueError):
            live_items = []
            live_fp = set()
    print("当前 bank.json：%d 条（%d 个不同指纹）" % (len(live_items), len(live_fp)))

    restored_from_zip = 0
    restored_from_snapshot = 0
    restored_from_backup_only = 0
    still_missing = 0

    # 先按指纹补回自动备份里的题（它带完整答案）
    zip_by_fp: dict[tuple, dict] = {}
    for item in zip_items:
        zip_by_fp.setdefault(fingerprint(item), item)
    for item in base_items:
        fp = fingerprint(item)
        if fp in live_fp:
            continue  # 当前库里还在，不用补
        donor = zip_by_fp.get(fp)
        if donor is not None:
            # 用备份里的版本（含答案）覆盖这条的字段，id 沿用库里的
            for key in ("answer", "status", "source", "options", "history",
                        "wrong_answers", "agree", "conflict", "official_via", "via"):
                if donor.get(key) not in (None, "", [], {}):
                    item[key] = donor[key]
            restored_from_zip += 1
            continue
        # 没有备份版本，但结果页快照里有官方答案 → 用快照答案校验/补全
        key = _title_key(item)
        candidates = truth.get(key)
        have_answer = bool(str(item.get("answer") or "").strip())
        if not candidates or len(candidates) != 1:
            # 快照帮不上忙；但这条记录本身自带答案（备份里就有），
            # 照样能补回当前库 —— 只是没法用平台反馈交叉验证。
            if have_answer:
                restored_from_backup_only += 1
            else:
                still_missing += 1
            continue
        good = list(candidates)[0]
        options = item.get("options") or []
        answer = ""
        if item.get("type") == "judge":
            answer = next((g for g in good if g in ("正确", "错误")), "")
        else:
            for index, opt in enumerate(options):
                if any((opt or "").strip() in (g or "").strip()
                       or (g or "").strip() in (opt or "").strip() for g in good):
                    answer = LETTERS[index] if index < len(LETTERS) else ""
                    break
        if answer:
            item["answer"] = answer
            item["status"] = "adopted"
            item["conflict"] = False
            item["agree"] = max(int(item.get("agree") or 0), 2)
            restored_from_snapshot += 1
        elif have_answer:
            restored_from_backup_only += 1
        else:
            still_missing += 1

    # 备份 zip 里有、db 备份里完全没有的题干 → 也补进来
    base_fps = {fingerprint(v) for v in base_items}
    for item in zip_items:
        fp = fingerprint(item)
        if fp in base_fps:
            continue
        by_id[item["id"]] = item
        base_fps.add(fp)
        restored_from_zip += 1

    items = list(by_id.values())
    print()
    print("补回：自动备份 %d 条，快照答案 %d 条，仅备份自带答案 %d 条，仍缺 %d 条"
          % (restored_from_zip, restored_from_snapshot, restored_from_backup_only, still_missing))

    # ---- 修复「悬空答案」----
    #
    # 公式题的选项是图片，入库时有时只存了 ["","","",""]，而答案字母（比如 C）
    # 正好指向那个空选项 —— 记录是 official+adopted，看起来是权威答案，
    # 实际上什么也没答对。实测 2026-11-10：新库里有 69~70 条这种记录。
    #
    # 结果页快照里有完整的选项内容（带图片 URL），按题干骨架对上就能补回来。
    dangling_fixed = 0
    dangling_left = 0
    for item in items:
        answer = str(item.get("answer") or "").strip()
        options = item.get("options") or []
        if not answer or not options:
            continue
        parts = [p.upper() for p in answer.replace("#", "") if p.upper() in LETTERS]
        if not parts:
            continue
        index = LETTERS.index(parts[0])
        if index < len(options) and str(options[index] or "").strip():
            continue  # 答案指向有内容的选项，正常
        donor_options = snap_options.get(_title_key(item))
        if not donor_options:
            dangling_left += 1
            continue
        # 用快照的选项替换；答案字母要按「内容」重新定位
        old_options = options
        item["options"] = list(donor_options)
        mapped = _remap_answer(answer, old_options, donor_options, item.get("type"))
        if mapped:
            item["answer"] = mapped
        dangling_fixed += 1
    print()
    print("悬空答案修复：%d 条（还剩 %d 条无快照可对）" % (dangling_fixed, dangling_left))
    print("合计 %d 条" % len(items))

    if not args.apply:
        print("\n（dry-run，加 --apply 写出 %s）" % os.path.basename(OUT_DB))
        return 0

    for suffix in ("", "-wal", "-shm"):
        path = OUT_DB + suffix
        if os.path.exists(path):
            os.remove(path)
    db = BankDB(OUT_DB)
    db.replace_all(items)
    db.checkpoint()

    check = sqlite3.connect("file:%s?mode=ro" % OUT_DB.replace("\\", "/"), uri=True)
    count = check.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    fts = check.execute("SELECT COUNT(*) FROM questions_fts").fetchone()[0]
    integrity = check.execute("PRAGMA quick_check").fetchone()[0]
    print()
    print("新库校验：题数 %d，FTS %d，quick_check=%s" % (count, fts, integrity[:40]))
    check.close()
    print("已写出", OUT_DB)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

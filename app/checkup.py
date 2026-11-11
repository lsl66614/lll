"""题库体检：管理台「题库体检」板块的只读数据源。

两块内容：

1. **轻量指标**（``light_metrics``，每次现算，毫秒级，不读快照）：
   - 官方空答案：``source=official`` 且 ``answer`` 为空的题 —— 这些会被
     ``find_official`` 当作权威答案返回，却没有内容可答；
   - 同骨架官方答案冲突：同题型 + 同题干骨架 + 同选项集，但官方答案语义不一致；
   - 无法解释的字母答案（``answer`` 有值但 ``answer_norm`` 为空）；
   - 判断题存字母答案（应为 0，语义词才是正确形态）；
   - canonical 分组内语义答案不一致。

2. **快照体检**（``snapshot_report``，读 ``data/snapshots``，秒级，结果缓存）：
   把快照结果页里的平台真值当标准答案，按网关的**题库侧**路径逐条模拟
   （``find_official`` → ``match``，不喂 AI），统计「命中来源 × 答对/答错」。
   用例收集、答案比对与官方候选统一走 ``app/evaluation.py``，
   体检、巡检与脚本共用同一口径。

只读：不改题库、不改快照。诊断里发现的「相似题复用官方答案」「试卷导入的
官方答案错/空」两处缺陷只展示，不在这里修。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from . import config
from . import evaluation as evaluation_mod
from .bank import (
    DEFAULT_SUBJECT,
    Bank,
    _LETTERS,
    _norm_option,
    _norm_text,
)
from .shape_key import text_key
from .subjects import SubjectRules

_CACHE: dict[str, Any] = {"signature": None, "at": 0.0, "report": None}
_LOCK = threading.Lock()
_TTL_SECONDS = 600

_subject_rules: SubjectRules | None = None
_subject_rules_sig: tuple[float, int] | None = None


def _rules_signature() -> tuple[float, int] | None:
    try:
        stat = os.stat(config.SUBJECTS_PATH)
    except OSError:
        return None
    return (stat.st_mtime, stat.st_size)


def _rules() -> SubjectRules:
    """按文件签名重载分类规则：管理台改了规则后，体检不必重启就生效。"""
    global _subject_rules, _subject_rules_sig
    signature = _rules_signature()
    if _subject_rules is None or signature != _subject_rules_sig:
        _subject_rules = SubjectRules(config.SUBJECTS_PATH)
        _subject_rules_sig = signature
    return _subject_rules


def _pick_subject(title: str, options: list[str]) -> str:
    inferred, _ = _rules().infer(title, options)
    return inferred or DEFAULT_SUBJECT


def _options(options_raw: Any, qtype: str) -> tuple[str, ...]:
    return tuple(sorted(_norm_option(o, qtype) for o in (options_raw or [])))


def _answer_empty(item: dict) -> bool:
    return not str(item.get("answer") or "").strip()


def _sample(item: dict, *fields: str) -> dict[str, Any]:
    out = {"id": item.get("id")}
    for field in fields:
        value = item.get(field)
        if isinstance(value, str):
            value = value[:80]
        out[field] = value
    return out


def light_metrics(bank: Bank) -> dict[str, Any]:
    """不依赖快照的体检指标。全部基于当前内存题库，只读。"""
    items = bank.all()
    official = [i for i in items if i.get("source") == "official" and i.get("status") == "adopted"]
    official_empty = [i for i in official if _answer_empty(i)]

    by_skeleton: dict[tuple, list[dict]] = {}
    for item in official:
        if _answer_empty(item):
            continue
        key = (item["type"], text_key(item.get("title") or ""), _options(item.get("options"), item["type"]))
        by_skeleton.setdefault(key, []).append(item)

    skeleton_conflicts: list[dict[str, Any]] = []
    for members in by_skeleton.values():
        if len(members) < 2:
            continue
        semantics = {str(m.get("answer_norm") or "").strip() for m in members}
        semantics.discard("")
        if len(semantics) > 1:
            skeleton_conflicts.append({
                "type": members[0]["type"],
                "title": str(members[0].get("title") or "")[:80],
                "answers": sorted(semantics),
                "ids": [m.get("id") for m in members],
            })
    skeleton_conflicts.sort(key=lambda row: (row["type"], row["title"]))

    unexplained: list[dict] = []
    judge_letters: list[dict] = []
    for item in items:
        answer = str(item.get("answer") or "").strip()
        if not answer:
            continue
        parts = [p.strip().upper() for p in answer.split("#") if p.strip()]
        if not str(item.get("answer_norm") or "").strip():
            unexplained.append(item)
        if item["type"] == "judge" and parts and all(p in _LETTERS for p in parts):
            judge_letters.append(item)

    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(item.get("canonical_id") or item["id"], []).append(item)
    canonical_conflicts = []
    for cid, members in groups.items():
        if len(members) < 2:
            continue
        semantics = {str(m.get("answer_norm") or "").strip() for m in members}
        semantics.discard("")
        if len(semantics) > 1:
            canonical_conflicts.append({
                "canonical_id": cid,
                "type": members[0]["type"],
                "title": str(members[0].get("title") or "")[:80],
                "answers": sorted(semantics),
                "members": len(members),
            })

    return {
        "total": len(items),
        "official": len(official),
        "official_empty": {
            "count": len(official_empty),
            "samples": [_sample(i, "type", "title", "subject") for i in official_empty[:10]],
        },
        "official_skeleton_conflicts": {
            "count": len(skeleton_conflicts),
            "samples": skeleton_conflicts[:10],
        },
        "letter_unexplained": {
            "count": len(unexplained),
            "samples": [_sample(i, "type", "title", "answer") for i in unexplained[:10]],
        },
        "judge_letter_answers": {
            "count": len(judge_letters),
            "samples": [_sample(i, "type", "title", "answer") for i in judge_letters[:10]],
        },
        "canonical_semantic_conflicts": {
            "count": len(canonical_conflicts),
            "samples": canonical_conflicts[:10],
        },
    }


def _bank_signature(bank: Bank) -> tuple[int, float]:
    items = bank.all()
    latest = max((float(i.get("updated") or 0) for i in items), default=0.0)
    return len(items), round(latest, 3)


def _snapshot_signature() -> tuple[int, float]:
    folder = config.SNAPSHOT_DIR
    count = 0
    latest = 0.0
    try:
        names = [n for n in os.listdir(folder) if n.endswith(".html")]
    except OSError:
        return 0, 0.0
    for name in names:
        count += 1
        try:
            latest = max(latest, os.path.getmtime(os.path.join(folder, name)))
        except OSError:
            pass
    return count, round(latest, 0)


def cached_report(bank: Bank) -> dict[str, Any] | None:
    """只读缓存，不触发计算。签名不匹配或过期返回 None。"""
    signature = (_bank_signature(bank), _snapshot_signature())
    with _LOCK:
        report = _CACHE.get("report")
        if (
            report is not None
            and _CACHE.get("signature") == signature
            and time.time() - float(_CACHE.get("at") or 0) < _TTL_SECONDS
        ):
            return {**report, "cached": True}
    return None


def snapshot_report(bank: Bank, force: bool = False) -> dict[str, Any]:
    """跑一遍快照 hold-out 体检（秒级），结果按「题库 + 快照」签名缓存。"""
    signature = (_bank_signature(bank), _snapshot_signature())
    with _LOCK:
        report = _CACHE.get("report")
        if (
            not force
            and report is not None
            and _CACHE.get("signature") == signature
            and time.time() - float(_CACHE.get("at") or 0) < _TTL_SECONDS
        ):
            return {**report, "cached": True}
        result = _compute(bank)
        _CACHE.update({"signature": signature, "at": time.time(), "report": result})
        return {**result, "cached": False}


def _compute(bank: Bank) -> dict[str, Any]:
    started = time.time()
    cases, _files_seen = evaluation_mod.collect_cases()
    try:
        snapshot_files = len([n for n in os.listdir(config.SNAPSHOT_DIR) if n.endswith(".html")])
    except OSError:
        snapshot_files = 0
    imported = evaluation_mod.imported_ids()

    judge_groups: dict[str, set] = {}
    for case in cases.values():
        if case["type"] != "judge":
            continue
        judge_groups.setdefault(text_key(case["title"]), set()).update(case["truth"])
    ambiguous_judge = {key for key, truths in judge_groups.items() if len(truths) > 1}

    holdout: list[dict] = []
    excluded_imported = 0
    excluded_ambiguous = 0
    for case in cases.values():
        identity = (
            case["type"],
            _norm_text(case["title"]),
            tuple(sorted(str(o) for o in case["options"])),
        )
        if identity in imported:
            excluded_imported += 1
            continue
        if case["type"] == "judge" and text_key(case["title"]) in ambiguous_judge:
            excluded_ambiguous += 1
            continue
        holdout.append(case)

    official = [i for i in bank.all() if i.get("source") == "official"]

    def candidates(case: dict) -> list[dict]:
        return evaluation_mod.official_candidates(bank, case, official=official)

    def official_ok(item: dict, case: dict) -> bool:
        return evaluation_mod.official_answer_matches(item, case)

    matrix: dict[str, dict[str, int]] = {}
    paths: dict[str, dict[str, int]] = {}
    buckets: dict[str, dict[str, int]] = {}
    bad_rows: list[dict[str, Any]] = []
    ok = bad = miss = 0

    def bump(label: str, cand_ok: bool | None = None) -> None:
        row = buckets.setdefault(label, {"count": 0, "cand_ok": 0})
        row["count"] += 1
        if cand_ok:
            row["cand_ok"] += 1

    for case in holdout:
        subject = _pick_subject(case["title"], case["options"])
        hit = bank.find_official(case["type"], case["title"], case["options"], "")
        via = "find_official"
        if hit is None:
            local = bank.match(case["type"], case["title"], case["options"], subject, None)
            if local is not None and str(local.get("status") or "") != "pending":
                hit, via = local, "match"
            else:
                hit, via = None, "miss"

        answer = str((hit or {}).get("answer") or "")
        good = bool(answer) and evaluation_mod.answer_matches(answer, case)
        source = (str(hit.get("source") or "unknown") if hit else "miss")
        empty = bool(hit) and not answer

        path_row = paths.setdefault(via, {"ok": 0, "bad": 0})
        matrix_row = matrix.setdefault(source, {"ok": 0, "bad": 0, "empty": 0})
        if good:
            ok += 1
            path_row["ok"] += 1
            matrix_row["ok"] += 1
        elif hit is None:
            miss += 1
            path_row["bad"] += 1
            matrix_row["bad"] += 1
        else:
            bad += 1
            path_row["bad"] += 1
            matrix_row["bad"] += 1
            if empty:
                matrix_row["empty"] += 1

        if good:
            # 标签沿用原诊断脚本口径：match 路径带专业名
            label = "答对/" + ("match/subject=%s" % subject if via == "match" else via)
            bump(label)
        else:
            cands = candidates(case)
            cand_ok = any(official_ok(c, case) for c in cands)
            if hit is None:
                label = "未命中但有官方候选" if cands else "未命中且无官方"
            elif source == "official":
                label = "官方命中但答错"
            else:
                label = "命中非官方(%s)但答错%s" % (
                    source, "，库里有可达官方" if cands else "")
            bump(label, cand_ok)
            bad_rows.append({
                "type": case["type"],
                "title": str(case["title"] or "")[:80],
                "truth": " / ".join(str(t) for t in case["truth"])[:80],
                "answer": answer[:80],
                "source": source,
                "via": via,
                "match": (hit or {}).get("_match"),
                # 命中的是哪条记录：前端据此「点题干直达题库条目」；未命中时为空。
                "id": str((hit or {}).get("id") or ""),
                "subject": subject,
                "official_candidates": len(cands),
                "official_candidate_ok": cand_ok,
            })

    bad_rows.sort(key=lambda row: (
        0 if row["source"] == "official" else 1 if row["via"] != "miss" else 2,
        row["type"],
    ))

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_ms": int((time.time() - started) * 1000),
        "snapshot_files": snapshot_files,
        "holdout": len(holdout),
        "excluded": {"imported": excluded_imported, "ambiguous_judge": excluded_ambiguous},
        "summary": {"ok": ok, "bad": bad, "miss": miss},
        "matrix": matrix,
        "paths": paths,
        "buckets": [
            {"label": label, "count": row["count"], "cand_ok": row["cand_ok"]}
            for label, row in sorted(buckets.items(), key=lambda kv: -kv[1]["count"])
        ],
        "bad_examples": bad_rows[:20],
        "bad_total": len(bad_rows),
    }

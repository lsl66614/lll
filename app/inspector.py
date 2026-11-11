"""只读巡检器：把题库 / 答题 / 依赖的异常整理成可追踪的 findings。

一期定位是**只读监管**：
- 确定性检查产出 findings（级别 / 类别 / 证据 / 建议），不写题库、不改答案；
- 每次运行与上一次对比，标出「新增 / 恶化 / 已消失」；
- 结果落 ``inspections.json``，管理台「智能巡检」板块展示；
- P0 走现有通知通道（可在配置里关掉）。

AI 只负责把 findings 归纳成中文简报（``ai_summary``），不参与判定、不执行修复。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from . import checkup as checkup_mod
from .bank import Bank
from .ops import JsonStore

DEFAULT_CONFIG = {
    "enabled": True,
    "interval_hours": 6,
    "notify_p0": True,
}
_RUN_LIMIT = 50
_SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}


def _finding(
    finding_id: str,
    severity: str,
    category: str,
    title: str,
    metric: int | float,
    unit: str,
    evidence: list[str],
    suggestion: str,
    items: list | None = None,
) -> dict[str, Any]:
    return {
        "id": finding_id,
        "severity": severity,
        "category": category,
        "title": title,
        "metric": metric,
        "unit": unit,
        "evidence": [str(e)[:200] for e in evidence[:6]],
        "suggestion": suggestion,
        # 证据链：能在题库里定位到的记录 id（管理台据此「点一下直达条目」）。
        "items": [str(i) for i in (items or []) if i][:20],
        "status": "new",
    }


def diff_findings(previous: list[dict], current: list[dict]) -> dict[str, list]:
    """对比两次 findings：新增 / 恶化 / 好转 / 已消失。"""
    prev = {f["id"]: f for f in previous or []}
    curr = {f["id"]: f for f in current or []}
    new = [fid for fid in curr if fid not in prev]
    resolved = [
        {"id": fid, "title": prev[fid].get("title"), "severity": prev[fid].get("severity")}
        for fid in prev if fid not in curr
    ]
    worse = [
        fid for fid in curr
        if fid in prev and float(curr[fid].get("metric") or 0) > float(prev[fid].get("metric") or 0)
    ]
    better = [
        fid for fid in curr
        if fid in prev and float(curr[fid].get("metric") or 0) < float(prev[fid].get("metric") or 0)
    ]
    for fid, item in curr.items():
        if fid in new:
            item["status"] = "new"
        elif fid in worse:
            item["status"] = "worse"
        elif fid in better:
            item["status"] = "better"
        else:
            item["status"] = "same"
    return {"new": new, "worse": worse, "better": better, "resolved": resolved}


def collect_findings(
    *,
    bank: Bank,
    light: dict[str, Any],
    snapshot: dict[str, Any] | None,
    stats: dict[str, Any] | None = None,
    failures: list[dict[str, Any]] | None = None,
    probes: dict[str, Any] | None = None,
    prev_total: int | None = None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    items = bank.all() if hasattr(bank, "all") else []

    def add(*args, **kwargs) -> None:
        findings.append(_finding(*args, **kwargs))

    empty = light.get("official_empty") or {}
    if int(empty.get("count") or 0) > 0:
        samples = empty.get("samples") or []
        add(
            "official-empty", "P1", "题库数据", "官方记录存在空答案",
            int(empty.get("count") or 0), "条",
            ["[%s] %s" % (s.get("type"), str(s.get("title") or "")[:60]) for s in samples],
            "命中这些记录时脚本拿不到答案（网关已做空答案回落 AI）；建议人工补答案或降级来源。",
            items=[s.get("id") for s in samples],
        )

    skeleton = light.get("official_skeleton_conflicts") or {}
    if int(skeleton.get("count") or 0) > 0:
        samples = skeleton.get("samples") or []
        add(
            "official-skeleton-conflict", "P1", "题库数据", "同骨架官方答案不一致",
            int(skeleton.get("count") or 0), "组",
            ["[%s] %s — %s" % (s.get("type"), str(s.get("title") or "")[:50],
                               " ≠ ".join(map(str, s.get("answers") or []))) for s in samples],
            "可能是导入时答案取错，或同一题干的不同子问题；对照快照逐个核对，不要批量改写。",
            items=[i for s in samples for i in (s.get("ids") or [])],
        )

    unexplained = light.get("letter_unexplained") or {}
    if int(unexplained.get("count") or 0) > 0:
        samples = unexplained.get("samples") or []
        add(
            "letter-unexplained", "P0", "题库数据", "字母答案无法换算成语义",
            int(unexplained.get("count") or 0), "条",
            ["%s [%s] answer=%r" % (s.get("id"), s.get("type"), s.get("answer")) for s in samples],
            "这些答案越界或指向空选项，命中后会答错；先查选项是否被改过，再决定补答案还是降级。",
            items=[s.get("id") for s in samples],
        )

    judge_letters = light.get("judge_letter_answers") or {}
    if int(judge_letters.get("count") or 0) > 0:
        samples = judge_letters.get("samples") or []
        add(
            "judge-letter-answer", "P1", "题库数据", "判断题答案存成了字母",
            int(judge_letters.get("count") or 0), "条",
            ["%s answer=%r" % (s.get("id"), s.get("answer")) for s in samples],
            "判断题应存「正确/错误」；字母会随选项换序指向相反语义，建议按 options_norm 换算成语义词。",
            items=[s.get("id") for s in samples],
        )

    canon = light.get("canonical_semantic_conflicts") or {}
    if int(canon.get("count") or 0) > 0:
        samples = canon.get("samples") or []
        add(
            "canonical-semantic-conflict", "P1", "题库数据", "canonical 组内答案语义冲突",
            int(canon.get("count") or 0), "组",
            ["组 %s [%s] %s — %s" % (str(s.get("canonical_id") or "")[:8], s.get("type"),
                                      str(s.get("title") or "")[:40],
                                      " ≠ ".join(map(str, s.get("answers") or [])))
             for s in samples],
            "只报告不改写：先确认规范记录（实际出答案的那条）是否正确，再决定成员去留。",
            items=[s.get("canonical_id") for s in samples],
        )

    conflicts = [i for i in items if i.get("conflict")]
    if conflicts:
        add(
            "bank-conflicts", "P1", "答题效果", "题库存在冲突标记",
            len(conflicts), "条",
            ["%s [%s] %s" % (i.get("id"), i.get("type"), str(i.get("title") or "")[:50])
             for i in conflicts[:5]],
            "先跑「自动裁决」（官方同题 → 可信记忆 → 结果页快照 → 历史一致）；"
            "仍有残余再逐条人工确认。",
            items=[i.get("id") for i in conflicts[:20]],
        )

    pending = [i for i in items if str(i.get("status") or "") == "pending"]
    if pending:
        add(
            "pending-backlog", "P2", "答题效果", "待确认题积压",
            len(pending), "条", [],
            "持续增长说明 AI 答案迟迟凑不齐两次一致；可抽查是否有题目每次答案都不同。",
            items=[i.get("id") for i in pending[:20]],
        )

    if snapshot:
        if snapshot.get("error"):
            add(
                "snapshot-failed", "P2", "答题效果", "快照体检失败",
                1, "次", [str(snapshot.get("error"))[:160]],
                "检查 data/snapshots 是否完整、题库是否可读；失败期间其余检查仍然有效。",
            )
        else:
            matrix = snapshot.get("matrix") or {}
            rows = snapshot.get("summary") or {}
            official_bad = int((matrix.get("official") or {}).get("bad") or 0)
            if official_bad:
                examples = [
                    "%s %s：真值 %s / 网关 %s" % (
                        row.get("type"), str(row.get("title") or "")[:40],
                        row.get("truth"), row.get("answer") or "（空）")
                    for row in (snapshot.get("bad_examples") or [])
                    if row.get("source") == "official"
                ][:4]
                add(
                    "snapshot-official-wrong", "P1", "答题效果", "官方命中但答错",
                    official_bad, "条", examples,
                    "官方答案本身错或为空（导入缺陷）；用答错明细逐条核对，不要按快照批量回填。",
                    items=[
                        row.get("id") for row in (snapshot.get("bad_examples") or [])
                        if row.get("source") == "official"
                    ],
                )
            miss = int(rows.get("miss") or 0)
            if miss:
                add(
                    "snapshot-miss", "P2", "答题效果", "快照用例未命中题库",
                    miss, "条", [],
                    "未命中会转 AI，成本更高；看是否集中在填空题（模糊匹配本来就弱）。",
                )
            nonofficial_bad = sum(
                int((matrix.get(src) or {}).get("bad") or 0)
                for src in ("manual", "calibrated", "ai")
            )
            if nonofficial_bad:
                add(
                    "snapshot-nonofficial-wrong", "P2", "答题效果", "非官方命中但答错",
                    nonofficial_bad, "条", [],
                    "多为 AI 旧答案或校准遗留；优先处理官方候选里已有正确答案的条目。",
                    items=[
                        row.get("id") for row in (snapshot.get("bad_examples") or [])
                        if row.get("source") in ("manual", "calibrated", "ai")
                    ],
                )

    if stats:
        per = stats.get("per") or {}
        unhealthy = []
        for pid, row in per.items():
            calls = int(row.get("calls") or 0)
            fails = int(row.get("fails") or 0)
            if calls >= 10 and fails * 2 > calls:
                unhealthy.append("%s：调用 %d / 失败 %d" % (str(pid)[:8], calls, fails))
        if unhealthy:
            add(
                "provider-unhealthy", "P1", "外部依赖", "模型失败率偏高",
                len(unhealthy), "个", unhealthy[:5],
                "到「缓存与校准」看调用明细；失败率过半的模型会被单元投票排除，必要时禁用。",
            )

    if failures:
        cutoff = time.time() - 86400
        recent = [f for f in failures if float(f.get("at") or 0) >= cutoff]
        if len(recent) >= 50:
            codes: dict[int, int] = {}
            for item in recent:
                code = int(item.get("code") or 0)
                codes[code] = codes.get(code, 0) + 1
            top = sorted(codes.items(), key=lambda kv: -kv[1])[:3]
            add(
                "recent-failures", "P1", "外部依赖", "24 小时内未答错误偏多",
                len(recent), "条",
                ["错误码 %s：%d 次" % (code, count) for code, count in top],
                "到「未答题目」看明细；错误码 50001 多为模型失败，40005 多为答案与选项不匹配。",
            )
        elif len(recent) >= 10:
            add(
                "recent-failures", "P2", "外部依赖", "24 小时内有未答错误",
                len(recent), "条", [],
                "数量不大，留意是否集中在同一题型/模型。",
            )

    if probes:
        offline = [
            name for name, item in probes.items()
            if isinstance(item, dict) and item.get("online") is False
        ]
        if offline:
            add(
                "probe-offline", "P2", "外部依赖", "脚本探针离线",
                len(offline), "个", offline[:6],
                "可能是页面没打开或脚本未运行；如果正在用脚本答题，这个提示可以忽略。",
            )

    total = len(items)
    if prev_total and total < prev_total * 0.9:
        add(
            "bank-drop", "P0", "题库数据", "题库条数骤降",
            round((prev_total - total) / prev_total * 100), "%",
            ["上次 %d 条 → 现在 %d 条" % (prev_total, total)],
            "先查是不是导入了旧备份或去重删多了；在确认之前不要再执行去重/恢复。",
        )

    findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f["severity"], 9), -float(f["metric"] or 0)))
    return findings


class Inspector:
    """巡检记录的存储与运行（服务对象在调用时传入，避免测试互相污染）。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.store = JsonStore(path, {"config": dict(DEFAULT_CONFIG), "runs": []})
        self._run_lock = threading.Lock()

    def config(self) -> dict[str, Any]:
        data = self.store.read()
        merged = dict(DEFAULT_CONFIG)
        if isinstance(data.get("config"), dict):
            merged.update({k: v for k, v in data["config"].items() if k in DEFAULT_CONFIG})
        return merged

    def set_config(self, patch: Any) -> dict[str, Any]:
        current = self.config()
        if isinstance(patch, dict):
            for key in DEFAULT_CONFIG:
                if key in patch:
                    current[key] = patch[key]
        with self.store._lock:
            data = self.store.read()
            data["config"] = current
            self.store.write(data)
        return current

    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        data = self.store.read()
        return list(data.get("runs") or [])[: max(1, int(limit))]

    def latest(self) -> dict[str, Any] | None:
        runs = self.runs(1)
        return runs[0] if runs else None

    def is_due(self, now: float | None = None) -> bool:
        """是否到点该跑：启用且距上次运行达到间隔；从未跑过视为到点。"""
        cfg = self.config()
        if not cfg.get("enabled"):
            return False
        interval = max(1, int(cfg.get("interval_hours") or 6)) * 3600
        last_at = float((self.latest() or {}).get("at") or 0)
        return (time.time() if now is None else float(now)) - last_at >= interval

    def run(
        self,
        bank: Bank,
        *,
        include_snapshot: bool = True,
        stats: Any = None,
        failures: Any = None,
        probes: Any = None,
    ) -> dict[str, Any]:
        with self._run_lock:
            started = time.time()
            light = checkup_mod.light_metrics(bank)
            if include_snapshot:
                snapshot = checkup_mod.snapshot_report(bank)
            else:
                snapshot = checkup_mod.cached_report(bank)
            stats_snapshot = stats.snapshot() if stats is not None else None
            failure_items = failures.all() if failures is not None else None
            probe_items = probes.all() if probes is not None else None
            previous = self.latest()
            findings = collect_findings(
                bank=bank, light=light, snapshot=snapshot,
                stats=stats_snapshot, failures=failure_items, probes=probe_items,
                prev_total=int(previous.get("total") or 0) if previous else None,
            )
            diff = diff_findings(previous.get("findings") or [] if previous else [], findings)
            counts = {"P0": 0, "P1": 0, "P2": 0}
            for item in findings:
                counts[item["severity"]] = counts.get(item["severity"], 0) + 1
            run = {
                "at": time.time(),
                "elapsed_ms": int((time.time() - started) * 1000),
                "total": len(bank.all()),
                "counts": counts,
                "findings": findings,
                "diff": diff,
                "snapshot": {
                    "included": snapshot is not None and not snapshot.get("error"),
                    "cached": bool((snapshot or {}).get("cached")),
                    "holdout": int((snapshot or {}).get("holdout") or 0),
                    "summary": (snapshot or {}).get("summary"),
                },
                "ai_summary": "",
            }
            with self.store._lock:
                data = self.store.read()
                runs = [run] + list(data.get("runs") or [])
                data["runs"] = runs[:_RUN_LIMIT]
                self.store.write(data)
            return run

    def set_summary(self, at: float, text: str) -> bool:
        with self.store._lock:
            data = self.store.read()
            runs = list(data.get("runs") or [])
            for item in runs:
                if abs(float(item.get("at") or 0) - float(at or 0)) < 0.001:
                    item["ai_summary"] = str(text or "")[:4000]
                    self.store.write(data)
                    return True
        return False

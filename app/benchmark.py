"""盲测基准：用例加载、答案判定、汇总与 Markdown 报告。

这个模块不访问网络，也不依赖网关进程。实际请求由调用方通过 ``ask`` 回调
注入，便于离线测试与在脚本里替换成 httpx。

盲测集的要求写在 ``benchmark/README.md``：题目必须来自没有参与题库建设、
缓存和记忆的课程；答案先冻结，再跑网关。用已有题库回放只能算回归测试。
"""

from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Sequence

from .evaluation import L as _LETTERS
from .evaluation import answer_matches, norm_option
from .parsing import normalize_type, parse_options
from .reliability import ABSTAIN_CODE

REQUIRED_FIELDS = ("type", "title", "answer")
_LETTER_RE = re.compile(r"[A-Z]")
_JUDGE_TRUE = {"对", "正确", "√", "✓", "是", "true", "t", "yes", "y"}
_JUDGE_FALSE = {"错", "错误", "×", "✗", "否", "not", "false", "f", "no", "n"}


def _canon_judge(text: str) -> str:
    value = str(text or "").strip().lower()
    if value in _JUDGE_FALSE or "错误" in value or "不正确" in value:
        return "错误"
    if value in _JUDGE_TRUE or "正确" in value:
        return "正确"
    return str(text or "").strip()


def _letters(answer: str) -> list[str]:
    seen: list[str] = []
    for char in _LETTER_RE.findall(str(answer or "").upper()):
        if char not in seen:
            seen.append(char)
    return seen


def _truth_from_letters(answer: str, options: list[str]) -> tuple[str, ...]:
    picked: list[str] = []
    for char in _letters(answer):
        index = _LETTERS.find(char)
        if index < 0 or index >= len(options):
            raise ValueError(f"答案 {answer!r} 的字母 {char} 超出选项范围")
        picked.append(norm_option(options[index]))
    return tuple(sorted(picked))


def _truth_from_text(answer: str, options: list[str]) -> tuple[str, ...] | None:
    target = norm_option(answer)
    for option in options:
        if norm_option(option) == target:
            return (target,)
    return None


def normalize_case(raw: Any, index: int = 1) -> dict[str, Any]:
    """校验一条用例并补上 ``truth``（与 ``app.evaluation`` 同口径）。"""
    if not isinstance(raw, dict):
        raise ValueError(f"第 {index} 条用例不是对象")
    missing = [key for key in REQUIRED_FIELDS if not str(raw.get(key) or "").strip()]
    if missing:
        raise ValueError(f"第 {index} 条用例缺少字段：{', '.join(missing)}")

    qtype = normalize_type(raw.get("type"))
    if qtype is None:
        raise ValueError(f"第 {index} 条用例的题型无法识别：{raw.get('type')!r}")

    options = parse_options(raw.get("options"))
    raw_answer = raw.get("answer")
    if isinstance(raw_answer, (list, tuple)):
        answer = "#".join(str(item).strip() for item in raw_answer if str(item).strip())
    else:
        answer = str(raw_answer or "").strip()
    if not answer:
        raise ValueError(f"第 {index} 条用例答案为空")

    if qtype in ("single", "multiple", "judge"):
        letters = _letters(answer)
        if letters and options:
            truth = _truth_from_letters(answer, options)
        elif qtype == "judge":
            truth = (_canon_judge(answer),)
        else:
            truth = _truth_from_text(answer, options) or ()
        if not truth:
            raise ValueError(f"第 {index} 条用例的答案无法映射到选项：{answer!r}")
    else:
        truth = (answer,)

    title = str(raw.get("title") or "").strip()
    return {
        "id": str(raw.get("id") or f"q{index}").strip(),
        "type": qtype,
        "title": title,
        "options": options,
        "answer": answer,
        "truth": truth,
        "source": str(raw.get("source") or "").strip(),
        "subject": str(raw.get("subject") or "").strip(),
        "context": str(raw.get("context") or "").strip(),
        "images": list(raw.get("images") or []),
        "note": str(raw.get("note") or "").strip(),
    }


def load_cases(path: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """读取 JSON 用例集；支持顶层数组或 ``{"questions": [...]}``。"""
    with open(path, "r", encoding="utf-8") as fp:
        payload = json.load(fp)
    if isinstance(payload, list):
        raw_cases, meta = payload, {}
    elif isinstance(payload, dict):
        raw_cases, meta = payload.get("questions"), {
            key: value for key, value in payload.items() if key != "questions"
        }
    else:
        raise ValueError("用例文件必须是数组或包含 questions 的对象")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("用例文件里没有题目")

    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_cases, 1):
        case = normalize_case(raw, index)
        if case["id"] in seen:
            raise ValueError(f"用例 id 重复：{case['id']}")
        seen.add(case["id"])
        cases.append(case)
    return cases, meta


def judge_payload(
    case: dict[str, Any], payload: Any, *, elapsed_ms: int = 0
) -> dict[str, Any]:
    """把一次网关响应判成 correct / wrong / abstained / failed / empty。"""
    result: dict[str, Any] = {
        "id": case["id"],
        "type": case["type"],
        "elapsed_ms": max(0, int(elapsed_ms or 0)),
        "answer": "",
        "source": "",
        "provider": "",
        "reliability": "",
        "reason": "",
        "error": "",
    }
    if not isinstance(payload, dict):
        result["outcome"] = "error"
        result["error"] = "响应不是 JSON 对象"
        return result

    try:
        code = int(payload.get("code") or 0)
    except (TypeError, ValueError):
        code = -1
    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}

    result["answer"] = str(data.get("answer") or "").strip()
    result["source"] = str(data.get("source") or "").strip()
    result["provider"] = str(data.get("provider") or "").strip()
    result["reliability"] = str(data.get("reliability") or "").strip()
    result["reason"] = str(data.get("reliability_reason") or "").strip()

    if code == ABSTAIN_CODE:
        result["outcome"] = "abstained"
        result["error"] = str(payload.get("message") or payload.get("msg") or "弃答")
        return result
    if code != 0:
        result["outcome"] = "failed"
        result["error"] = str(payload.get("message") or payload.get("msg") or f"code={code}")
        return result
    if not result["answer"]:
        result["outcome"] = "empty"
        result["error"] = "响应没有答案"
        return result

    result["outcome"] = "correct" if answer_matches(result["answer"], case) else "wrong"
    if not result["error"]:
        result["elapsed_ms"] = max(0, int(data.get("elapsed_ms") or elapsed_ms or 0))
    return result


def run_cases(
    cases: Sequence[dict[str, Any]],
    ask: Callable[[dict[str, Any]], Any],
    *,
    concurrency: int = 4,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """并发执行用例；``ask`` 返回网关 JSON，异常记为 error。"""
    workers = max(1, min(int(concurrency or 1), max(1, len(cases))))

    def one(case: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            payload = ask(case)
        except Exception as exc:  # noqa: BLE001 - 单题失败不中断整批
            return {
                "id": case["id"], "type": case["type"], "outcome": "error",
                "error": f"{type(exc).__name__}: {exc}", "elapsed_ms": 0,
                "answer": "", "source": "", "provider": "", "reliability": "",
                "reason": "",
            }
        elapsed = int((time.perf_counter() - started) * 1000)
        result = judge_payload(case, payload, elapsed_ms=elapsed)
        if progress:
            progress(result)
        return result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, cases))


def _percentile(values: Sequence[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return float(ordered[index])


def _bucket(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    counts = {"total": len(results), "correct": 0, "wrong": 0,
              "abstained": 0, "failed": 0, "empty": 0, "error": 0}
    for row in results:
        outcome = str(row.get("outcome") or "error")
        counts[outcome] = counts.get(outcome, 0) + 1
    solved = counts["correct"] + counts["wrong"]
    counts["solved"] = solved
    counts["accuracy"] = round(counts["correct"] / solved, 4) if solved else None
    counts["coverage"] = round(solved / counts["total"], 4) if counts["total"] else 0.0
    counts["abstain_rate"] = round(counts["abstained"] / counts["total"], 4) if counts["total"] else 0.0
    return counts


def summarize(
    results: Sequence[dict[str, Any]],
    *,
    meta: dict[str, Any] | None = None,
    started: float | None = None,
) -> dict[str, Any]:
    """按整体、题型、来源汇总；准确率的分母是有效作答（正确 + 错误）。"""
    rows = list(results)
    by_type: dict[str, list[dict[str, Any]]] = {}
    by_source: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_type.setdefault(str(row.get("type") or "unknown"), []).append(row)
        by_source.setdefault(str(row.get("source") or "unknown"), []).append(row)

    latencies = [float(row.get("elapsed_ms") or 0) for row in rows if row.get("elapsed_ms")]
    summary: dict[str, Any] = {
        "meta": dict(meta or {}),
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_seconds": round(time.time() - started, 1) if started else None,
        "overall": _bucket(rows),
        "by_type": {key: _bucket(value) for key, value in sorted(by_type.items())},
        "by_source": {
            key: _bucket(value) for key, value in sorted(
                by_source.items(), key=lambda item: (-len(item[1]), item[0])
            )
        },
        "latency_ms": {
            "p50": round(_percentile(latencies, 0.50), 1) if latencies else 0.0,
            "p95": round(_percentile(latencies, 0.95), 1) if latencies else 0.0,
            "max": round(max(latencies), 1) if latencies else 0.0,
        },
    }
    return summary


def _pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.1f}%"


def render_markdown(summary: dict[str, Any]) -> str:
    """把汇总渲染成一页 Markdown。"""
    meta = summary.get("meta") or {}
    overall = summary.get("overall") or {}
    lines = [
        "# 答题盲测报告",
        "",
        f"- 时间：{summary.get('generated', '')}",
        f"- 用例文件：{meta.get('file', '-')}",
        f"- 网关：{meta.get('base_url', '-')}",
        f"- 并发：{meta.get('concurrency', '-')}",
        f"- 耗时：{summary.get('duration_seconds') or '-'} 秒",
        "",
        "## 总览",
        "",
        "| 总题数 | 有效作答 | 正确 | 错误 | 弃答 | 失败 | 空答案 | 准确率（有效） | 覆盖率 |",
        "|---|---|---|---|---|---|---|---|---|",
        f"| {overall.get('total', 0)} | {overall.get('solved', 0)} | {overall.get('correct', 0)} "
        f"| {overall.get('wrong', 0)} | {overall.get('abstained', 0)} | {overall.get('failed', 0)} "
        f"| {overall.get('empty', 0)} | {_pct(overall.get('accuracy'))} | {_pct(overall.get('coverage'))} |",
        "",
        "## 按题型",
        "",
        "| 题型 | 总数 | 有效 | 正确 | 错误 | 弃答 | 失败 | 准确率 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for key, row in (summary.get("by_type") or {}).items():
        lines.append(
            f"| {key} | {row.get('total', 0)} | {row.get('solved', 0)} | {row.get('correct', 0)} "
            f"| {row.get('wrong', 0)} | {row.get('abstained', 0)} | {row.get('failed', 0)} "
            f"| {_pct(row.get('accuracy'))} |"
        )
    lines += [
        "",
        "## 按答案来源",
        "",
        "| 来源 | 总数 | 有效 | 正确 | 错误 | 准确率 |",
        "|---|---|---|---|---|---|",
    ]
    for key, row in (summary.get("by_source") or {}).items():
        lines.append(
            f"| {key} | {row.get('total', 0)} | {row.get('solved', 0)} | {row.get('correct', 0)} "
            f"| {row.get('wrong', 0)} | {_pct(row.get('accuracy'))} |"
        )
    latency = summary.get("latency_ms") or {}
    lines += [
        "",
        "## 延迟",
        "",
        f"- p50：{latency.get('p50', 0)} ms",
        f"- p95：{latency.get('p95', 0)} ms",
        f"- max：{latency.get('max', 0)} ms",
        "",
        "## 口径",
        "",
        "- 准确率 = 正确 / 有效作答；弃答、失败、空答案不计入分母。",
        "- 覆盖率 = 有效作答 / 总题数。",
        "- 填空题比对沿用 `app.evaluation.judge_completion`：精确、数学等价、数值容差、前缀四档。",
        "- 本报告只反映这一批用例；换成新课程或新批次需要重新收集。",
        "",
    ]
    return "\n".join(lines)


def write_reports(
    summary: dict[str, Any],
    results: Sequence[dict[str, Any]],
    out_dir: str,
    *,
    prefix: str = "benchmark",
) -> tuple[str, str]:
    """写出 JSON 与 Markdown 两份报告，返回路径。"""
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join(out_dir, f"{prefix}-{stamp}")
    json_path, md_path = base + ".json", base + ".md"
    with open(json_path, "w", encoding="utf-8") as fp:
        json.dump({"summary": summary, "results": list(results)}, fp,
                  ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as fp:
        fp.write(render_markdown(summary))
    return json_path, md_path

"""单元模式首答准确率实测：把题目集逐条 POST 给影子网关，按标准答案判分。

用法（在项目根目录）：
    .venv/Scripts/python.exe tools/eval_run.py --base-url http://127.0.0.1:8010

说明：
- /api/scene 的 TTL 是 20 分钟且 get 不续期，所以每题前都重设一次 scene=unit；
- 判分用项目自身的 canonical_answer，与网关内部投票口径保持一致；
- 网络/上游错误不计入准确率分母，单独统计失败数。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.quality import canonical_answer  # noqa: E402

TYPE_NAMES = {
    "single": "单选",
    "multiple": "多选",
    "judge": "判断",
    "completion": "填空",
}


def post_json(url: str, payload: dict, timeout: float = 180.0) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def get_json(url: str, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def wait_ready(base: str, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            data = get_json(base + "/health")
            if data.get("status") in ("ok", "no_provider"):
                return
            last = str(data)
        except Exception as exc:  # noqa: BLE001
            last = repr(exc)
        time.sleep(1.0)
    raise RuntimeError(f"网关未就绪: {last}")


def data_url(path: str) -> str:
    with open(path, "rb") as fp:
        raw = fp.read()
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def is_correct(qtype: str, expected: str, got: str) -> bool:
    if not got:
        return False
    return canonical_answer(qtype, expected) == canonical_answer(qtype, got)


def load_questions(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fp:
        data = json.load(fp)
    return data["questions"]


def slim(data: dict) -> dict:
    keys = (
        "answer", "provider", "consensus", "share", "risk", "scene",
        "elapsed_ms", "confidence", "cached", "official", "official_check",
        "vote", "type", "source",
    )
    return {k: data.get(k) for k in keys if k in data}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument(
        "--questions",
        default=os.path.join(ROOT, "tools", "eval_questions_20261110.json"),
    )
    parser.add_argument("--outdir", default=os.path.join(ROOT, "reports"))
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--only", default="")
    parser.add_argument("--assets-root", default=ROOT)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    questions = load_questions(args.questions)
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        questions = [q for q in questions if q["id"] in wanted]
    if args.limit:
        questions = questions[: args.limit]

    wait_ready(base)
    print(f"网关就绪: {base}，共 {len(questions)} 题", flush=True)

    results: list[dict] = []
    started = time.time()
    for idx, q in enumerate(questions, 1):
        qid, qtype = q["id"], q["type"]
        # 每题重设 scene，避免 20 分钟 TTL 过期后退回 quick
        try:
            post_json(base + "/api/scene", {"scene": "unit", "url": "eval-20261110"}, 10.0)
        except Exception as exc:  # noqa: BLE001
            print(f"[{idx}/{len(questions)}] {qid} 设置 scene 失败: {exc!r}", flush=True)

        payload = {"title": q["title"], "type": qtype}
        if q.get("options"):
            payload["options"] = q["options"]
        image_files = q.get("image_files") or []
        if image_files:
            payload["images"] = [
                data_url(os.path.join(args.assets_root, rel)) for rel in image_files
            ]

        t0 = time.time()
        data: dict = {}
        error = ""
        code = None
        for attempt in (1, 2):
            try:
                resp = post_json(base + "/query", payload, args.timeout)
                code = resp.get("code")
                if code == 0 and isinstance(resp.get("data"), dict):
                    data = resp["data"]
                    error = ""
                    break
                error = str(resp.get("message") or resp)
            except Exception as exc:  # noqa: BLE001
                error = repr(exc)
            if attempt == 1:
                time.sleep(3.0)
        elapsed = round(time.time() - t0, 2)

        got = str(data.get("answer") or "")
        ok = is_correct(qtype, q["answer"], got) if data else None
        vote = data.get("vote") or {}
        row = {
            "id": qid,
            "type": qtype,
            "group": q.get("group", ""),
            "expected": q["answer"],
            "got": got,
            "correct": ok,
            "code": code,
            "error": error,
            "wall_s": elapsed,
            "source": q.get("source", ""),
            "detail": slim(data),
        }
        results.append(row)
        mark = "✓" if ok else ("✗" if ok is False else "!")
        print(
            f"[{idx}/{len(questions)}] {qid} {mark} got={got!r} exp={q['answer']!r} "
            f"share={vote.get('share')} rounds={vote.get('rounds')} "
            f"models={vote.get('models')} consensus={data.get('consensus')!r} "
            f"{elapsed}s {error}",
            flush=True,
        )
        if idx < len(questions) and args.delay > 0:
            time.sleep(args.delay)

    total_s = round(time.time() - started, 1)

    # ---------------- 统计 ----------------
    by_type: dict[str, dict] = defaultdict(lambda: {"valid": 0, "correct": 0, "failed": 0})
    for row in results:
        bucket = by_type[row["type"]]
        if row["correct"] is None:
            bucket["failed"] += 1
        else:
            bucket["valid"] += 1
            bucket["correct"] += 1 if row["correct"] else 0

    by_group: dict[str, dict] = defaultdict(lambda: {"valid": 0, "correct": 0, "failed": 0})
    for row in results:
        if not row.get("group"):
            continue
        bucket = by_group[row["group"]]
        if row["correct"] is None:
            bucket["failed"] += 1
        else:
            bucket["valid"] += 1
            bucket["correct"] += 1 if row["correct"] else 0

    def rate(c: int, n: int) -> float:
        return round(c / n * 100, 1) if n else 0.0

    valid = sum(b["valid"] for b in by_type.values())
    correct = sum(b["correct"] for b in by_type.values())
    failed = sum(b["failed"] for b in by_type.values())
    summary = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_url": base,
        "total_questions": len(results),
        "valid": valid,
        "correct": correct,
        "wrong": valid - correct,
        "failed": failed,
        "accuracy_pct": rate(correct, valid),
        "elapsed_s": total_s,
        "by_type": {
            k: {
                "name": TYPE_NAMES.get(k, k),
                "valid": v["valid"],
                "correct": v["correct"],
                "wrong": v["valid"] - v["correct"],
                "failed": v["failed"],
                "accuracy_pct": rate(v["correct"], v["valid"]),
            }
            for k, v in by_type.items()
        },
        "by_group": {
            k: {
                "valid": v["valid"],
                "correct": v["correct"],
                "wrong": v["valid"] - v["correct"],
                "failed": v["failed"],
                "accuracy_pct": rate(v["correct"], v["valid"]),
            }
            for k, v in by_group.items()
        },
        "model_votes": sum(
            sum(((r["detail"].get("vote") or {}).get("votes") or {}).values())
            for r in results
        ),
    }

    os.makedirs(args.outdir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = os.path.join(args.outdir, f"eval-unit-{stamp}.json")
    md_path = os.path.join(args.outdir, f"eval-unit-{stamp}.md")
    with open(json_path, "w", encoding="utf-8") as fp:
        json.dump({"summary": summary, "results": results}, fp, ensure_ascii=False, indent=2)

    lines = [
        "# 单元模式首答准确率实测",
        "",
        f"- 时间：{summary['generated']}",
        f"- 网关：{base}（影子实例，空题库/空缓存/空记忆）",
        f"- 总题数：{summary['total_questions']}，有效作答 {valid}，失败 {failed}",
        f"- 总正确率：**{summary['accuracy_pct']}%**（{correct}/{valid}）",
        f"- 总耗时：{total_s}s",
        "",
        "| 题型 | 有效 | 正确 | 错误 | 失败 | 正确率 |",
        "|---|---|---|---|---|---|",
    ]
    for key in ("single", "multiple", "judge", "completion"):
        if key not in by_type:
            continue
        b = summary["by_type"][key]
        lines.append(
            f"| {b['name']} | {b['valid']} | {b['correct']} | {b['wrong']} | "
            f"{b['failed']} | {b['accuracy_pct']}% |"
        )
    if by_group:
        lines += ["", "| 分组 | 有效 | 正确 | 错误 | 失败 | 正确率 |", "|---|---|---|---|---|---|"]
        for key, b in summary["by_group"].items():
            lines.append(
                f"| {key} | {b['valid']} | {b['correct']} | {b['wrong']} | "
                f"{b['failed']} | {b['accuracy_pct']}% |"
            )
    lines += ["", "## 逐题明细", "", "| 题号 | 题型 | 标准答案 | 网关答案 | 结果 | share | 轮次 | 模型数 | 耗时 |", "|---|---|---|---|---|---|---|---|---|"]
    for row in results:
        vote = row["detail"].get("vote") or {}
        mark = "✓" if row["correct"] else ("✗" if row["correct"] is False else "失败")
        got = row["got"] or (row["error"][:40] if row["error"] else "")
        lines.append(
            f"| {row['id']} | {TYPE_NAMES.get(row['type'], row['type'])} | {row['expected']} | "
            f"{got} | {mark} | {vote.get('share')} | {vote.get('rounds')} | "
            f"{vote.get('models')} | {row['wall_s']}s |"
        )
    with open(md_path, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")

    print("\n==== 汇总 ====")
    print(f"有效 {valid} / 失败 {failed}，正确率 {summary['accuracy_pct']}% ({correct}/{valid})")
    for key, b in summary["by_type"].items():
        print(f"  {b['name']}: {b['correct']}/{b['valid']} = {b['accuracy_pct']}%")
    for key, b in summary["by_group"].items():
        print(f"  [{key}]: {b['correct']}/{b['valid']} = {b['accuracy_pct']}%")
    print(f"报告: {json_path}")
    print(f"报告: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

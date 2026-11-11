"""跑一次答题盲测，输出 JSON 与 Markdown 报告。

用法：
    uv run python scripts/run_benchmark.py benchmark/sample.json
    uv run python scripts/run_benchmark.py benchmark/blind-2026-11.json --concurrency 4
    uv run python scripts/run_benchmark.py benchmark/blind.json --fail-under 0.9

盲测集必须来自没有参与题库、缓存和记忆建设的课程。用已有题库回放只能算
回归测试，不能当作准确率。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app import benchmark  # noqa: E402


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OCS 网关答题盲测")
    parser.add_argument("file", help="用例 JSON 文件")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="网关地址")
    parser.add_argument("--token", default=os.getenv("GATEWAY_TOKEN", ""), help="网关口令")
    parser.add_argument("--subject", default="", help="覆盖用例里的专业名")
    parser.add_argument("--context", default="", help="覆盖用例里的上下文")
    parser.add_argument("--concurrency", type=int, default=4, help="并发请求数")
    parser.add_argument("--timeout", type=float, default=120.0, help="单题超时（秒）")
    parser.add_argument("--out-dir", default="reports", help="报告输出目录")
    parser.add_argument("--prefix", default="benchmark", help="报告文件名前缀")
    parser.add_argument("--max-cases", type=int, default=0, help="只跑前 N 题；0 表示全部")
    parser.add_argument("--fail-under", type=float, default=None, help="准确率低于该值时退出码 2")
    parser.add_argument("--dry-run", action="store_true", help="只校验用例，不发请求")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        cases, meta = benchmark.load_cases(args.file)
    except (OSError, ValueError) as exc:
        print(f"用例文件不合法：{exc}")
        return 1
    if args.max_cases > 0:
        cases = cases[: args.max_cases]
    meta = dict(meta, file=os.path.basename(args.file), base_url=args.base_url,
                concurrency=args.concurrency, cases=len(cases))
    if args.dry_run:
        print(f"用例校验通过：{len(cases)} 题")
        for case in cases:
            print(f"  {case['id']:>8}  {case['type']:<10} {case['title'][:60]}")
        return 0

    headers = {"Content-Type": "application/json"}
    if args.token:
        headers["X-Gateway-Token"] = args.token

    done = 0
    lock = threading.Lock()
    total = len(cases)

    def progress(result: dict) -> None:
        nonlocal done
        with lock:
            done += 1
            print(f"\r  进度 {done}/{total}  {result.get('id')} {result.get('outcome')}",
                  end="", flush=True)

    with httpx.Client(timeout=args.timeout, headers=headers) as client:
        def ask(case: dict) -> dict:
            payload = {
                "title": case["title"],
                "type": case["type"],
                "options": case["options"],
            }
            subject = args.subject or case.get("subject") or ""
            context = args.context or case.get("context") or ""
            if subject:
                payload["subject"] = subject
            if context:
                payload["context"] = context
            if case.get("images"):
                payload["images"] = case["images"]
            response = client.post(args.base_url.rstrip("/") + "/query", json=payload)
            response.raise_for_status()
            return response.json()

        started = time.time()
        try:
            results = benchmark.run_cases(
                cases, ask, concurrency=args.concurrency, progress=progress
            )
        except httpx.ConnectError:
            print("\n连不上网关。先启动 start.bat，或检查 --base-url。")
            return 2
        except httpx.TimeoutException:
            print("\n请求超时。调大 --timeout，或检查上游模型。")
            return 2
        finally:
            if done:
                print()

    summary = benchmark.summarize(results, meta=meta, started=started)
    json_path, md_path = benchmark.write_reports(
        summary, results, args.out_dir, prefix=args.prefix
    )

    overall = summary["overall"]
    print(f"总题数 {overall['total']} · 有效 {overall['solved']} · 正确 {overall['correct']} "
          f"· 错误 {overall['wrong']} · 弃答 {overall['abstained']} · 失败 {overall['failed']}")
    print(f"准确率（有效作答）{_pct(overall['accuracy'])} · "
          f"覆盖率 {_pct(overall['coverage'])}")
    print(f"报告：{json_path}")
    print(f"      {md_path}")

    if args.fail_under is not None:
        accuracy = overall.get("accuracy")
        if accuracy is None or accuracy < args.fail_under:
            print(f"准确率低于阈值 {args.fail_under:.1%}")
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""依次重试「未答题目」里可恢复的题。

每道题直接复用网关的 ``POST /query``；成功返回后，网关会通过
``FailureLog.resolve`` 自动删掉对应的失败记录。脚本只做调度和进度输出。

用法：
    python scripts/retry_failures.py
    python scripts/retry_failures.py --limit 5 --timeout 240
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("GATEWAY_TOKEN", "")
    if token:
        headers["X-Gateway-Token"] = token
    return headers


def _get_json(url: str, timeout: float = 15) -> dict:
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="批量重试未答题目")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--limit", type=int, default=0, help="只重试前 N 道（0 = 全部）")
    parser.add_argument("--timeout", type=float, default=240.0, help="单题 HTTP 超时秒数")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    items = _get_json(base + "/api/failures").get("items") or []
    todo = [item for item in items if item.get("retryable")]
    if args.limit > 0:
        todo = todo[: args.limit]
    print(f"start: {len(items)} questions, {len(todo)} retryable", flush=True)
    if not todo:
        print("done: nothing to retry", flush=True)
        return 0

    ok = 0
    bad = 0
    for index, item in enumerate(todo, 1):
        title = str(item.get("title") or "")
        label = " ".join(title.split())[:70]
        print(f"[{index}/{len(todo)}] START {label}", flush=True)
        started = time.time()
        try:
            result = _post_json(
                base + "/query",
                {
                    "title": title,
                    "type": item.get("type") or "single",
                    "options": item.get("options") or [],
                    "subject": item.get("subject") or "",
                },
                args.timeout,
            )
        except Exception as exc:  # noqa: BLE001 - 单题失败不能中断整批
            bad += 1
            print(f"[{index}/{len(todo)}] ERROR {type(exc).__name__}: {exc}", flush=True)
            continue

        elapsed = time.time() - started
        data = result.get("data") or {}
        answer = str(data.get("answer") or "").strip()
        if result.get("code") == 0 and answer:
            ok += 1
            print(
                f"[{index}/{len(todo)}] OK {elapsed:.1f}s answer={answer!r} "
                f"source={data.get('source')} provider={data.get('provider')}",
                flush=True,
            )
        else:
            bad += 1
            message = str(result.get("message") or "")
            print(
                f"[{index}/{len(todo)}] FAIL {elapsed:.1f}s "
                f"code={result.get('code')} message={message[:180]}",
                flush=True,
            )

    remaining = _get_json(base + "/api/failures")
    print(
        f"done: ok={ok} bad={bad}; remaining={remaining.get('count')} questions / "
        f"{remaining.get('events')} events",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())

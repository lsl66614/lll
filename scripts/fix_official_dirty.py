"""一次性修复：11 条判断题坏选项 + 1 条无图残题答案（2026-11-10）。

通过运行中网关的 HTTP API 修改，避免外部进程写库被网关内存态覆盖。
只改目标字段；题干、来源、状态保持不变。

- 判断题坏选项：``['正确','正确']`` → ``['正确','错误']``（答案语义不变）
- 残题 ``eb16b7eb``：无图题干只剩残句，答案「正确」与带图版
  ``8847f082``（「错误」，经 OCR 核验命题为假）冲突；只改答案，不补题干
  （补题干会让无图查询不再命中官方记录，反而失去正确答案）。
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"

JUDGE_BROKEN = [
    "e8b5cecb", "d992b428", "ffee420c", "b7cc7629", "0f6bfeb1",
    "2c83d47a", "ea7bbcd9", "7c7942fd", "08cf1e82", "0bc3c640",
    "b2035db0",
]
RESIDUAL = "eb16b7eb"


def get_item(item_id: str) -> dict:
    with urllib.request.urlopen(f"{BASE}/api/bank/item/{item_id}", timeout=15) as resp:
        return json.load(resp)


def put_item(item_id: str, body: dict) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/api/bank/{item_id}", data=data, method="PUT",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main() -> int:
    failures: list[tuple[str, str]] = []
    changed = 0

    for item_id in JUDGE_BROKEN:
        try:
            item = get_item(item_id)
        except Exception as exc:  # noqa: BLE001
            failures.append((item_id, f"GET 失败：{exc}"))
            continue
        answer = str(item.get("answer") or "").strip()
        if item.get("type") != "judge" or item.get("source") != "official":
            failures.append((item_id, f"非官方判断题：type={item.get('type')} source={item.get('source')}"))
            continue
        if answer not in ("正确", "错误"):
            failures.append((item_id, f"答案不是判断题语义：{answer!r}"))
            continue
        body = {"type": "judge", "title": item["title"],
                "options": ["正确", "错误"], "answer": answer}
        try:
            out = put_item(item_id, body)
        except Exception as exc:  # noqa: BLE001
            failures.append((item_id, f"PUT 失败：{exc}"))
            continue
        if (out.get("options") == ["正确", "错误"] and out.get("answer") == answer
                and out.get("source") == "official" and out.get("status") == "adopted"):
            changed += 1
            print(f"OK  {item_id} judge options -> ['正确','错误'] answer={answer}")
        else:
            failures.append((item_id, f"回写校验不通过：{out.get('options')} {out.get('answer')}"))

    try:
        item = get_item(RESIDUAL)
        body = {"type": item["type"], "title": item["title"],
                "options": item.get("options"), "answer": "错误", "conflict": False}
        out = put_item(RESIDUAL, body)
        if out.get("answer") == "错误" and out.get("source") == "official":
            changed += 1
            print(f"OK  {RESIDUAL} judge answer 正确 -> 错误（题干保持不动）")
        else:
            failures.append((RESIDUAL, f"回写校验不通过：{out.get('answer')}"))
    except Exception as exc:  # noqa: BLE001
        failures.append((RESIDUAL, f"处理失败：{exc}"))

    print(f"changed={changed} failed={len(failures)}")
    for item_id, reason in failures:
        print(f"FAIL {item_id}: {reason}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())

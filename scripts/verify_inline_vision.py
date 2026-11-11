"""视觉升级的 A/B 验证脚本（可复现上面的 0/6 → 5/6）。

用法：
    .venv\\Scripts\\python.exe scripts/verify_inline_vision.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config  # noqa: E402
from app.bank import Bank  # noqa: E402
from app.inline_images import extract_urls, fetch_inline_images  # noqa: E402
from app.llm import call_provider  # noqa: E402
from app.parsing import build_messages  # noqa: E402


def providers() -> list[dict]:
    data = json.load(open(os.path.join(config.DATA_DIR, "providers.json"), encoding="utf-8"))
    items = data if isinstance(data, list) else data.get("providers", data.get("items", []))
    return [p for p in items if p.get("enabled")]


def short(text: str) -> str:
    return re.sub(r"https?://\S+", "[图]", text)[:66]


async def ask(provider, title, with_images: bool):
    msgs = build_messages("completion", title, ["请输入答案"], None, "", "")
    if with_images:
        urls = extract_urls([title])
        data_urls = await fetch_inline_images(urls)
        if data_urls:
            content = [{"type": "text", "text": msgs[1]["content"]}]
            for url in data_urls:
                content.append({"type": "image_url", "image_url": {"url": url}})
            msgs = [msgs[0], {"role": "user", "content": content}]
    started = time.time()
    try:
        text = await asyncio.wait_for(call_provider(provider, msgs), timeout=120)
        return (time.time() - started) * 1000, str(text)[:100]
    except Exception as exc:  # noqa: BLE001
        return (time.time() - started) * 1000, "ERR " + str(exc)[:80]


async def main() -> int:
    bank = Bank(config.BANK_PATH, config.BANK_DB_PATH)
    provs = providers()
    if not provs:
        print("没有启用的 provider")
        return 1
    provider = provs[0]

    cands = [
        i for i in bank.all()
        if i["type"] == "completion" and i["source"] == "official"
        and i["status"] == "adopted" and i["answer"]
        and re.search(r"https?://", i["title"])
    ]
    limit = min(6, len(cands))
    print(f"候选 {len(cands)} 道，本次抽 {limit} 道，模型 {provider['name']}")
    ok_a = ok_b = 0
    for item in cands[:limit]:
        ms_a, a = await ask(provider, item["title"], False)
        ms_b, b = await ask(provider, item["title"], True)
        truth = str(item["answer"])
        if truth in a:
            ok_a += 1
        if truth in b:
            ok_b += 1
        print()
        print("题:", short(item["title"]), "| 官方答案:", truth)
        print("  A 只给 OCR 文字 %6.0fms -> %s" % (ms_a, a.replace("\n", " ")[:78]))
        print("  B 图 + OCR 文字  %6.0fms -> %s" % (ms_b, b.replace("\n", " ")[:78]))
    print()
    print("命中官方答案：A(现状) %d 条，B(视觉) %d 条，共 %d 题" % (ok_a, ok_b, limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""多次重复，量化「完整获取」对正确率的稳定收益。

单次跑有模型随机性（同一批题上次 8/8，这次 6/8）。每题跑 3 次，
按「至少答对一次」和「平均正确率」两个口径统计。
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

ROUNDS = 3


def providers():
    data = json.load(open(os.path.join(config.DATA_DIR, "providers.json"), encoding="utf-8"))
    items = data if isinstance(data, list) else data.get("providers", data.get("items", []))
    return [p for p in items if p.get("enabled")]


def parse_answer(text):
    try:
        return str(json.loads(text).get("answer") or "").strip()
    except Exception:
        m = re.search(r'"answer"\s*:\s*"([^"]*)"', text)
        return (m.group(1) if m else "").strip()


async def ask(provider, title, with_images):
    msgs = build_messages("completion", title, ["请输入答案"], None, "", "")
    if with_images:
        urls = extract_urls(title)
        data_urls = await fetch_inline_images(urls)
        if data_urls:
            content = [{"type": "text", "text": msgs[1]["content"]}]
            for url in data_urls:
                content.append({"type": "image_url", "image_url": {"url": url}})
            msgs = [msgs[0], {"role": "user", "content": content}]
    try:
        text = await asyncio.wait_for(call_provider(provider, msgs), timeout=150)
        return parse_answer(str(text))
    except Exception:  # noqa: BLE001
        return ""


async def main():
    bank = Bank(config.BANK_PATH, config.BANK_DB_PATH)
    provider = providers()[0]
    cands = [
        i for i in bank.all()
        if i["type"] == "completion" and i["source"] == "official"
        and i["status"] == "adopted" and i["answer"]
        and re.search(r"https?://", i["title"])
    ][:8]
    print("样本 %d 道 × %d 轮，模型 %s\n" % (len(cands), ROUNDS, provider["name"]))
    blind_total = full_total = 0
    blind_any = full_any = 0
    for item in cands:
        truth = str(item["answer"]).strip()
        blind_hits = full_hits = 0
        for _ in range(ROUNDS):
            if await ask(provider, item["title"], False) == truth:
                blind_hits += 1
            if await ask(provider, item["title"], True) == truth:
                full_hits += 1
        blind_total += blind_hits
        full_total += full_hits
        blind_any += 1 if blind_hits else 0
        full_any += 1 if full_hits else 0
        print("  官方=%-6s 看不到图 %d/%d   完整获取 %d/%d" % (
            truth[:6], blind_hits, ROUNDS, full_hits, ROUNDS))
    total = len(cands) * ROUNDS
    print()
    print("平均正确率：看不到图 %d/%d (%.0f%%)   完整获取 %d/%d (%.0f%%)" % (
        blind_total, total, 100 * blind_total / total,
        full_total, total, 100 * full_total / total))
    print("至少答对一次：看不到图 %d/%d   完整获取 %d/%d" % (
        blind_any, len(cands), full_any, len(cands)))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

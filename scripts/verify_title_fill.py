"""验证「题干补全」的实际效果。

做法：拿题库里存有完整官方题干、但请求只带截断题干的题，
对比「补全前 / 补全后」模型给出的答案与官方答案。
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
from app.title_fill import find_full_title  # noqa: E402


def providers():
    data = json.load(open(os.path.join(config.DATA_DIR, "providers.json"), encoding="utf-8"))
    items = data if isinstance(data, list) else data.get("providers", data.get("items", []))
    return [p for p in items if p.get("enabled")]


async def ask(provider, title):
    msgs = build_messages("completion", title, ["请输入答案"], None, "", "")
    # 带上图片（flatten 到白底），否则模型看不到矩阵内容
    urls = extract_urls([title])
    data_urls = await fetch_inline_images(urls)
    if data_urls:
        content = [{"type": "text", "text": msgs[1]["content"]}]
        for url in data_urls:
            content.append({"type": "image_url", "image_url": {"url": url}})
        msgs = [msgs[0], {"role": "user", "content": content}]
    started = time.time()
    try:
        text = await asyncio.wait_for(call_provider(provider, msgs), timeout=150)
        return (time.time() - started) * 1000, str(text)[:90]
    except Exception as exc:  # noqa: BLE001
        return (time.time() - started) * 1000, "ERR " + str(exc)[:70]


async def main():
    bank = Bank(config.BANK_PATH, config.BANK_DB_PATH)
    provs = providers()
    provider = provs[0]
    items = bank.all()

    # 找：官方答案 + 完整题干（>300 字）+ 库里有同题的截断版本
    cands = []
    for i in items:
        if i["type"] != "completion" or i["source"] != "official":
            continue
        if i["status"] != "adopted" or not i["answer"]:
            continue
        if len(i["title"]) <= 300:
            continue
        cands.append(i)
    print("题库中题干>300 的官方题: %d 道\n" % len(cands))

    ok_short = ok_full = total = 0
    for item in cands[:6]:
        full = item["title"]
        truncated = full[:300]
        found = find_full_title(items, "completion", truncated, item.get("subject"))
        if not found:
            continue
        total += 1
        ms_a, a = await ask(provider, truncated)
        ms_b, b = await ask(provider, found[0])
        truth = str(item["answer"])
        hit_a = truth in a
        hit_b = truth in b
        ok_short += 1 if hit_a else 0
        ok_full += 1 if hit_b else 0
        print("官方答案:", truth)
        print("  截断题干 %6.0fms -> %s  %s" % (ms_a, a.replace("\n", " ")[:70], "OK" if hit_a else "NG"))
        print("  完整题干 %6.0fms -> %s  %s" % (ms_b, b.replace("\n", " ")[:70], "OK" if hit_b else "NG"))
        print()
    print("命中官方答案：截断题干 %d/%d，完整题干 %d/%d" % (ok_short, total, ok_full, total))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""验证「选项是图」的题现在能答对。

样本：题库里选项本身是图片的单选/多选题。
做法：拿它们的官方答案，直接问模型（绕开题库命中），看答对没有。
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
from app.option_images import annotate_options  # noqa: E402
from app.parsing import build_messages  # noqa: E402


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


async def ask(provider, title, options, with_images):
    annotated, opt_urls = annotate_options(list(options))
    prompt_options = annotated if with_images else list(options)
    msgs = build_messages("single", title, prompt_options, None, "", "")
    if with_images:
        urls = extract_urls([title]) + opt_urls
        data_urls = await fetch_inline_images(urls)
        if data_urls:
            content = [{"type": "text", "text": msgs[1]["content"]}]
            for url in data_urls:
                content.append({"type": "image_url", "image_url": {"url": url}})
            msgs = [msgs[0], {"role": "user", "content": content}]
    started = time.time()
    try:
        text = await asyncio.wait_for(call_provider(provider, msgs), timeout=150)
        return (time.time() - started) * 1000, parse_answer(str(text))
    except Exception as exc:  # noqa: BLE001
        return (time.time() - started) * 1000, "ERR:" + str(exc)[:40]


async def main():
    bank = Bank(config.BANK_PATH, config.BANK_DB_PATH)
    provider = providers()[0]
    cands = [
        i for i in bank.all()
        if i["type"] in ("single", "multiple") and i["status"] == "adopted"
        and i["answer"]
        and any(extract_urls(o) for o in i["options"])
    ][:6]
    print("样本 %d 道（选项是图），模型 %s\n" % (len(cands), provider["name"]))
    ok_a = ok_b = 0
    for item in cands:
        truth = str(item["answer"]).strip()
        ms_a, a = await ask(provider, item["title"], item["options"], False)
        ms_b, b = await ask(provider, item["title"], item["options"], True)
        ok_a += 1 if a == truth else 0
        ok_b += 1 if b == truth else 0
        print("官方=%-3s  A(只文字) %-8s %s   B(图+归属标记) %-8s %s" % (
            truth[:3], a[:8] or "空", "✓" if a == truth else "✗",
            b[:8] or "空", "✓" if b == truth else "✗"))
    print("\nA 只给选项文字   %d/%d" % (ok_a, len(cands)))
    print("B 图+归属标记    %d/%d" % (ok_b, len(cands)))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

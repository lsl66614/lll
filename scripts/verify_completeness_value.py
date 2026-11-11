"""测「题目完整获取」对 AI 正确率的影响。

用官方卷 DTO 通道（paper）拿到的题：题干 + 选项 + 正确答案都在手上。
对同一批题跑三种输入形态，比较模型答案与官方答案的命中率：

  A 纯文字（把图 URL 换成占位）—— 模拟「模型看不到图」
  B 图 + 文字 —— 完整获取（现在的做法）
  C 文字骨架（去掉图片，保留 OCR 文字）—— 中间态

这能量化「完整获取」到底值多少。
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


def providers():
    data = json.load(open(os.path.join(config.DATA_DIR, "providers.json"), encoding="utf-8"))
    items = data if isinstance(data, list) else data.get("providers", data.get("items", []))
    return [p for p in items if p.get("enabled")]


async def ask(provider, title, mode):
    texts = [title]
    if mode == "A":
        # 纯文字：图片 URL 全删掉
        texts = [re.sub(r"https?://\S+", "[公式图]", title)]
    msgs = build_messages("completion", texts[0], ["请输入答案"], None, "", "")
    if mode in ("B", "C"):
        urls = extract_urls([title])
        data_urls = await fetch_inline_images(urls)
        if data_urls and mode == "B":
            content = [{"type": "text", "text": msgs[1]["content"]}]
            for url in data_urls:
                content.append({"type": "image_url", "image_url": {"url": url}})
            msgs = [msgs[0], {"role": "user", "content": content}]
    started = time.time()
    try:
        text = await asyncio.wait_for(call_provider(provider, msgs), timeout=150)
        return (time.time() - started) * 1000, str(text)[:100]
    except Exception as exc:  # noqa: BLE001
        return (time.time() - started) * 1000, "ERR " + str(exc)[:60]


def parse_answer(text):
    """从模型输出里抠出 answer 字段（严格，避免 '0.4' 匹配到 confidence）。"""
    try:
        data = json.loads(text)
        return str(data.get("answer") or "").strip()
    except Exception:
        m = re.search(r'"answer"\s*:\s*"([^"]*)"', text)
        return (m.group(1) if m else "").strip()


async def main():
    bank = Bank(config.BANK_PATH, config.BANK_DB_PATH)
    provider = providers()[0]
    cands = [
        i for i in bank.all()
        if i["type"] == "completion" and i["source"] == "official"
        and i["status"] == "adopted" and i["answer"]
        and re.search(r"https?://", i["title"])
    ][:8]
    print("样本 %d 道，模型 %s\n" % (len(cands), provider["name"]))
    score = {"A": 0, "B": 0, "C": 0}
    for item in cands:
        truth = str(item["answer"]).strip()
        row = []
        for mode in ("A", "B", "C"):
            ms, raw = await ask(provider, item["title"], mode)
            got = parse_answer(raw)
            hit = got == truth
            score[mode] += 1 if hit else 0
            row.append("%s:%s%s" % (mode, got[:8] or "空", "✓" if hit else "✗"))
        print("  官方=%-8s %s" % (truth[:8], "  ".join(row)))
    print()
    print("A 纯文字(看不到图) %d/%d" % (score["A"], len(cands)))
    print("B 图+文字(完整获取) %d/%d" % (score["B"], len(cands)))
    print("C 仅文字(有URL无图) %d/%d" % (score["C"], len(cands)))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

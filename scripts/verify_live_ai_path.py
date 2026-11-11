"""线上验证视觉路径：挑「题库里没有答案」的含图题，看模型能否读出图。

这些题的官方答案在别的记录里（题干不同），所以走 AI 链路。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
BASE = "http://127.0.0.1:8000"


def post(payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + "/query", data=body,
                                 headers={"Content-Type": "application/json"})
    started = time.time()
    raw = urllib.request.urlopen(req, timeout=180).read().decode()
    out = json.loads(raw)
    return (time.time() - started) * 1000, (out.get("data") or out)


def main():
    from app import config
    from app.bank import Bank

    bank = Bank(config.BANK_PATH, config.BANK_DB_PATH)
    items = bank.all()
    # 找出「题干含图 + 至少有一条官方答案」的题，再挑官方答案与请求不同的 URL 版本
    cands = []
    for i in items:
        if i["type"] != "completion" or not i["answer"]:
            continue
        if not re.search(r"https?://", i["title"]):
            continue
        cands.append(i)
    # 去掉题库里已能直接命中的（题干完全相同的）
    usable = []
    for i in cands:
        if bank.find_official("completion", i["title"], ["请输入答案"]):
            continue
        usable.append(i)
    usable = usable[:5]
    print("走 AI 链路的含图题 %d 道\n" % len(usable))
    for item in usable:
        ms, d = post({
            "title": item["title"], "type": item["type"],
            "options": ["请输入答案"], "images": 0,
        })
        print("%6.0fms source=%-10s match=%-18s answer=%-10s | %s" % (
            ms, d.get("source"), d.get("match"),
            str(d.get("answer"))[:10],
            re.sub(r"https?://\S+", "[图]", item["title"])[:52]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

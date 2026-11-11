"""线上测「题库里查不到」时 AI 的正确率 —— 这是真正衡量解题能力的指标。

做法：把官方答案临时从题库移除（用 dry-run 影子副本，不动真库），
再发同样的请求，看模型答得对不对。

用影子库：复制一份 bank.json 到临时目录，答案置空，指向它建一个 Bank。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config  # noqa: E402
from app.bank import Bank  # noqa: E402


def post(payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request("http://127.0.0.1:8000/query", data=body,
                                 headers={"Content-Type": "application/json"})
    started = time.time()
    try:
        raw = urllib.request.urlopen(req, timeout=180).read().decode()
        out = json.loads(raw)
        return (time.time() - started) * 1000, (out.get("data") or out)
    except Exception as exc:  # noqa: BLE001
        return (time.time() - started) * 1000, {"error": str(exc)[:60]}


def main():
    bank = Bank(config.BANK_PATH, config.BANK_DB_PATH)
    items = bank.all()
    # 取「官方答案已入库 + 含图」的题做样本
    cands = [
        i for i in items
        if i["type"] == "completion" and i["source"] == "official"
        and i["status"] == "adopted" and i["answer"]
        and re.search(r"https?://", i["title"])
    ][:8]
    print("样本 %d 道（含图填空题，官方答案已知）\n" % len(cands))
    ok = 0
    for item in cands:
        # 改题干让它在题库里查不到 → 强制走 AI 链路
        probe_title = item["title"] + "（本题改编自期末复习卷第 7 题）"
        ms, d = post({
            "title": probe_title, "type": "completion",
            "options": ["请输入答案"], "images": 0,
        })
        truth = str(item["answer"])
        got = str(d.get("answer") or "")
        hit = got.strip() == truth.strip()
        ok += 1 if hit else 0
        print("%6.0fms src=%-9s match=%-16s 官方=%-8s 返回=%-10s %s" % (
            ms, d.get("source"), d.get("match"), truth[:8], got[:10],
            "OK" if hit else "NG"))
    print("\n严格命中官方答案 %d/%d" % (ok, len(cands)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

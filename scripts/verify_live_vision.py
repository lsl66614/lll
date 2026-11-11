"""线上端到端验证：视觉升级 + 官方优先，两件事一起测。

跑法：
    .venv\\Scripts\\python.exe scripts/verify_live_vision.py
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
    payload_out = json.loads(raw)
    return (time.time() - started) * 1000, (payload_out.get("data") or payload_out)


def short(text):
    return re.sub(r"https?://\S+", "[图]", str(text or ""))[:60]


def main():
    sys.path.insert(0, ROOT)
    from app import config
    from app.bank import Bank

    bank = Bank(config.BANK_PATH, config.BANK_DB_PATH)
    # 取「官方答案已入库、题干含图」的题做线上验证
    cands = [
        i for i in bank.all()
        if i["type"] == "completion" and i["source"] == "official"
        and i["status"] == "adopted" and i["answer"]
        and re.search(r"https?://", i["title"])
    ][:5]
    print("验证 %d 道（含图 + 官方答案已入库）\n" % len(cands))
    hit = 0
    for item in cands:
        ms, d = post({
            "title": item["title"], "type": item["type"],
            "options": ["请输入答案"], "images": 0,
        })
        truth = str(item["answer"])
        got = str(d.get("answer") or "")
        ok = truth in got
        hit += 1 if ok else 0
        print("%6.0fms %s 官方=%-6s 返回=%-8s source=%-8s match=%-18s %s" % (
            ms, "OK " if ok else "NG ", truth, got[:8],
            d.get("source"), d.get("match"), short(item["title"])))
    print("\n命中官方答案 %d/%d" % (hit, len(cands)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""量化「题目完整获取」的剩余缺口。

分四类统计题库里的题各自缺什么：
  1. 题干完整 + 选项完整        → 模型能直接答
  2. 题干完整 + 选项是图        → 需要把选项图也送进模型
  3. 题干是图 + 选项完整        → 现在支持
  4. 题干是图 + 选项是图        → 两边都要送
再统计：图片现在还能下载吗（404 的救不了）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app import config  # noqa: E402
from app.bank import Bank  # noqa: E402
from app.shape_key import image_urls  # noqa: E402


def main():
    bank = Bank(config.BANK_PATH, config.BANK_DB_PATH)
    items = bank.all()
    groups = Counter()
    samples = {}
    dead_urls = Counter()

    for item in items:
        title_has = bool(image_urls(item["title"]))
        opt_has = any(image_urls(o) for o in item["options"])
        key = ("题干有图" if title_has else "题干纯文字",
               "选项有图" if opt_has else "选项纯文字")
        groups[key] += 1
        samples.setdefault(key, item)

    print("题库 %d 条的形态分布：" % len(items))
    for key, count in groups.most_common():
        print("  %-12s %-12s %d" % (key[0], key[1], count))

    print("\n各形态里图片的存活情况（抽样 8 条）：")
    for key, sample in samples.items():
        urls = []
        for item in items:
            if ("题干有图" if image_urls(item["title"]) else "题干纯文字",
                    "选项有图" if any(image_urls(o) for o in item["options"]) else "选项纯文字") != key:
                continue
            urls = image_urls(item["title"]) + [u for o in item["options"] for u in image_urls(o)]
            if urls:
                break
        if not urls:
            print("  %-12s %-12s 无图" % (key[0], key[1]))
            continue
        alive = dead = 0
        for u in urls[:8]:
            try:
                urllib.request.urlopen(u, timeout=8).read()
                alive += 1
            except Exception:
                dead += 1
        print("  %-12s %-12s 图 %d 张：活 %d / 死 %d" % (key[0], key[1], len(urls[:8]), alive, dead))
        if dead:
            dead_urls[key] += dead
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

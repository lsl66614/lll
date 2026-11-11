"""模拟「把语义冲突的 official+adopted 判断题降级」能改善多少。

只读，不改库。
"""
import collections
import html as H
import importlib.util
import os
import re
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402
from app.mooc_result import _option_text, _option_body  # noqa: E402
from app.shape_key import image_urls, text_key  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "ve", os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_e2e_accuracy.py"))
ve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ve)

# 快照真值：按 (文字骨架, 选项顺序) —— 协作会话的口径
optorder = collections.defaultdict(set)
exact = collections.defaultdict(set)
for f in sorted(os.listdir(config.SNAPSHOT_DIR)):
    if not f.endswith(".html"):
        continue
    s = open(os.path.join(config.SNAPSHOT_DIR, f), encoding="utf-8", errors="replace").read()
    if "u-questionItem" not in s:
        continue
    for blk in re.split(r'(?=<div[^>]*class="[^"]*u-questionItem)', s):
        if "u-questionItem" not in blk[:300]:
            continue
        order, opts = [], []
        for om in re.finditer(r"<li([^>]*)>(.*?)</li>", blk, re.S):
            pos = re.search(r'class="[^"]*optionPos[^"]*"[^>]*>([^<]*)<', om.group(2))
            if not pos:
                continue
            order.append((re.findall(r"[A-Za-z]", pos.group(1)) or ["?"])[0].upper())
            opts.append(_option_text(_option_body(om.group(2))))
        if len(opts) != 2 or not all(o in ("正确", "错误") for o in opts):
            continue
        ans = re.search(r'正确答案：</span><span[^>]*>([^<]+)</span>', blk)
        if not ans:
            continue
        L2 = re.findall(r"[A-Za-z]", ans.group(1))
        if not L2 or L2[0].upper() not in order:
            continue
        tm = re.search(r'class="[^"]*j-richTxt[^"]*"[^>]*>(.*?)</div>\s*<div class="qaMark', blk, re.S)
        if not tm:
            continue
        title = re.sub(r"<[^>]+>", " ", H.unescape(tm.group(1)))
        sem = opts[order.index(L2[0].upper())]
        optorder[(text_key(title), tuple(opts))].add(sem)
        exact[(text_key(title), tuple(u.rsplit("/", 1)[-1] for u in image_urls(title)))].add(sem)

conn = sqlite3.connect("file:%s?mode=ro" % config.BANK_DB_PATH.replace("\\", "/"), uri=True)
conn.row_factory = sqlite3.Row
rows = [dict(r) for r in conn.execute("SELECT * FROM questions WHERE type='judge'")]


def opts_of(r):
    import json
    o = r["options"]
    if isinstance(o, str):
        try:
            o = json.loads(o)
        except ValueError:
            o = []
    return tuple(o or [])


demote = set()
for r in rows:
    if r["source"] != "official" or r["status"] != "adopted":
        continue
    sems = optorder.get((text_key(str(r["title"])), opts_of(r)))
    if sems and len(sems) > 1:
        demote.add(r["id"])

print("拟降级（语义冲突）的 official+adopted 判断题: %d 条" % len(demote))


def fp(t):
    return tuple(u.rsplit("/", 1)[-1] for u in image_urls(t))


bykey = collections.defaultdict(list)
for r in rows:
    bykey[(text_key(str(r["title"])), fp(str(r["title"])))].append(r)

cases = [c for c in ve.build_cases() if c["type"] == "judge"]
hit_cases = 0
hit_err = 0
errs_before = 0
errs_after = 0
for c in cases:
    recs = bykey.get((text_key(c["title"]), fp(c["title"])), [])
    touched = [r for r in recs if r["id"] in demote]
    a, s = ve.query(8000, c)
    ok = ve.answer_matches(a, c) if a else False
    if not ok:
        errs_before += 1
    if touched:
        hit_cases += 1
        # 降级后：这些记录不再作为 official 权威，取剩下的最高优先级
        rest = [r for r in recs if r["id"] not in demote]
        if rest:
            rest.sort(key=lambda r: {"official": 4, "manual": 3, "calibrated": 2, "ai": 1}.get(r["source"], 0),
                     reverse=True)
            newans = str(rest[0]["answer"] or "").strip()
            newok = (newans == c["truth"][0])
        else:
            newok = False   # 没有记录了 → 掉 AI，本来就错
        if not ok and newok:
            hit_err += 1
    else:
        # 没被降级影响 → 答案不变
        newok = ok
    if not newok:
        errs_after += 1

print("  被 434 条判断题用例命中的: %d 条" % hit_cases)
print("  其中「降级后能变对」的: %d 条" % hit_err)
print()
print("降级前判断题错误数: %d  →  降级后: %d" % (errs_before, errs_after))

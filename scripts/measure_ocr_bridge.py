"""测量「OCR 跨表示兜底匹配」的收益。

问题：一道题在库里是**图片形式**的选项，在请求里是**文字形式**的选项，
内容对不上 → `find_official` 匹配不上 → 官方答案白白浪费，掉进 AI 兜底。

本脚本只读，不改任何库，量化：
1. 库里有多少条 official 记录的选项是图片；
2. 其中有多少条，图片的 OCR 文本能和「文字形式的同题记录」的选项对上；
3. 对上的那些，答案是否和快照真值一致（会不会引入误配）。

用法：
    .venv\\Scripts\\python.exe scripts/measure_ocr_bridge.py
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config  # noqa: E402
from app.bank import _norm_title_for_dedupe  # noqa: E402

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
URL_RE = re.compile(r"https?://\S+", re.I)


def load_image_text() -> dict[str, str]:
    """URL → OCR 文本。

    索引有三种形状都见过：顶层直接是 ``{url: text}``、
    ``{url: {"text": ...}}``、``{"mapping": {...}}`` —— 都兼容。
    """
    path = os.path.join(config.DATA_DIR, "image_text.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fp:
        raw = json.load(fp)
    mapping: dict[str, str] = {}

    def absorb(entries) -> None:
        if isinstance(entries, dict):
            for url, value in entries.items():
                if isinstance(value, str):
                    mapping[url] = value
                elif isinstance(value, dict):
                    text = value.get("text") or value.get("ocr") or value.get("value")
                    if text:
                        mapping[url] = str(text)
        elif isinstance(entries, list):
            for item in entries:
                if isinstance(item, dict):
                    url = item.get("url") or item.get("src")
                    text = item.get("text") or item.get("ocr")
                    if url and text:
                        mapping[url] = str(text)

    if isinstance(raw, dict):
        inner = raw.get("mapping")
        absorb(inner if isinstance(inner, (dict, list)) else raw)
    else:
        absorb(raw)
    return mapping


def content_key(text: str, image_text: dict[str, str]) -> str:
    """把一个选项归一成可比对的键：图片用 OCR 文本，文字用原文。"""
    value = str(text or "")
    if URL_RE.search(value):
        ocr = ""
        for url in URL_RE.findall(value):
            ocr = image_text.get(url, "") or ocr
            if ocr:
                break
        value = ocr or value
    return _norm_title_for_dedupe(value)


def main() -> int:
    db_path = config.BANK_DB_PATH
    if not os.path.exists(db_path):
        print("找不到题库：%s" % db_path)
        return 1
    image_text = load_image_text()
    print("OCR 索引：%d 条 URL → 文本" % len(image_text))

    conn = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM questions")]

    # 按「题干骨架 + 选项集（归一后）」分组
    groups: dict[tuple, list[dict]] = {}
    for item in rows:
        try:
            options = item.get("options") or []
            if isinstance(options, str):
                options = json.loads(options)
        except (TypeError, ValueError):
            options = []
        title = str(item.get("title") or "")
        key = (str(item.get("type") or ""), _norm_title_for_dedupe(title))
        groups.setdefault(key, []).append({**item, "_options": options})

    # 找出「同组里有图片形式、也有文字形式」的组
    bridged = 0
    total_official = 0
    image_only_official = 0
    detail = []
    for key, items in groups.items():
        officials = [i for i in items if i.get("source") == "official" and i.get("status") == "adopted"]
        for official in officials:
            total_official += 1
            opts = official["_options"]
            if not opts:
                continue
            if not any(URL_RE.search(str(o or "")) for o in opts):
                continue
            image_only_official += 1
            # 同组里有没有文字形式的记录？
            text_siblings = [
                i for i in items
                if i is not official
                and i["_options"]
                and not any(URL_RE.search(str(o or "")) for o in i["_options"])
            ]
            if not text_siblings:
                continue
            # 图片版选项的 OCR 文本，能否对上文字版的选项？
            image_keys = [content_key(o, image_text) for o in opts]
            matched = False
            for sib in text_siblings:
                sib_keys = {content_key(o, image_text) for o in sib["_options"]}
                if all(k and k in sib_keys for k in image_keys):
                    matched = True
                    break
            if matched:
                bridged += 1
                if len(detail) < 8:
                    detail.append((official["id"], official.get("answer"),
                                   [str(o)[:20] for o in opts[:4]]))

    print()
    print("官方答案（adopted）总条数      : %d" % total_official)
    print("  其中选项是图片形式的          : %d" % image_only_official)
    print("  能被同组文字记录桥接上的      : %d" % bridged)
    if image_only_official:
        print("  → 可桥接比例                  : %.1f%%" % (bridged / image_only_official * 100))
    print()
    for d in detail:
        print("  桥接示例 id=%s ans=%r opts=%s" % d)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""语义召回离线评估：用题库里的「同选项同答案、题干不同」配对当正样本，
随机不同答案配对当负样本，输出各阈值下的命中/误命中，供标定 SEMANTIC_THRESHOLD。

用法：.venv/Scripts/python.exe tools/eval_semantic.py [--limit 400]
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bank import _dice, _norm_text  # noqa: E402
from app.config import BANK_PATH, SEMANTIC_DB, SEMANTIC_DIR  # noqa: E402
from app.parsing import normalize_type, parse_options  # noqa: E402
from app.semantic import SemanticStore  # noqa: E402


def load_items(limit: int) -> list[dict]:
    with open(BANK_PATH, "r", encoding="utf-8") as fp:
        data = json.load(fp)
    items = []
    for raw in data.get("items", []):
        qtype = normalize_type(raw.get("type"))
        if qtype not in ("single", "multiple", "completion"):
            continue
        options = parse_options(raw.get("options"))
        if qtype in ("single", "multiple") and len(options) < 2:
            continue
        if str(raw.get("image_hash") or ""):
            continue
        title = str(raw.get("title") or "").strip()
        answer = str(raw.get("answer") or "").strip()
        if not title or not answer:
            continue
        items.append({"id": raw["id"], "type": qtype, "title": title, "answer": answer,
                      "options": tuple(sorted(_norm_text(o) for o in options)), "sh": raw.get("sh") or ""})
    random.Random(20261109).shuffle(items)
    return items[:limit]


def build_pairs(items: list[dict]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    positives, negatives = [], []
    by_key: dict[tuple, list[dict]] = {}
    for item in items:
        by_key.setdefault((item["type"], item["options"], item["answer"]), []).append(item)

    for group in by_key.values():
        for left, right in itertools.combinations(group[:8], 2):
            if _dice(_norm_text(left["title"]), _norm_text(right["title"])) < 0.9:
                positives.append((left["title"], right["title"]))
            if len(positives) >= 120:
                break

    rng = random.Random(7)
    for _ in range(4000):
        if len(negatives) >= 120:
            break
        left, right = rng.sample(items, 2)
        if left["type"] != right["type"] or left["answer"] == right["answer"]:
            continue
        if _dice(_norm_text(left["title"]), _norm_text(right["title"])) >= 0.5:
            continue
        negatives.append((left["title"], right["title"]))
    return positives, negatives


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=400)
    args = parser.parse_args()

    store = SemanticStore(SEMANTIC_DIR, SEMANTIC_DB)
    if not store.available():
        raise SystemExit("语义模型不可用（检查 data/models/bge-small-zh-v1.5）")

    items = load_items(args.limit)
    positives, negatives = build_pairs(items)
    print(f"items={len(items)} positives={len(positives)} negatives={len(negatives)}")

    def scores(pairs):
        values = []
        for left, right in pairs:
            a, b = store.embed(left), store.embed(right)
            if a and b:
                values.append(store.cosine(a, b))
        return values

    pos = sorted(scores(positives))
    neg = sorted(scores(negatives))
    if pos:
        print("positives p10/p50/p90 = "
              f"{pos[len(pos)//10]:.3f} / {pos[len(pos)//2]:.3f} / {pos[min(len(pos)-1, len(pos)*9//10)]:.3f}")
    if neg:
        print("negatives p50/p90/p99 = "
              f"{neg[len(neg)//2]:.3f} / {neg[min(len(neg)-1, len(neg)*9//10)]:.3f} / "
              f"{neg[min(len(neg)-1, len(neg)*99//100)]:.3f}")
    print("threshold  recall  fpr  precision")
    for threshold in (0.75, 0.78, 0.80, 0.82, 0.84, 0.86, 0.88, 0.90):
        tp = sum(1 for value in pos if value >= threshold)
        fp = sum(1 for value in neg if value >= threshold)
        recall = tp / len(pos) if pos else 0
        fpr = fp / len(neg) if neg else 0
        precision = tp / (tp + fp) if (tp + fp) else 0
        print(f"{threshold:.2f}       {recall:.2f}    {fpr:.2f}  {precision:.2f}")


if __name__ == "__main__":
    main()

"""AI-only 实测：只挑「题库里查不到」的填空题，原题打网关，按快照真值判分。

为什么不复用 ``verify_ai_accuracy.py``：那个脚本靠给题干追加一句噪声来绕开题库，
但 ``Bank.match`` 的字符相似度阈值是 0.9，追加短句后仍然会命中官方记录
（实测 8/8 全是 ``source=official, match=similar``、耗时 27~98ms，根本没走 AI）。
本脚本先用 ``find_official`` + ``match`` 离线筛掉所有能命中题库/官方的题，
剩下的原题发送，保证请求真的落到 AI 链路。

用法：
    .venv\\Scripts\\python.exe scripts/probe_ai_only.py --port 8000 --limit 8
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config  # noqa: E402
from app.bank import Bank, DEFAULT_SUBJECT  # noqa: E402
from app.formula_index import ImageTextStore  # noqa: E402
from app.subjects import SubjectRules  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "ve", os.path.join(ROOT, "scripts", "verify_e2e_accuracy.py"))
ve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ve)


def load_bank() -> tuple[Bank, str]:
    """用 sqlite backup API 复制一份只读题库，绝不碰真实库。"""
    tmp = tempfile.mkdtemp(prefix="ai-probe-")
    src = sqlite3.connect(config.BANK_DB_PATH)
    dst = sqlite3.connect(os.path.join(tmp, "bank.db"))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    bank = Bank(os.path.join(tmp, "bank.json"), os.path.join(tmp, "bank.db"))
    bank.set_image_text(
        ImageTextStore(os.path.join(config.DATA_DIR, "image_text.json")).mapping())
    return bank, tmp


def select_cases(bank: Bank) -> list[tuple[dict, str]]:
    """筛出「官方和本地题库都不命中」的填空题。"""
    rules = SubjectRules(config.SUBJECTS_PATH)
    picked: list[tuple[dict, str]] = []
    for case in ve.build_cases():
        if case["type"] != "completion":
            continue
        subject, _ = rules.infer(case["title"], case["options"])
        subject = subject or DEFAULT_SUBJECT
        if bank.find_official("completion", case["title"], case["options"], ""):
            continue
        if bank.match("completion", case["title"], case["options"], subject,
                      None, semantic=False):
            continue
        picked.append((case, subject))
    return picked


def post(port: int, case: dict) -> tuple[int, dict, str]:
    payload = json.dumps({
        "title": case["title"], "type": case["type"], "options": case["options"],
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d/query" % port, data=payload,
        headers={"Content-Type": "application/json"},
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 单题失败不中断整轮
        return int((time.time() - started) * 1000), {}, str(exc)[:80]
    elapsed = int((time.time() - started) * 1000)
    if body.get("code") != 0:
        return elapsed, body.get("data") or {}, "code=%s %s" % (
            body.get("code"), body.get("message") or "")
    return elapsed, body.get("data") or {}, ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()

    bank, tmp = load_bank()
    try:
        picked = select_cases(bank)
        print("题库无记录的填空题：%d 道；本轮取前 %d 道"
              % (len(picked), min(args.limit, len(picked))))
        picked = picked[: max(1, args.limit)]
        print()

        exact = loose = wrong = failed = 0
        sources: dict[str, int] = {}
        for index, (case, _subject) in enumerate(picked, 1):
            truth = str(case["truth"][0])
            elapsed, data, error = post(args.port, case)
            got = str(data.get("answer") or "")
            source = str(data.get("source") or "")
            match = str(data.get("match") or "")
            provider = str(data.get("provider") or "")
            cached = bool(data.get("cached"))
            sources[source or "?"] = sources.get(source or "?", 0) + 1
            if error:
                failed += 1
                print("%2d. %5dms ERR %s | 真值=%r" % (index, elapsed, error, truth[:20]))
                continue
            ok, basis = ve.judge_completion(got, truth)
            strict = ve.norm_text(got) == ve.norm_text(truth)
            if strict:
                exact += 1
            elif ok:
                loose += 1
            else:
                wrong += 1
            print("%2d. %5dms src=%-9s match=%-14s provider=%-10s cached=%s | 真值=%-14r 返回=%-14r %s"
                  % (index, elapsed, source, match, provider, cached,
                     truth[:14], got[:14],
                     "OK-exact" if strict else ("OK-" + basis if ok else "NG-" + basis)))
            print("      题：%s" % str(case["title"])[:88].replace("\n", " "))
        total = len(picked)
        print()
        print("严格文本相等：%d/%d" % (exact, total))
        print("宽松通过（numeric/prefix）：%d/%d" % (loose, total))
        print("合计算对：%d/%d；错/空：%d；调用失败：%d"
              % (exact + loose, total, wrong, failed))
        print("答案来源分布：%s" % ", ".join("%s=%d" % kv for kv in sorted(sources.items())))
        return 0
    finally:
        try:
            bank.db.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

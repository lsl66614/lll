"""清洗题库中被污染的官方答案。

两个根因：

1. 结果页解析器把 ``score`` 区块的对错图标当成选项内容，公式图片选项被写成
   「正确」/「错误」（app/mooc_result.py 已修）。
2. 官方答案按**字母**存，而同一道题的选项顺序会随时间变化（12:00 正确项在 B，
   21:38 同样的题正确项在 D）。入库那一刻的字母，事后可能指向别的选项。

对策：用快照（结果页原始 HTML）重建「正确选项的**内容**」作为基准，把每条官方
记录重算一遍：
- 内容能对上 → 保留（同时把存的是字母的答案按内容重新定位）
- 内容对不上但真值还在 → 按内容重定位字母
- 存的内容已经不可用（空白/图标文字）→ 用快照里的选项替换，答案按新选项算
- 快照里没有 → 保持原样，但若选项明显被污染则降级为 pending + conflict

用法：
    .venv\\Scripts\\python.exe scripts/clean_polluted_answers.py          # 只看清单
    .venv\\Scripts\\python.exe scripts/clean_polluted_answers.py --apply  # 落库
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 控制台默认 GBK，遇到下标字符会抛 UnicodeEncodeError——而那通常发生在
# 落库之后的打印环节，看起来像「脚本失败了」，实际数据已经写进去了。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import config  # noqa: E402
from app.bank import Bank  # noqa: E402
from app.mooc_result import parse_result_html  # noqa: E402

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_IMG_RE = re.compile(r'<img[^>]*src="([^"]+)"[^>]*>', re.I)
_URL_RE = re.compile(r"https?://\S+")
# 图标污染：单选/多选的选项里出现了判断题的图标文字
_GLYPH_TEXT = ("正确", "错误")


def option_identity(text: str) -> str:
    """选项的稳定标识：图片取 URL，文字取归一后的文本。

    快照里图片选项常常是「图片 + 尾随文字」（如 ``xxx.png 可逆.``），而 OCS
    请求和 paper 通道只带纯 URL。比对时按图片 URL 认，避免同一项因为尾随
    文字不同而被当成两项。
    """
    raw = str(text or "")
    img = _IMG_RE.search(raw)
    if img:
        return img.group(1).strip()
    url = _URL_RE.search(raw)
    if url:
        return url.group(0).strip()
    from app.bank import _norm_text

    return _norm_text(raw)


def clean_options(options: list[str]) -> list[str]:
    """把选项里的图片 URL 抽出来（去掉尾随文字），与 paper 通道写法对齐。"""
    result = []
    for opt in options:
        raw = str(opt or "")
        img = _IMG_RE.search(raw)
        if img:
            result.append(img.group(1).strip())
            continue
        result.append(raw.strip())
    return result


def build_truth() -> dict[tuple[str, str], dict]:
    """从快照重建「题干 → 正确选项内容 + 该题的选项列表」。"""
    truth: dict[tuple[str, str], dict] = {}
    conflicts: list[tuple[tuple[str, str], set]] = []
    snap_dir = config.SNAPSHOT_DIR
    if not os.path.isdir(snap_dir):
        return truth
    for name in sorted(os.listdir(snap_dir)):
        if not name.endswith(".html"):
            continue
        try:
            with open(os.path.join(snap_dir, name), "r", encoding="utf-8", errors="replace") as fp:
                html = fp.read()
        except OSError:
            continue
        if "u-questionItem" not in html:
            continue
        for item in parse_result_html(html):
            letters = item.get("correct_letters") or []
            options = item.get("options") or []
            if not letters or not options:
                continue
            good = []
            for letter in letters:
                index = LETTERS.find(letter.upper())
                if 0 <= index < len(options):
                    good.append(option_identity(options[index]))
            if not good:
                continue
            key = (item["type"], item["title"])
            entry = truth.get(key)
            if entry is None:
                truth[key] = {"identities": set(good), "options": list(options), "letters": set(letters)}
            else:
                if entry["identities"] != set(good):
                    conflicts.append((key, entry["identities"] | set(good)))
                entry["identities"] = set(good)
                entry["letters"] |= set(letters)
    if conflicts:
        print(f"[warn] 快照内部自相矛盾的题 {len(conflicts)} 条（已取首个出现的值）")
    return truth


def answer_identity(item: dict) -> str | None:
    """库里这条记录的答案，指向哪个选项内容。"""
    answer = str(item.get("answer") or "").strip()
    options = item.get("options") or []
    if item.get("type") == "judge":
        return answer if answer in ("正确", "错误") else None
    indexes = [LETTERS.index(c) for c in answer.replace("#", "") if c in LETTERS]
    if not indexes or any(i >= len(options) for i in indexes):
        return None
    picked = {option_identity(options[i]) for i in indexes}
    return "#".join(sorted(picked))


def plan(bank: Bank, truth: dict) -> dict:
    """算出每条官方记录该怎么改。"""
    changes: list[dict] = []
    for item in bank.all():
        if item.get("source") != "official":
            continue
        key = (item["type"], item["title"])
        entry = truth.get(key)
        options = item.get("options") or []
        polluted = (
            item["type"] in ("single", "multiple")
            and any((o or "").strip() in _GLYPH_TEXT for o in options)
        )
        unusable = item["type"] in ("single", "multiple") and any(
            not (o or "").strip() for o in options
        )
        # 选项重复也算坏：按内容映射答案时会映射到第一个，把正确答案翻反
        duplicated = len(set(options)) != len(options) if options else False
        broken = unusable or duplicated

        if entry is None:
            if polluted:
                changes.append({
                    "id": item["id"], "action": "downgrade", "reason": "选项被图标污染且无快照可核对",
                    "title": item["title"][:60], "from": item["answer"], "to": "",
                })
            elif broken:
                # 选项本身已经不可用（空项/重复），这条记录不能作为权威答案
                changes.append({
                    "id": item["id"], "action": "downgrade",
                    "reason": "选项为空或重复且无快照可核对",
                    "title": item["title"][:60], "from": item["answer"], "to": "",
                })
            continue

        good = entry["identities"]
        # 判断题的真值本身就是语义词（正确/错误），与选项内容不同空间，要直接比
        if item["type"] == "judge":
            semantic = next((g for g in good if g in ("正确", "错误")), "")
            if semantic and semantic != item["answer"]:
                changes.append({
                    "id": item["id"], "action": "answer", "reason": "判断题语义修正",
                    "title": item["title"][:60], "from": item["answer"], "to": semantic,
                })
            elif broken:
                changes.append({
                    "id": item["id"], "action": "downgrade", "reason": "判断题选项不可用",
                    "title": item["title"][:60], "from": item["answer"], "to": "",
                })
            continue

        current = answer_identity(item)
        if current is not None and current == "#".join(sorted(good)):
            # 内容对得上；只是选项本身不可用/被污染时，顺手换成干净选项
            if polluted or unusable:
                new_options = clean_options(entry["options"])
                if new_options != options:
                    changes.append({
                        "id": item["id"], "action": "options", "reason": "替换被污染的选项",
                        "title": item["title"][:60], "from": item["answer"], "to": item["answer"],
                        "options": new_options,
                    })
            continue

        # 内容对不上：能在现有选项里按内容精确重定位，就重定位
        new_options = list(options)
        identities = [option_identity(o) for o in options]
        hit = [i for i, ident in enumerate(identities)
               if ident and ident in good and len(ident) > 4]
        if hit:
            letter = LETTERS[hit[0]]
            if letter != item["answer"]:
                changes.append({
                    "id": item["id"], "action": "answer", "reason": "按选项内容重定位字母",
                    "title": item["title"][:60], "from": item["answer"], "to": letter,
                })
            continue

        # 现有选项里找不到真值：整条选项都不可信，用快照的选项 + 字母替换
        snap_options = clean_options(entry["options"])
        snap_ids = [option_identity(o) for o in snap_options]
        hit = [i for i, ident in enumerate(snap_ids) if ident and ident in good]
        if not hit:
            changes.append({
                "id": item["id"], "action": "downgrade", "reason": "选项内容与真值不符且无法重建",
                "title": item["title"][:60], "from": item["answer"], "to": "",
            })
            continue
        letter = LETTERS[hit[0]]
        changes.append({
            "id": item["id"], "action": "rewrite", "reason": "用快照选项重建答案",
            "title": item["title"][:60], "from": item["answer"], "to": letter,
            "options": snap_options,
        })
    return {"changes": changes, "truth_size": len(truth)}


def apply_changes(bank: Bank, changes: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for change in changes:
        action = change["action"]
        counts[action] = counts.get(action, 0) + 1
        if action == "downgrade":
            bank.update(change["id"], {"status": "pending", "conflict": True,
                                       "answer": "", "agree": 0})
        elif action == "options":
            bank.update(change["id"], {"options": change["options"], "conflict": False})
        elif action == "answer":
            bank.update(change["id"], {"answer": change["to"], "conflict": False})
        elif action == "rewrite":
            bank.update(change["id"], {"options": change["options"], "answer": change["to"],
                                       "conflict": False})
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只打印清单）")
    parser.add_argument("--dedupe", action="store_true",
                        help="额外执行去重合并（会**删除**重复记录，需单独确认）")
    parser.add_argument("--yes", action="store_true",
                        help="确认执行删除：打印将被删除的记录清单再落库")
    args = parser.parse_args()

    bank = Bank(config.BANK_PATH, config.BANK_DB_PATH)
    truth = build_truth()
    result = plan(bank, truth)
    changes = result["changes"]
    print(f"快照真值 {result['truth_size']} 条，待处理官方记录 {len(changes)} 条")
    for change in changes[:40]:
        print(f"  [{change['action']:9}] {change['id']} {change['reason']} | "
              f"{change['from']!r} -> {change['to']!r} | {change['title']}")
    if len(changes) > 40:
        print(f"  ... 还有 {len(changes) - 40} 条")
    if not args.apply:
        print("\n（dry-run，加 --apply 落库）")
        return 0

    counts = apply_changes(bank, changes)
    print("\n已落库：", counts)
    # 批量改完立刻 checkpoint，别让 WAL 攒着
    bank.checkpoint()

    # 去重会**删除**记录 —— 2026-11-10 的事故就是这一步误删了 452 条。
    # 所以：默认不做；要做必须同时给 --dedupe 和 --yes，
    # 且先打印删除清单，让人有机会喊停。
    if args.dedupe:
        preview = bank.dedupe(dry_run=True)
        print("\n去重预览：", {k: v for k, v in preview.items() if k not in ("details", "preview")})
        if not args.yes:
            print("（会删除记录，加 --yes 才真正执行）")
            return 0
        merged = bank.dedupe(dry_run=False)
        print("去重合并：", {k: v for k, v in merged.items() if k != "details"})
        bank.checkpoint()
    else:
        print("\n未执行去重（会删记录，需要 --dedupe --yes）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

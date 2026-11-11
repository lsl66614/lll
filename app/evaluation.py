"""评估内核：快照用例收集、答案比对、官方候选查找。

原先散在 ``scripts/verify_e2e_accuracy.py`` / ``scripts/verify_holdout.py`` 与
``app/checkup.py`` 的三套实现，现在收在这里，供体检、巡检和脚本共用同一口径：

- ``collect_cases`` / ``build_cases``：从 ``data/snapshots`` 抽真值，按
  (题型, 题干骨架, 题干图片指纹, 选项集) 去重，选项与真值成对保存；
- ``answer_matches`` / ``judge_completion``：把网关答案与真值比对；
- ``official_candidates``：与 ``Bank.find_official`` 同口径的可达官方候选。

比对口径（从严到宽，都单独统计，避免把「口径差」当成「答错」）：
  exact    归一后文本完全相等
  numeric  数值等价（``1/12`` 与真值 ``0.08``：真值保留两位小数）
  prefix   一方是另一方的前缀（``随机`` vs ``随机事件``）—— 最宽的一档
  mismatch 都不成立 → 计入错误

填空题真值不能用 ``text_key`` 归一：它会把小数点当标点剥掉（``0.3`` → ``03``），
真值一律保留原文；填空题也不能按文字骨架去重（公式图题骨架会为空）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from fractions import Fraction

from . import config
from .bank import (
    _norm_option,
    _norm_text,
    same_question_truncated,
    translate_answer,
)
from .mathcheck import math_equivalent
from .mooc_result import parse_result_html
from .shape_key import image_urls, text_key

L = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def norm_option(text: str) -> str:
    """选项内容的可比形式：图片取 URL（同一道题不同出卷版本 URL 会变 → 属正常）。"""
    urls = image_urls(text or "")
    if urls:
        return "IMG:" + urls[0]
    return text_key(text)


def norm_text(text: str) -> str:
    """文本归一：全角→半角、去空白、去首尾标点。**不做**去小数点。"""
    value = str(text or "").strip()
    value = re.sub(r"[！-～]", lambda m: chr(ord(m.group()) - 0xFEE0), value)
    value = re.sub(r"\s+", "", value)
    return value.strip("。；;，,、")


def as_number(text: str) -> tuple[float, int] | None:
    """把 '0.08' '1/12' '-1' 解析成 (数值, 小数位数)；解析不了返回 None。"""
    value = norm_text(text)
    if not value:
        return None
    decimals = 0
    if "." in value:
        head, _, tail = value.partition(".")
        if tail.isdigit():
            decimals = len(tail)
    try:
        if "/" in value:
            num, _, den = value.partition("/")
            frac = Fraction(num.strip()) / Fraction(den.strip())
            number = float(frac)
        else:
            number = float(value)
    except (ValueError, ZeroDivisionError, TypeError):
        return None
    return number, decimals


def case_id(item: dict) -> tuple:
    """快照条目的去重键：题型 + 文字骨架 + 题干图片指纹 + 选项集。"""
    return (
        item.get("type"),
        text_key(item.get("title")),
        tuple(image_urls(item.get("title"))),
        tuple(norm_option(o) for o in (item.get("options") or [])),
    )


def collect_cases() -> tuple[dict[tuple, dict], dict[tuple, int]]:
    """从快照抽真值，返回 (用例表, 每道题出现在几个快照文件里)。

    三种题型分别处理：
    - 单选/多选：答案是**字母** → 映射到选项内容
    - 判断题：答案是**字母**，但选项就是「正确/错误」两个图标 → 同样映射到内容
    - 填空题：答案是**原文**（correct_text），没有选项、也没有字母 → 保留原文
    """
    cases: dict[tuple, dict] = {}
    files_seen: dict[tuple, set] = {}
    snap_dir = config.SNAPSHOT_DIR
    try:
        names = sorted(os.listdir(snap_dir))
    except OSError:
        return cases, {}
    for fname in names:
        if not fname.endswith(".html"):
            continue
        path = os.path.join(snap_dir, fname)
        try:
            with open(path, encoding="utf-8", errors="replace") as fp:
                html = fp.read()
        except OSError:
            continue
        if "u-questionItem" not in html:
            continue
        for item in parse_result_html(html):
            qtype = item.get("type")
            options = item.get("options") or []
            text = str(item.get("correct_text") or "").strip()
            if qtype == "completion":
                if not text:
                    continue
                truth = (text,)
            else:
                letters = item.get("correct_letters") or []
                if not letters:
                    continue
                picked = []
                for ch in letters:
                    idx = L.find(ch)
                    if 0 <= idx < len(options):
                        picked.append(norm_option(options[idx]))
                if not picked:
                    continue
                truth = tuple(sorted(picked))
            key = case_id(item)
            files_seen.setdefault(key, set()).add(fname)
            cases.setdefault(key, {
                "title": item["title"],
                "type": qtype,
                "options": options,
                "truth": truth,
            })
    return cases, {k: len(v) for k, v in files_seen.items()}


def build_cases() -> list[dict]:
    """``collect_cases`` 的列表形式，保持快照扫描的首次出现顺序。"""
    return list(collect_cases()[0].values())


def snapshot_fingerprint() -> str:
    """快照目录的内容指纹 —— 用例数会随快照增长而变，数字必须能对上是哪一批。"""
    digest = hashlib.md5()
    snap_dir = config.SNAPSHOT_DIR
    try:
        names = sorted(os.listdir(snap_dir))
    except OSError:
        names = []
    for fname in names:
        if not fname.endswith(".html"):
            continue
        path = os.path.join(snap_dir, fname)
        try:
            with open(path, "rb") as fp:
                digest.update(fname.encode("utf-8"))
                digest.update(fp.read())
        except OSError:
            continue
    return digest.hexdigest()[:12]


def imported_ids() -> set[tuple]:
    """补录记录对应的用例身份。只读 SQLite。"""
    conn = sqlite3.connect(
        "file:%s?mode=ro" % os.path.abspath(config.BANK_DB_PATH).replace("\\", "/"),
        uri=True,
    )
    try:
        rows = conn.execute(
            "SELECT type, title, options FROM questions WHERE via = 'snapshot-restore'"
        ).fetchall()
    finally:
        conn.close()
    ids = set()
    for qtype, title, options in rows:
        try:
            opts = json.loads(options or "[]")
        except json.JSONDecodeError:
            opts = []
        ids.add((qtype, _norm_text(title), tuple(sorted(str(o) for o in opts))))
    return ids


def _decimal_value(text: str) -> tuple[float, int] | None:
    """只有**有限小数/整数**才返回 (数值, 小数位数)；分数返回 None。

    分数没有「小数位数」可言，旧实现把 1/3 和 3/32 都四舍五入到 0 位，
    于是把两个完全不同的答案判成相等。分数一律交给 ``math_equivalent``
    做精确判定，不再走数值近似档。
    """
    value = norm_text(text)
    if not value or "/" in value:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    decimals = len(value.partition(".")[2]) if "." in value else 0
    return number, decimals


def judge_completion(answer: str, truth: str) -> tuple[bool, str]:
    """填空题比对：文本相等 → 数学等价 → 数值近似 → 前缀。返回 (是否算对, 依据)。

    数值档只在**真值是有限小数**时启用，容差为真值末位的一半：
    真值 0.08 接受 1/12（=0.0833…，差 0.0033），但真值 3/32 不接受 1/3
    （真值是分数，没有“末位”可依据，只能按数学等价判）。

    ``symbolic`` 档来自 Math-Verify：``\\frac{1}{2}`` / ``0.5`` / ``1/2``
    判等；``0.0833`` / ``1/12`` 判不等（近似不是精确）。
    """
    got, want = norm_text(answer), norm_text(truth)
    if not got:
        return False, "empty"
    if got == want:
        return True, "exact"
    if math_equivalent(answer, truth):
        return True, "symbolic"
    want_dec = _decimal_value(want)
    if want_dec:
        number, decimals = want_dec
        got_num = as_number(got)
        if got_num:
            if decimals == 0:
                # 整数真值：只接受整数值，避免 15 被 14.9 蒙对
                if float(got_num[0]).is_integer() and got_num[0] == number:
                    return True, "numeric"
            else:
                tolerance = 0.5 * (10 ** -decimals)
                if abs(got_num[0] - number) <= tolerance + 1e-12:
                    return True, "numeric"
    # 文本相近：一方是另一方的前缀/后缀（「随机」vs「随机事件」）
    if got and want and (got.startswith(want) or want.startswith(got)):
        return True, "prefix"
    return False, "mismatch"


def answer_matches(answer: str, case: dict) -> bool:
    """把网关给的答案解析成「选项内容集合」，与真值集合比。"""
    if not answer:
        return False
    if case["type"] == "completion":
        return judge_completion(answer, case["truth"][0])[0]
    options = case["options"]
    letters = [p.strip().upper() for p in re.split(r"[#、,，;；/|]+", answer)
               if p.strip().upper() in L]
    if not letters:
        return text_key(answer) == text_key(" ".join(case["truth"]))
    picked = []
    for ch in letters:
        idx = L.find(ch)          # 字母的值才是下标
        if 0 <= idx < len(options):
            picked.append(norm_option(options[idx]))
    return tuple(sorted(picked)) == tuple(case["truth"])


def _options_key(options_raw, qtype: str) -> tuple[str, ...]:
    """与 ``Bank.find_official`` 比较选项时同口径的排序元组。"""
    return tuple(sorted(_norm_option(o, qtype) for o in (options_raw or [])))


def official_candidates(
    bank,
    case: dict,
    *,
    official: list[dict] | None = None,
) -> list[dict]:
    """与 find_official 同口径的可达官方候选（不看专业；看选项键与题干）。"""
    qtype = case["type"]
    target = _options_key(case["options"], qtype)
    rows = official if official is not None else [
        i for i in bank.all() if i.get("source") == "official"
    ]
    out = []
    for item in rows:
        if item["type"] != qtype or item.get("status") != "adopted":
            continue
        if _options_key(item.get("options"), qtype) != target:
            continue
        if _norm_text(item.get("title")) == _norm_text(case.get("title")) or (
            same_question_truncated(case.get("title"), item["title"])
        ):
            out.append(item)
    return out


def official_answer_matches(item: dict, case: dict) -> bool:
    """把可选官方记录的答案映射到用例选项后，与真值比对。"""
    translated = translate_answer(
        item.get("answer", ""),
        item.get("options") or [],
        case["options"],
        case["type"],
    )
    return answer_matches(translated or "", case)

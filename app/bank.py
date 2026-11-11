"""本地题库：自建题目存本地，支持相似题命中、AI 答案待确认、按专业隔离。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import unicodedata
import uuid
from typing import Any

from .formula_index import latex_plain
from .shape_key import has_image, image_key, same_question_truncated, text_key, truncated_match, url_fingerprint
from .bankdb import BankDB
from .parsing import normalize_type, parse_answer, parse_options

_URL_RE = re.compile(r"https?://\S+")


def _search_text(text: Any, image_text: dict[str, str] | None = None) -> str:
    """搜题用的归一文本：图片 URL 换成 OCR 文本（没有索引就丢掉 URL 噪声），再压低 LaTeX 噪音。"""
    value = str(text or "")
    if image_text is not None:
        value = _URL_RE.sub(lambda m: " " + image_text.get(m.group(0), " ") + " ", value)
    return _norm_text(latex_plain(value))


# LaTeX 命令 → 纯文本符号。同一道题在答题页是 LaTeX 文本、在结果页是图片 URL，
# 两者要归一到同一个键才能互认；命令映射不到的一律只留命令名本身。
_LATEX_COMMANDS = {
    "overline": "", "underline": "", "cup": "U", "cap": "I", "cdot": "*",
    "times": "*", "geq": ">=", "leq": "<=", "neq": "!=", "Rightarrow": "=>",
    "in": "in", "sum": "sum", "prod": "prod", "int": "int", "sqrt": "sqrt",
    "alpha": "a", "beta": "b", "lambda": "l", "mu": "m", "sigma": "s",
    "theta": "t", "Delta": "D", "rho": "r", "pi": "p", "omega": "w",
    "phi": "f", "psi": "p", "varepsilon": "e", "eta": "h", "xi": "x",
}

_LATEX_CMD_RE = re.compile(r"\\([a-zA-Z]+)\{([^{}]*)\}")
_LATEX_CMD_BARE_RE = re.compile(r"\\([a-zA-Z]+)")
_CANON_STRIP_RE = re.compile(r"[()\[\]{}$]")

# 嵌套分数：\frac{a}{b} → (a)/(b)。要循环替换直到收敛，
# 否则 \frac{1}{2} 的分母里再套一个分数时外层展不开。
_LATEX_FRAC_RE = re.compile(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_LATEX_SQRT_N_RE = re.compile(r"\\sqrt\s*\[([^\]]*)\]\s*\{([^{}]*)\}")
_LATEX_SQRT_RE = re.compile(r"\\sqrt\s*\{([^{}]*)\}")


def _flatten_latex(text: str) -> str:
    """把 LaTeX 命令压成等价的纯文本，让 LaTeX 题干和 OCR 文本能对齐。"""
    value = str(text or "")
    # 分数先循环展开（嵌套的也要展平），再走命令映射
    previous = ""
    while previous != value:
        previous = value
        value = _LATEX_FRAC_RE.sub(r"(\1)/(\2)", value)
    value = _LATEX_SQRT_N_RE.sub(r"sqrt[\1](\2)", value)
    value = _LATEX_SQRT_RE.sub(r"sqrt(\1)", value)
    value = _LATEX_CMD_RE.sub(
        lambda m: " " + _LATEX_COMMANDS.get(m.group(1), m.group(1)) + " " + m.group(2) + " ", value
    )
    value = _LATEX_CMD_BARE_RE.sub(
        lambda m: " " + _LATEX_COMMANDS.get(m.group(1), m.group(1)) + " ", value
    )
    return _CANON_STRIP_RE.sub("", value)


def canon_text(text: Any, image_text: dict[str, str] | None = None) -> str:
    """题目的「规范键」：图片 URL 换成 OCR 文本、LaTeX 压平，只剩可比对的字符。

    答题页给的是 LaTeX 文本 + 纯字母选项，结果页/题库存的是图片 URL；
    两者经同一个 canon 后应当相等，这样同一道题的两个来源能互相认出来。

    注意：只替换「索引里已有 OCR 文本」的 URL。没有 OCR 文本的图必须保留原样，
    否则两道只差一张图（而那张图还没 OCR）的题会被并成同一个键。
    """
    value = str(text or "")
    if image_text:
        value = _URL_RE.sub(
            lambda m: " " + image_text[m.group(0)] + " " if m.group(0) in image_text else m.group(0),
            value,
        )
    value = _flatten_latex(value)
    return "".join(ch for ch in value.lower() if ch.isalnum())

TYPE_LABELS = {"single": "单选题", "multiple": "多选题", "judge": "判断题", "completion": "填空题"}
MAX_TITLE = 2000
MAX_OPTIONS = 26
DEFAULT_SUBJECT = "默认"
SIMILARITY_THRESHOLD = 0.9
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
SAVE_DEBOUNCE_ITEMS = 1000
SAVE_DEBOUNCE_SECONDS = 0.8

_JUDGE_TRUE = {"对", "正确", "√", "✓", "是", "是的", "true", "t", "正确选项"}
_JUDGE_FALSE = {"错", "错误", "×", "✗", "x", "否", "不是", "false", "f", "错误选项"}

# 填空题的占位选项：请求里带这些、题库里存的是空列表，不归一化就永远匹配不上。
# 「你没有填写答案」是 OCS 在用户交白卷时传上来的另一种占位说法。
_PLACEHOLDER_OPTIONS = {
    "请输入答案", "请输入", "填写答案", "答案",
    "你没有填写答案", "未填写答案", "请作答",
}


def _strip_placeholder_options(qtype: str | None, options: list[Any]) -> list[str]:
    """去掉填空题的占位选项（OCS 会把「请输入答案」当选项传上来）。"""
    if qtype != "completion":
        return [str(o) for o in options]
    return [str(o).strip() for o in options if str(o).strip() not in _PLACEHOLDER_OPTIONS]


def _canon_options_key(options: list[str], qtype: str | None = None) -> tuple[str, ...]:
    """选项的规范键：同样把图片 URL 换成 OCR 文本、LaTeX 压平。

    题库存的是图片 URL 选项、请求带的是纯字母选项时，两边规范键对不上，
    只能靠「请求的选项全是字母」这一条单独放行（见 _is_positional_options）。
    """
    return tuple(sorted(canon_text(o) for o in options if str(o).strip()))


def _options_broken(options: list[str], qtype: str | None = None) -> bool:
    """选项本身是否已经不可用（重复或有空项）。

    这类记录是历史脏数据：结果页解析器曾把对错图标当成选项内容，
    于是判断题存成 ['正确','正确']、单选题存成 ['','','','']。

    两类坏法的危害程度不同：
    - **空选项**：匹配时对不上，自然落空 → 顶多是「查不到」，不会给出错答案；
    - **重复选项**：``translate_answer`` 按选项内容映射，重复项一律映射到第一个，
      于是「匹配得上但答案是错的」——实测 7 道判断题的正确答案被翻反
      （题库存「正确」，实际返回「错误」）。这类必须挡住，不能当权威答案。
    """
    if not options:
        return False
    if any(not str(o).strip() for o in options):
        return True
    if len(set(options)) != len(options):
        return True
    return False


# 选项前缀标签：``A. 北京`` / ``B、上海`` / ``(C) 上海`` / ``D．上海``
# OCS 有些页面把字母标签一起塞进选项文本，官方脚本的 ``removeRedundant``
# 会先剥掉它再比对。我们不剥的话，"A. 北京" 与 "北京" 对不上，
# 同一道题在答题页和题库里就成了两条记录。
_OPTION_LABEL_RE = re.compile(
    r"^\s*[\(\[【]?\s*[A-Za-z]\s*[\)\]】]?\s*[.．、,，:：]?\s*"
)


def _strip_option_label(text: str) -> str:
    """剥掉选项前面的字母标签（``A.`` ``B、`` ``(C)``）。"""
    value = str(text or "").strip()
    if not value:
        return value
    stripped = _OPTION_LABEL_RE.sub("", value, count=1)
    # 剥完必须还剩东西，且不能把整个选项吃掉（防止 "A" 这种单字母选项被剥成空）
    if stripped and stripped != value:
        # 原始前缀必须真的像字母标签（后面跟分隔符或空格），不能是 "A型" 这种
        if re.match(r"^\s*[\(\[【]?\s*[A-Za-z]\s*[\)\]】]?\s*[.．、,，:：]\s*", value):
            return stripped.strip()
        if re.match(r"^\s*[\(\[【]\s*[A-Za-z]\s*[\)\]】]\s*", value):
            return stripped.strip()
    return value


def _norm_option(text: Any, qtype: str | None = None) -> str:
    """判断题选项按语义归一，'对/正确/√/✓' 视为同一个选项。

    符号必须在归一化之前判断：_norm_text 会丢掉 ✓/✗ 这类非字母数字字符，
    先归一再比对会让图标形式的判断题选项变成空串。
    """
    if qtype == "judge":
        raw = str(text or "").strip().lower()
        if raw in _JUDGE_TRUE:
            return "正确"
        if raw in _JUDGE_FALSE:
            return "错误"
    norm = _norm_text(_strip_option_label(str(text or "")))
    if qtype == "judge":
        low = norm.lower()
        if low in _JUDGE_TRUE:
            return "正确"
        if low in _JUDGE_FALSE:
            return "错误"
    return norm

_HINTS = {
    "single": "单选题答案应为选项字母，如 A",
    "multiple": "多选题答案应为字母升序、# 连接，如 A#C",
    "judge": "判断题答案应为 正确 或 错误",
    "completion": "填空题答案直接填文本，多个空用 # 分隔",
}


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


# 这些符号参与语义：全部丢掉会让 |A|≠0 / |A|=0、A>B / A<B 归一化成同一题。
# 括号也保留，避免 (a+b)^2 与 a+b^2 被当成同一道题。
_MATH_SYMBOLS = frozenset("><=≠≤≥|+-−*/^%×÷√∞≈±∑∏∫°()[]{}")

# 图片 URL（含常见 CDN 前缀），用于判重时保留公式图之间的区分度
_IMAGE_URL_RE = re.compile(
    r"https?://[^\s\"'<>]+?\.(?:png|jpe?g|gif|bmp|webp)(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)



def _norm_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    out: list[str] = []
    last = len(text) - 1
    for index, ch in enumerate(text):
        if ch.isalnum() or ch in _MATH_SYMBOLS:
            out.append(ch)
        elif ch == "." and 0 < index < last and text[index - 1].isdigit() and text[index + 1].isdigit():
            # 只保留数字之间的小数点，句末句点仍然丢掉
            out.append(ch)
    return "".join(out)


def _norm_title_for_dedupe(value: Any) -> str:
    """判重专用的题干归一化：**保留图片 URL 的区分度**。

    ``_norm_text`` 会把 URL 里的 ``:`` ``.`` ``/`` 丢掉，只剩 ``http//edu-ima``
    这种残片 —— 于是「设 http://…9ac47dce」和「设 http://…6d486bc1」
    （公式图不同、题干文字相同）归一化成同一个键，判重时互相吞掉。
    实测 2026-11-10：``dedupe_near`` 一轮删掉 300 条，其中 6 条是官方答案。

    这里先把每张图片的 URL 换成稳定指纹（去掉易变的 host 前缀，保留路径与
    文件名），再走文字归一化。指纹不同 → 键不同 → 不会被误并。
    """
    text = unicodedata.normalize("NFKC", str(value or "")).lower()

    def _fingerprint(match: re.Match[str]) -> str:
        # 统一走 shape_key.url_fingerprint：这里原先自己复制了一份正则，
        # 且同样有「前缀分支漏了 _?」的 bug（_PhotoUploadUtils_ 剥不掉）。
        # 指纹只用于判等，多两字前缀不影响结果，但没必要留两份实现。
        return " img %s " % url_fingerprint(match.group(0))

    text = _IMAGE_URL_RE.sub(_fingerprint, text)
    return _norm_text(text)


def _has_image(value: Any) -> bool:
    """题干/选项里是否含图片 URL（含被截断的 URL 碎片）。"""
    text = str(value or "")
    return bool(_IMAGE_URL_RE.search(text) or re.search(r"https?://\S*", text, re.I))


def _has_image_options(options: Any) -> bool:
    """选项列表里是否有图片。"""
    if isinstance(options, dict):
        options = list(options.values())
    if not isinstance(options, (list, tuple)):
        return False
    return any(_has_image(o) for o in options)


def _image_compatible(item: dict[str, Any], target_image: str) -> bool:
    """图片指纹只在一方缺失时放行，交给文本匹配兜底。

    OCS 同一道题有时把图片以 base64 数组传来（请求带图、旧记录无指纹），
    有时又把图片 URL 直接写进题干；后者入库时指纹为空，会导致带图请求
    永远匹配不上。只有「请求有图、记录无指纹」这一向才放行——反方向
    （请求无图、记录有图）仍要求精确匹配，避免纯文字请求蹭到看图题。
    """
    item_image = str(item.get("image_hash") or "").strip()
    target = str(target_image or "").strip()
    if item_image == target:
        return True
    return bool(target) and not item_image


def _bigrams(text: str) -> set[str]:
    if len(text) <= 1:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def _dice(a: str, b: str) -> float:
    left, right = _bigrams(a), _bigrams(b)
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return 2 * len(left & right) / (len(left) + len(right))


def _shingle_hash(text: str) -> int:
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def simhash64(text: Any) -> str:
    """64 位 SimHash 指纹（十六进制），用于近似重复检测；对短题干比逐对 bigram 快得多。"""
    norm = _norm_text(text)
    shingles = _bigrams(norm) or {norm}
    weights = [0] * 64
    for shingle in shingles:
        value = _shingle_hash(shingle)
        for bit in range(64):
            weights[bit] += 1 if value & (1 << bit) else -1
    fingerprint = 0
    for bit, weight in enumerate(weights):
        if weight > 0:
            fingerprint |= 1 << bit
    return f"{fingerprint:016x}"


def hamming(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (TypeError, ValueError):
        return 64


def _norm_subject(value: Any) -> str:
    return str(value or DEFAULT_SUBJECT).strip() or DEFAULT_SUBJECT


_SOURCE_WRITE_RANK = {"official": 4, "manual": 3, "import": 2, "calibrated": 1, "ai": 1}


def _source_level(source: str, status: str) -> int:
    """答案来源等级：4 官方 > 3 人工/导入 > 2 校准/已采纳 AI > 1 单次 AI 待确认。

    **官方答案只有 adopted 才是 4 级。** official+pending 表示这条官方答案
    正处于冲突/待确认状态（例如被复核推翻、或历史答案不一致被降级），
    它仍然是官方来源，但不能算权威：早期这里对 official 无条件返回 4，
    于是 match() 的排序（_source_level 在前、adopted 在后）会让 pending 的
    官方记录压过已采纳的校准/AI 答案 —— 好答案被挤掉，请求转去重问 AI。
    pending 官方给 2 级（与校准同级）：排序时同级的 adopted 标志会先胜出，
    所以「已采纳的校准/AI」能压过它，而「单次 AI 待确认」（1 级）压不过它。
    """
    if source == "official":
        return 4 if status == "adopted" else 2
    if source in ("manual", "import"):
        return 3
    if source == "calibrated":
        return 2
    return 2 if status == "adopted" else 1


def _can_overwrite_source(existing: str, new: str) -> bool:
    """写入优先级：AI/校准同属 AI 层，不能覆盖人工/导入/官方答案。"""
    return _SOURCE_WRITE_RANK.get(new, -1) >= _SOURCE_WRITE_RANK.get(existing, -1)


def _can_update_source(existing: str, new: str) -> bool:
    """人工编辑可以修正任何来源；AI/校准仍受写入优先级限制。"""
    return new == "manual" or _can_overwrite_source(existing, new)


_NEGATIONS = set("不非无未没")


def _is_positional_options(options: list[str]) -> bool:
    """选项是否只剩 A/B/C/D 这类占位字母。

    OCS 答题页有时把选项内容做成图片，只传字母过来；此时字母本身就是位置，
    不能再按「选项内容」做映射（两边的内容一个是字母一个是图片 URL，永远对不上）。

    注意不能写成 ``o.strip() in _LETTERS``：Python 里空串是任何字符串的子串，
    ``'' in 'ABC'`` 为 True，空选项会被误判成位置占位。
    """
    if not options:
        return False
    return all(str(o).strip() and str(o).strip() in _LETTERS for o in options)


def is_bare_letter_options(options: list[str]) -> bool:
    """选项是否只有 A/B/C/D 这种纯字母（选项内容是图片，OCS 只传得出字母）。"""
    return _is_positional_options(options)


def translation_is_positional(source_options: list[str], target_options: list[str]) -> bool:
    """答案是否只能按「位置」翻译：目标选项是纯字母，源选项有内容。

    这种翻译把「源记录第 N 个选项」直接当成「目标第 N 个选项」。OCS 每次
    出卷/重做都可能重排选项（实测同一测验 10 题出现过 20 个不同 answer_key），
    顺序一变，字母就指向另一个选项。调用方必须能看出这是「猜位置」，
    不能当成内容已核对过的官方答案。
    """
    return (
        bool(target_options)
        and _is_positional_options(target_options)
        and not _is_positional_options(source_options)
    )


def translate_answer(answer: str, source_options: list[str], target_options: list[str],
                     qtype: str | None = None) -> str | None:
    """把选项顺序不同来源的答案按「选项内容」映射到目标顺序；无法映射返回 None。

    答案有两种形态，都要能翻译：
    - **字母**（题库主流形态）：先按 source_options 换成内容，再在 target_options 里定位；
    - **语义内容**（人工/导入直接写选项原文，见 ``_answer_semantic``）：
      直接在 target_options 里按内容定位。对外返回的永远是字母或原文。
    """
    answer = str(answer or "").strip()
    if qtype not in ("single", "multiple") or not answer:
        return answer
    parts = [part.strip().upper() for part in answer.split("#") if part.strip()]
    if not parts or any(part not in _LETTERS for part in parts):
        # 语义答案：不能再走「字母当下标」那条路，按内容逐个映射到目标选项。
        # 目标选项是纯字母（图片题的占位）时无从映射 —— 内容对不上位置，
        # 返回 None 让上层当作「答案不可用」，不要猜。
        content_parts = [part.strip() for part in answer.split("#") if part.strip()]
        if not content_parts or _is_positional_options(target_options):
            return None
        tgt = [_norm_option(option, qtype) for option in target_options]
        mapped: list[int] = []
        for part in content_parts:
            key = _norm_option(part, qtype)
            if not key or key not in tgt:
                return None
            index = tgt.index(key)
            if index not in mapped:
                mapped.append(index)
        return "#".join(_LETTERS[i] for i in sorted(mapped))
    if _is_positional_options(target_options):
        # 目标选项只剩字母：按位置对应，选项顺序在两个来源里都是 DOM 顺序。
        for part in parts:
            if _LETTERS.index(part) >= len(target_options):
                return None
        return "#".join(sorted(set(parts)))
    src = [_norm_option(option, qtype) for option in source_options]
    tgt = [_norm_option(option, qtype) for option in target_options]
    mapped: list[int] = []
    for part in parts:
        index = _LETTERS.index(part)
        if index >= len(src):
            return None
        try:
            target_index = tgt.index(src[index])
        except ValueError:
            return None
        if target_index not in mapped:
            mapped.append(target_index)
    return "#".join(_LETTERS[i] for i in sorted(mapped))


def _loose_option_key(text: Any, qtype: str | None) -> str:
    """把选项里的图片 URL 换成稳定指纹后再归一，用于跨场次比较。

    同一张公式图在不同出卷版本里 URL 的 host / 查询串会变（实测
    ``bab9cac5 → 5031cf4b``），``translate_answer`` 按原始 URL 比对会失败，
    于是「题库明明已经是官方答案」被显示成未同步、历史 episode 也判不了对错。
    URL 指纹只保留文件名主干，host/查询串变化不影响。
    """
    if qtype == "judge":
        return _norm_option(text, qtype)
    value = str(text or "")

    def replace(match: re.Match) -> str:
        return " img " + url_fingerprint(match.group(0)) + " "

    return _norm_option(_IMAGE_URL_RE.sub(replace, value), qtype)


def translate_answer_loose(answer: str, source_options: list[str],
                           target_options: list[str],
                           qtype: str | None = None) -> str | None:
    """先按选项原文严格映射；失败后再按「图片 URL 指纹」映射一次。

    只用于**比较/展示**（同步状态、历史对错判定），写库时仍必须用严格的
    ``translate_answer`` —— 宽松映射在两个选项归一后撞车时直接放弃，不猜。
    """
    mapped = translate_answer(answer, source_options, target_options, qtype)
    if mapped is not None:
        return mapped
    if qtype not in ("single", "multiple"):
        return None
    loose_src = [_loose_option_key(option, qtype) for option in (source_options or [])]
    loose_tgt = [_loose_option_key(option, qtype) for option in (target_options or [])]
    if not loose_src or not loose_tgt:
        return None
    if any(not value for value in loose_src) or any(not value for value in loose_tgt):
        return None
    if len(set(loose_src)) != len(loose_src) or len(set(loose_tgt)) != len(loose_tgt):
        return None
    return translate_answer(answer, loose_src, loose_tgt, qtype)


def _options_norm_list(options: list[str], qtype: str | None = None) -> list[str]:
    """选项内容的规范化形式，**保持原顺序**（与 ``_norm_options_key`` 的排序不同）。

    存进库里的 ``options_norm`` 就是它：字母答案 ``B`` 只有对照「当时这份选项顺序」
    才能解出语义，而原始选项文本会带 ``A.`` 前缀、全角符号、判断题的 对/√ 等写法。
    先归一化再落库，答案语义的换算（``_answer_semantic``）就不必每次重算。
    """
    return [_norm_option(option, qtype) for option in options]


def _answer_semantic(answer: str, options: list[str], qtype: str | None) -> str:
    """答案的语义形式（与选项顺序无关），入库时派生，供比较与修复使用。

    - 单选/多选：字母 → 所选选项的规范化内容，排序后 ``#`` 连接；
    - 判断题：``对/√`` 这类统一成「正确/错误」，字母形式按选项位置换算；
    - 填空/其它：答案原文（填空的答案本身就是语义）。

    换算不了（字母越界、指向空选项）返回空串 —— 明确表示「这条答案不可解释」，
    不要退回字母：字母在换序后指向的是另一个选项。
    """
    raw = str(answer or "").strip()
    if not raw:
        return ""
    if qtype == "judge":
        semantic = _norm_option(raw, "judge")
        if semantic in ("正确", "错误"):
            return semantic
        parts = [part.strip().upper() for part in raw.split("#") if part.strip()]
        if len(parts) == 1 and parts[0] in _LETTERS:
            norm = _options_norm_list(options, qtype)
            index = _LETTERS.index(parts[0])
            if index < len(norm) and norm[index] in ("正确", "错误"):
                return norm[index]
        return raw
    if qtype in ("single", "multiple"):
        parts = [part.strip().upper() for part in raw.split("#") if part.strip()]
        if not parts:
            return ""
        if any(part not in _LETTERS for part in parts):
            # 已经是内容形式的答案（人工/导入直接写选项原文）：原样保留
            return raw
        norm = _options_norm_list(options, qtype)
        picked: list[str] = []
        for part in parts:
            index = _LETTERS.index(part)
            if index >= len(norm) or not norm[index]:
                return ""
            picked.append(norm[index])
        return "#".join(sorted(set(picked)))
    return raw


def _answer_key(item: dict[str, Any]) -> str:
    """比较答案用的键：优先语义形式（选项顺序无关），缺失时退回原文。

    历史/坏数据可能没有（或算不出）answer_norm：这时用原文参与比较，
    保持与 P3 之前完全一样的行为，不让比较结果凭空变化。
    """
    return str(item.get("answer_norm") or "").strip() or str(item.get("answer") or "").strip()


def _negation_signature(text: str) -> frozenset[str]:
    return frozenset(ch for ch in _norm_text(text) if ch in _NEGATIONS)


def classify_mismatch(
    qtype: str, answer: str, options: list[str], bank_item: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """判断「答案用不上」的具体原因，给 OCS 侧显示准确的告警。

    官方脚本（v5.4.0）的黄条文案是「⚠️ 给出的答案和选项不匹配，可能是题库答案错误」，
    但它的触发条件写反了（只在"根本没解析出答案"时才显示，真正算错反而不显示）。

    我们把三种情况分开报，让脚本能显示真正准确的文案：
      - ``answer-not-in-options`` 答案字母指不到任何选项（题库答案过期/选项被改）
      - ``invalid-format``           答案格式不合法（判断题写成了字母、多选字母超范围）
      - ``broken-options``           选项本身坏（空项/重复项），匹配得上但答案是错的

    正常返回 None。
    """
    raw = str(answer or "").strip()
    if not raw:
        return None
    opts = list(options or [])

    # 1) 选项本身坏
    if _options_broken(opts, qtype):
        return {
            "kind": "broken-options",
            "reason": "选项内容有缺失或重复，无法可靠匹配",
            "hint": "请在管理台检查这道题的选项",
        }

    # 2) 答案格式不合法
    if qtype == "judge":
        # 判断题接受「正确/错误」这类语义词，也接受指向选项的字母
        # （A 就是第一个选项 —— 官方脚本就是这么发的）。
        if raw not in _JUDGE_TRUE and raw not in _JUDGE_FALSE:
            parsed = parse_answer(qtype, raw, opts)
            if parsed not in ("正确", "错误"):
                return {
                    "kind": "invalid-format",
                    "reason": f"判断题答案「{raw}」不是「正确」或「错误」",
                    "hint": _HINTS.get(qtype, "答案格式不对"),
                }
    else:
        try:
            parsed = parse_answer(qtype, raw, opts)
        except Exception:  # noqa: BLE001 - 解析异常当作格式问题
            parsed = None
        if parsed is None or (parsed != raw and qtype in ("single", "multiple")):
            # 字母超出选项范围 → 归为「指不到选项」，不是格式错。
            # 注意 parse_answer 对越界字母是**静默丢弃**的
            # （"A#D" 三个选项会解析成 "A"），所以不能只看 parsed。
            # 分隔符要先拆再逐个判断，不能先删掉 "#" 再 split
            # （"A#D".replace("#","") 会变成 "AD"，当成一个非法字母）。
            letters = [
                p.strip().upper()
                for p in re.split(r"[#、,，;；/|]+", raw)
                if p.strip()
            ]
            if letters and all(p in _LETTERS for p in letters) and any(
                _LETTERS.index(p) >= len(opts) for p in letters
            ):
                return {
                    "kind": "answer-not-in-options",
                    "reason": f"答案 {''.join(letters)} 超出了选项范围（只有 {len(opts)} 个选项）",
                    "hint": "可能是题库答案过期",
                }
            return {
                "kind": "invalid-format",
                "reason": f"答案「{raw}」不是有效的{qtype}答案",
                "hint": _HINTS.get(qtype, "答案格式不对"),
            }

    # 3) 答案指不到选项（题库里的答案指向的内容，当前选项里没有）
    if bank_item:
        mapped = translate_answer(raw, bank_item.get("options") or [], opts, qtype)
        if mapped is None:
            return {
                "kind": "answer-not-in-options",
                "reason": "题库答案指向的选项内容，在当前选项里找不到",
                "hint": "可能是题目被改过，或题库答案过期",
            }
    return None


def validate_item(data: dict[str, Any]) -> str | None:
    """校验题目数据，合法返回 None，否则返回错误信息。"""
    qtype = normalize_type(data.get("type"))
    if qtype is None:
        return "题型不支持，只支持单选/多选/判断"

    title = str(data.get("title") or "").strip()
    if not title:
        return "题干不能为空"
    if len(title) > MAX_TITLE:
        return f"题干过长（最多 {MAX_TITLE} 字）"

    options = parse_options(data.get("options"))
    if len(options) > MAX_OPTIONS:
        return f"选项过多（最多 {MAX_OPTIONS} 个）"
    if qtype in ("single", "multiple") and len(options) < 2:
        return f"{TYPE_LABELS[qtype]}至少需要 2 个选项"

    answer = str(data.get("answer") or "").strip()
    if not answer:
        return "答案不能为空"
    if parse_answer(qtype, answer, options) != answer:
        return f"答案「{answer}」与题型/选项不匹配（{_HINTS[qtype]}）"
    return None


class Bank:
    def __init__(self, path: str, db_path: str | None = None) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.db = BankDB(db_path) if db_path else None
        self._dirty_ids: set[str] = set()
        self._deleted_ids: set[str] = set()
        self._lock = threading.RLock()
        self._items: list[dict[str, Any]] = []
        # 记录级变更回调：由上层挂接（main 用它让答案缓存失效）。
        # 签名 (before, after)，after 为 None 表示删除；异常一律吞掉，不影响写库。
        self.on_change: Any = None
        self.semantic = None
        self._image_text: dict[str, str] = {}
        self._canon_index: dict[tuple[str, str], list[dict[str, Any]]] | None = None
        self._hash_index: dict[str, str] = {}
        # 规范记录分组的归一化键缓存：id → ((type, title, options), key)。
        # 只有题面/选项真的变了才重算归一化（_norm_title_for_dedupe 是逐条正则，
        # 1272 条全量重算是 ~30ms，而每次答题落盘都会触发一次）。
        self._canon_key_cache: dict[str, tuple[tuple, tuple]] = {}
        self._index_dirty = True
        self._save_lock = threading.Lock()
        self._save_timer: threading.Timer | None = None
        self._pending_save = False
        self._load_failed = False
        self._load()
        # 加载后立刻算一次规范记录标注（派生列，不落盘也不算错误）。
        self.recompute_canonicals()

    def set_semantic(self, store: Any) -> None:
        """接入可选的语义索引（app.semantic.SemanticStore）。"""
        self.semantic = store

    def set_image_text(self, mapping: dict[str, str]) -> None:
        """接入公式图片的 OCR 文本索引（图片 URL → 文字）。

        同一道题在答题页是 LaTeX 文本、在结果页是图片 URL，靠这个索引把两者对齐。
        """
        self._image_text = dict(mapping or {})
        self._canon_index = None

    def _json_is_fresh(self, items: list[dict[str, Any]]) -> bool:
        """json 快照是否比库里现有的数据新。

        库是空的（路径配错 / 新建）时，json 是唯一的真相来源，允许回填。
        库里有数据时，只有 json 更新（含更晚的 created）才允许覆盖 ——
        否则就是拿旧快照盖掉新数据。
        """
        if not items:
            return True
        db_items = self.db.load_all() if self.db is not None else []
        if not db_items:
            return True
        json_created = max(float(i.get("created") or 0) for i in items)
        db_created = max(float(i.get("created") or 0) for i in db_items)
        if json_created > db_created:
            return True
        # 时间相同但条目更多，也算更新（去重/合并后数量可能变）
        return len(items) >= len(db_items)

    def _load(self) -> None:
        if self.db is not None:
            rows = self.db.load_all()
            if rows:
                self._items = [self._normalize(i) for i in rows if isinstance(i, dict)]
                return
            # db 读不出记录时，可能是「库损坏」而不是「库为空」。
            # 这时如果拿 bank.json 覆盖 db，陈旧的 json 会把库里的真实数据
            # 二次抹掉（2026-11-10 实测：库损坏 + 陈旧 json = 1224 条变 777 条）。
            # 只有在确认 db 确实是空表（能正常读到 0 行）时才允许回填 json。
            if not self.db.is_readable():
                self._items = []
                self._load_failed = True
                return
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as fp:
                        data = json.load(fp)
                    items = data.get("items", []) if isinstance(data, dict) else data
                    items = [i for i in items if isinstance(i, dict)]
                    # json 必须比 db 新，否则它就是旧快照，回填等于用旧数据覆盖新库。
                    # （2026-11-10 实测：db 路径配错导致指向空库 → 旧的 bank.json
                    #   被回填进去，1224 条的库变成 777 条还以为"恢复成功"）
                    if not self._json_is_fresh(items):
                        self._items = []
                        self._load_failed = True
                        return
                    self._items = [self._normalize(i) for i in items]
                except (OSError, json.JSONDecodeError, ValueError):
                    self._items = []
                self.db.replace_all(self._items)
                return
            self._items = []
            return
        if not os.path.exists(self.path):
            self._write_now()
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            items = data.get("items", []) if isinstance(data, dict) else data
            self._items = [self._normalize(i) for i in items if isinstance(i, dict)]
        except (OSError, json.JSONDecodeError, ValueError):
            self._items = []

    @staticmethod
    def _norm_history(raw) -> list[dict[str, Any]]:
        history: list[dict[str, Any]] = []
        if isinstance(raw, list):
            for item in raw[-20:]:
                if isinstance(item, dict) and item.get("answer"):
                    history.append(
                        {
                            "answer": str(item["answer"]),
                            "source": str(item.get("source") or ""),
                            "at": float(item.get("at") or 0.0),
                            "wrong": bool(item.get("wrong")),
                        }
                    )
        return history

    @staticmethod
    def _norm_str_list(raw) -> list[str]:
        if not isinstance(raw, list):
            return []
        return [str(x).strip() for x in raw[-20:] if str(x).strip()]

    def _normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        qtype = normalize_type(raw.get("type")) or "single"
        answer = str(raw.get("answer") or "").strip()
        if qtype == "judge" and answer:
            answer = _norm_option(answer, "judge")
        options = parse_options(raw.get("options"))
        if qtype == "completion":
            options = []  # 填空题没有选项；忽略「请输入答案」等占位文本
        raw_source = str(raw.get("source") or "").strip()
        source = raw_source if raw_source in _SOURCE_WRITE_RANK else "manual"
        status = "pending" if str(raw.get("status") or "") == "pending" else "adopted"
        created = float(raw.get("created") or time.time())
        return {
            "id": str(raw.get("id") or _new_id()),
            "type": qtype,
            "title": str(raw.get("title") or "").strip(),
            "options": options,
            "answer": answer,
            "subject": _norm_subject(raw.get("subject")),
            "image_hash": str(raw.get("image_hash") or "").strip(),
            "sh": simhash64(raw.get("title")),
            # 题干图片指纹：与请求「是不是同一道题」的可靠依据。
            # 派生自 title，纯计算、可重复，旧记录读进来时会自动补算。
            "img_key": image_key(raw.get("title")),
            # 选项顺序归一化（P3）：与 options 同序的规范化内容，派生列。
            # 字母答案必须对照「入库时的选项顺序」才有意义，这份归一化结果
            # 让答案语义可随时解出来，也让「选项换序」能被比较出来。
            "options_norm": _options_norm_list(options, qtype),
            # 答案语义（P3）：与选项顺序无关的形式，派生列 —— 见 _answer_semantic。
            # 匹配/返回仍走字母（answer），这里只用于比较、修复和排查。
            "answer_norm": _answer_semantic(answer, options, qtype),
            "source": source,
            "level": _source_level(source, status),
            "via": str(raw.get("via") or "")[:80],
            "official_via": str(raw.get("official_via") or "")[:20],
            "status": status,
            "history": self._norm_history(raw.get("history")),
            "wrong_answers": self._norm_str_list(raw.get("wrong_answers")),
            "agree": int(raw.get("agree") or 0),
            "conflict": bool(raw.get("conflict")),
            "created": created,
            # 最后修改时间：每次写成功都应由调用方刷新（见 record_attempt/update/
            # mark_wrong/remember_answer）。老记录没有这个信息，用 created 当下界。
            "updated": float(raw.get("updated") or created),
            # 原始出处：只在入库时定一次，之后不随 via 变化。
            # via 会被每次作答覆盖（record_attempt），追踪「这条最早从哪来」要用 origin。
            "origin": str(raw.get("origin") or raw.get("via") or "").strip()[:120],
            # 规范记录 id：同一道题的多条记录都指向保留优先级最高的那条（可以是自己）。
            # **只挂不删** —— 由 recompute_canonicals() 统一计算，见那里的说明。
            "canonical_id": str(raw.get("canonical_id") or "").strip(),
        }

    def _notify_change(
        self, before: dict[str, Any] | None, after: dict[str, Any] | None
    ) -> None:
        """题库记录变更后的外部通知（main 用它清理对应答案缓存）。

        ``after=None`` 表示删除。回调异常不能影响写库，所以一律吞掉。
        """
        handler = self.on_change
        if handler is None:
            return
        try:
            handler(dict(before) if before else None, dict(after) if after else None)
        except Exception:  # noqa: BLE001 - 缓存清理失败不影响题库写入
            pass

    def _save(self) -> None:
        """SQLite 后端增量写；JSON 后端小题库同步落盘，大题库合并 0.8s 内的多次写。"""
        self._index_dirty = True
        self._canon_index = None
        # 结构/优先级变化都会影响规范记录的选取，所以每次落盘前重算一次。
        # 它只改 canonical_id、把变化行加进 dirty 集合，不会再调 _save（无递归）。
        self.recompute_canonicals()
        if self.db is not None:
            self._flush_db()
            return
        if len(self._items) < SAVE_DEBOUNCE_ITEMS:
            self._write_now()
            return
        with self._save_lock:
            self._pending_save = True
            if self._save_timer is not None:
                return
            timer = threading.Timer(SAVE_DEBOUNCE_SECONDS, self._flush_save)
            timer.daemon = True
            self._save_timer = timer
            timer.start()

    def _write_now(self) -> None:
        with self._lock:
            payload = json.dumps({"items": self._items}, ensure_ascii=False, indent=2)
        tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            fp.write(payload)
        os.replace(tmp, self.path)

    def _flush_save(self) -> None:
        with self._save_lock:
            self._save_timer = None
            if not self._pending_save:
                return
            self._pending_save = False
        self._write_now()

    def _flush_db(self) -> None:
        with self._lock:
            dirty = [item for item in self._items if item["id"] in self._dirty_ids]
            deleted = list(self._deleted_ids)
            self._dirty_ids.clear()
            self._deleted_ids.clear()
        if deleted:
            self.db.delete_many(deleted)
        if dirty:
            self.db.upsert_many(dirty)

    def close(self) -> None:
        self.flush()
        if self.db is not None:
            self.db.close()

    def export_json(self) -> str:
        with self._lock:
            return json.dumps({"items": self._items}, ensure_ascii=False, indent=2)

    def rebuild_fts(self) -> int:
        """重建 FTS5 搜索索引（虚表是派生数据，主表才是源）。"""
        if self.db is None:
            return 0
        return self.db.rebuild_fts()

    def checkpoint(self) -> bool:
        """把 WAL 合并进主库。

        批量写入/删除后必须调：WAL 不 checkpoint 会涨到几 MB，
        而且**主库文件被整体替换后**旧 WAL 会按旧页布局套到新主库上，
        读出来是 ``database disk image is malformed``（2026-11-10 实测）。
        """
        if self.db is None:
            return False
        return self.db.checkpoint()

    def flush(self) -> None:
        """立即把未落盘的改动写入文件（进程退出/备份前调用）。"""
        if self.db is not None:
            self._flush_db()
            return
        with self._save_lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
                self._save_timer = None
            pending = self._pending_save
            self._pending_save = False
        if pending:
            self._write_now()

    def get(self, item_id: str) -> dict[str, Any] | None:
        with self._lock:
            for item in self._items:
                if item["id"] == item_id:
                    return dict(item)
        return None

    def count(self, subject: str | None = None, status: str | None = None) -> int:
        with self._lock:
            return len(self._filter(subject, status))

    def all(self, subject: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(i) for i in self._filter(subject, status)]

    def subjects(self) -> list[str]:
        with self._lock:
            return sorted({item["subject"] for item in self._items})

    def _filter(self, subject: str | None, status: str | None) -> list[dict[str, Any]]:
        items = self._items
        if subject:
            wanted = _norm_subject(subject)
            items = [i for i in items if i["subject"] == wanted]
        if status:
            items = [i for i in items if i["status"] == status]
        return items

    @staticmethod
    def _index_key(qtype: str, norm_title: str, options_key: tuple[str, ...],
                   subject: str, image_hash: str) -> str:
        payload = "\x1f".join((qtype, norm_title, "|".join(options_key), subject, image_hash))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _ensure_canon_index(self) -> dict[tuple[str, str], list[dict[str, Any]]]:
        """(题型, 规范题干) → 候选题列表。

        规范题干把图片 URL 换成 OCR 文本、LaTeX 压平，因此同一道题的
        「LaTeX 文本形态」与「图片 URL 形态」会落到同一个键上。
        """
        if self._canon_index is not None:
            return self._canon_index
        index: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in self._items:
            if item.get("status") != "adopted":
                continue
            key = (item["type"], canon_text(item["title"], self._image_text))
            if not key[1]:
                continue
            index.setdefault(key, []).append(item)
        self._canon_index = index
        return index

    def _options_equivalent(self, item: dict[str, Any], options: list[str],
                            qtype: str) -> bool:
        """题库选项与请求选项是否指同一组选项。

        题库存的是图片 URL、请求带的是纯字母时，两边内容对不上，
        但 OCS 的选项顺序就是 DOM 顺序，字母即位置，可以直接对上。
        只有题库选项确实是图片时才放行——题库有文字选项时，说明内容可比，
        比不上就是另一道题，不能按位置蒙。
        """
        if not options:
            return not item.get("options")
        item_options = item.get("options") or []
        if not item_options:
            return False
        if len(item_options) != len(options):
            return False
        if _options_broken(item_options, qtype):
            # 选项坏掉的记录（重复/空项）不能用来定答案
            return False
        if _is_positional_options(options):
            if _is_positional_options(item_options):
                return True
            # 只有题库选项本身是图片时，OCS 才可能只传得出字母
            return all(_URL_RE.search(str(o)) for o in item_options)
        if _is_positional_options(item_options):
            return False
        return _canon_options_key(item_options, qtype) == _canon_options_key(options, qtype)

    def _find_by_canon(
        self, qtype: str, title: str, options: list[str], subject: str | None = None,
        official_only: bool = False,
    ) -> dict[str, Any] | None:
        """按规范题干找题；题干形态不同但内容等价时也能命中。

        规范键必须同时满足选项等价、来源可信（官方/人工/导入），
        避免把不同题合并到同一个答案上。
        """
        key = canon_text(title, self._image_text)
        if not key:
            return None
        with self._lock:
            candidates = self._ensure_canon_index().get((qtype, key)) or []
            if official_only:
                candidates = [i for i in candidates if i.get("source") == "official"]
            else:
                candidates = [i for i in candidates
                              if i.get("source") in ("official", "manual", "import")]
            hits = [item for item in candidates
                    if self._options_equivalent(item, options, qtype)]
            if subject is not None:
                same_subject = [i for i in hits if i["subject"] == _norm_subject(subject)]
                if same_subject:
                    hits = same_subject
            if not hits:
                return None
            # 位置映射的可靠性和「最近一次确认的选项顺序」强相关：越新的记录
            # 越可能是本次作答的顺序，旧的只是历史快照。
            hits.sort(key=lambda i: (i.get("subject") == DEFAULT_SUBJECT,
                                     -(i.get("updated") or i.get("created") or 0)))
            best = hits[0]
            translated = translate_answer(
                best.get("answer", ""), best.get("options") or [], options, qtype
            )
            if translated is None:
                return None
            result = dict(best)
            result["answer"] = translated
            if translation_is_positional(best.get("options") or [], options):
                result["_positional"] = True
            return result

    def find_by_position(
        self, qtype_raw: Any, title: str, options_raw: Any, subject: Any = None,
        image_hash: str = "",
    ) -> dict[str, Any] | None:
        """请求只给了 A/B/C/D 这种纯字母选项时，按「位置」找题库里的答案。

        OCS 答题页把选项内容做成图片，只传得出字母；此时字母就是位置，
        题库若存着同一道题（选项是图片 URL），可以直接按位置取答案。

        位置映射本来就是猜的，能用图片指纹锚定就必须锚定：同名题可能有多条
        记录（不同场次各建一条），答案字母只在各自的选项顺序里成立。
        带 ``image_hash`` 时只认指纹兼容的记录，并优先取最近更新的那条。
        """
        qtype = normalize_type(qtype_raw)
        if qtype is None:
            return None
        options = parse_options(options_raw)
        if not _is_positional_options(options):
            return None
        target_image = str(image_hash or "").strip()
        canon = canon_text(title, self._image_text)
        if not canon:
            return None
        with self._lock:
            candidates = self._ensure_canon_index().get((qtype, canon)) or []
            hits = []
            for item in candidates:
                item_options = item.get("options") or []
                if len(item_options) != len(options):
                    continue
                if _options_broken(item_options, qtype):
                    continue
                # 题库选项必须是图片（OCS 才会只传得出字母）
                if not all(_URL_RE.search(str(o)) for o in item_options):
                    continue
                # 只有请求带指纹时才用它锚定；没带指纹的老客户端维持原行为，
                # 不能因为记录有指纹就把它们全排除掉。
                if target_image and not _image_compatible(item, target_image):
                    continue
                if subject is not None and item["subject"] != _norm_subject(subject):
                    continue
                hits.append(item)
            if not hits:
                return None
            hits.sort(key=lambda i: (i.get("subject") == DEFAULT_SUBJECT,
                                     -(i.get("updated") or i.get("created") or 0)))
            best = hits[0]
            answer = str(best.get("answer") or "").strip()
            if not answer:
                return None
            result = dict(best)
            if qtype in ("single", "multiple"):
                letters = answer.upper().replace("#", "")
                if letters and all(c in _LETTERS for c in letters):
                    # 题库答案就是字母；请求的字母即位置，直接沿用
                    result["answer"] = answer.upper()
            result["_match"] = "position"
            result["_positional"] = True
            return result

    def _ensure_index(self) -> None:
        if not self._index_dirty:
            return
        by_id = {item["id"]: item for item in self._items}
        index: dict[str, str] = {}
        for item in self._items:
            key = self._index_key(
                item["type"], _norm_text(item["title"]),
                self._norm_options_key(item["options"], item["type"]),
                item["subject"], item.get("image_hash") or "",
            )
            current = by_id.get(index.get(key, ""))
            if current is None or self._rank_item(item) > self._rank_item(current):
                index[key] = item["id"]
        self._hash_index = index
        self._index_dirty = False

    def _fast_lookup(self, qtype: str, norm_title: str, options_key: list[str],
                     subject: str, image_hash: str) -> dict[str, Any] | None:
        """题干/选项/专业完全一致的 O(1) 命中，避免长题库每次全表扫描。"""
        with self._lock:
            self._ensure_index()
            target = self._index_key(qtype, norm_title, tuple(options_key), subject, image_hash)
            item_id = self._hash_index.get(target)
            if not item_id:
                return None
            for item in self._items:
                if item["id"] == item_id:
                    return item
        return None

    def query(
        self,
        subject: str | None = None,
        status: str | None = None,
        keyword: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """按专业/状态/关键词筛选并分页，返回（当前页, 总数）。"""
        with self._lock:
            items = list(self._items)
        if subject:
            wanted = _norm_subject(subject)
            items = [i for i in items if i["subject"] == wanted]
        if status == "conflict":
            items = [i for i in items if i.get("conflict")]
        elif status in ("pending", "adopted"):
            items = [i for i in items if i["status"] == status]
        keyword = str(keyword or "").strip().lower()
        if keyword:
            fts_ids = self.db.search_ids(keyword) if (self.db is not None and len(keyword) >= 3) else []
            if fts_ids:
                allowed = set(fts_ids)
                items = [i for i in items if i["id"] in allowed]
            else:
                items = [
                    i
                    for i in items
                    if keyword in i["title"].lower()
                    or any(keyword in option.lower() for option in i["options"])
                ]
        items.sort(key=lambda i: i.get("created") or 0, reverse=True)
        total = len(items)
        offset = max(0, int(offset or 0))
        if offset:
            items = items[offset:]
        if limit:
            items = items[: max(0, int(limit))]
        return [dict(i) for i in items], total

    def match(
        self, qtype_raw: Any, title: str, options_raw: Any, subject: Any = None, image_hash: str | None = None,
        semantic: bool = False,
    ) -> dict[str, Any] | None:
        """同一专业内：题型/选项一致。文本题按题干相似度 ≥ 0.9；图片题按图片指纹精确匹配。

        semantic=True 且本地 embedding 模型可用时，字符相似度不够的题再走一次向量召回。
        """
        qtype = normalize_type(qtype_raw)
        if qtype is None:
            return None
        options = _strip_placeholder_options(qtype, parse_options(options_raw))
        target_options = sorted(_norm_option(o, qtype) for o in options)
        target_title = _norm_text(title)
        target_image = str(image_hash or "").strip()
        wanted = _norm_subject(subject)
        target_skeleton = text_key(title)

        # 官方答案优先：同题存在官方记录时，AI/校准答案不得抢走匹配。
        # （我们早期把请求题干截到 300 字，图片题的完整官方题干更长，匹配不上时
        #   会让被截断的 AI 记录「完全匹配」——实测官方答案 6 被 AI 的 4 顶掉。）
        if has_image(title):
            official_hit = self.find_official(qtype, title, options, target_image)
            if official_hit is not None:
                result = dict(official_hit)
                result["_score"] = 1.0
                result["_match"] = "official-truncated"
                return result

        fast = self._fast_lookup(qtype, target_title, target_options, wanted, target_image)
        if fast is not None:
            translated = translate_answer(fast.get("answer", ""), fast.get("options") or [], options, qtype)
            if translated is not None:
                result = dict(fast)
                result["answer"] = translated
                result["_score"] = 1.0
                result["_match"] = "exact-hash"
                return result

        best: dict[str, Any] | None = None
        best_score = 0.0
        best_rank: tuple = ()
        candidates: list[dict[str, Any]] = []
        with self._lock:
            for item in self._items:
                if item["type"] != qtype or item["subject"] != wanted:
                    continue
                if not _image_compatible(item, target_image):
                    continue
                if len(item["options"]) != len(options):
                    continue
                if sorted(_norm_option(o, qtype) for o in item["options"]) != target_options:
                    continue
                candidates.append(item)
                same_image = (item.get("image_hash") or "") == target_image
                score = 1.0 if (target_image and same_image) else _dice(target_title, _norm_text(item["title"]))
                rank = (_source_level(str(item.get("source") or ""), str(item.get("status") or "")),
                        1 if item.get("status") == "adopted" else 0)
                if score > best_score + 1e-9 or (abs(score - best_score) <= 1e-9 and rank > best_rank):
                    best, best_score, best_rank = item, score, rank

        match_kind = "exact" if best_score >= 0.999 else "similar"
        # 语义召回需要选项集合锚定「同一道题」。填空题 options 为空，候选池
        # 退化成同专业全部填空题；题干又常是「设/若随机变量 + 图片 URL」，
        # BGE 余弦轻松 0.9+，但两句话根本不是同一题（实测 17 条无库记录的
        # 填空题全部被语义错配顶包，严格判定 0 对 17 错）。因此无选项的题
        # 不走语义，交给缓存/AI。
        if (best_score < SIMILARITY_THRESHOLD and semantic and options
                and not target_image and self.semantic is not None):
            try:
                if self.semantic.available():
                    hit = self.semantic.best_match(title, candidates)
                else:
                    hit = None
            except Exception:  # noqa: BLE001 - 语义层异常不影响字符匹配
                hit = None
            if hit is not None:
                candidate, score = hit
                # 否定词一变答案通常就反转，语义再像也不复用
                if _negation_signature(candidate.get("title") or "") == _negation_signature(title):
                    best, best_score = candidate, score
                    match_kind = "semantic"

        if best is None or best_score < SIMILARITY_THRESHOLD:
            return None
        translated = translate_answer(best.get("answer", ""), best.get("options") or [], options, qtype)
        if translated is None:
            return None
        result = dict(best)
        result["answer"] = translated
        result["_score"] = round(best_score, 3)
        result["_match"] = match_kind
        return result

    def search(self, text: str, limit: int = 10, image_text: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """按题干相似度检索题库（不要求题型/选项一致），用于截图搜题等模糊查找。

        image_text 是图片 URL → OCR 文本的索引，传入后公式图片题也能按文字匹配。
        """
        target = _search_text(text, image_text)
        if not target:
            return []
        with self._lock:
            scored = []
            for item in self._items:
                score = _dice(target, _search_text(item["title"], image_text))
                if score > 0:
                    scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        results = []
        for score, item in scored[: max(1, min(int(limit or 10), 50))]:
            results.append(
                {
                    "id": item["id"],
                    "type": item["type"],
                    "title": item["title"],
                    "answer": item["answer"],
                    "subject": item["subject"],
                    "source": item.get("source"),
                    "status": item.get("status"),
                    "conflict": bool(item.get("conflict")),
                    "score": round(score, 3),
                }
            )
        return results

    def record_attempt(self, item_id: str, answer: str, source: str = "ai", allow_adopt: bool = True,
                       via: str = "") -> dict[str, Any] | None:
        """记录一次作答：答案一致则累计，连续 2 次一致自动采纳；不同则旧答案入历史并标冲突。

        ``source`` 是本次作答的来源；AI/校准同属 AI 层，不能覆盖官方、人工或导入答案。
        """
        answer = str(answer or "").strip()
        if not answer:
            return None
        source = str(source or "ai").strip()
        if source not in _SOURCE_WRITE_RANK:
            source = "ai"
        with self._lock:
            for index, item in enumerate(self._items):
                if item["id"] != item_id:
                    continue
                before = dict(item)
                existing_answer = str(item.get("answer") or "").strip()
                if existing_answer and not _can_overwrite_source(str(item.get("source") or "manual"), source):
                    # 已有的官方/人工/导入答案不允许被 AI 尝试覆盖或降级
                    return dict(item)
                old_via = str(item.get("via") or "")
                if via:
                    item["via"] = str(via)[:80]
                wrong_answers = item.get("wrong_answers") or []
                if answer == item["answer"]:
                    # 「最后修改时间」只在真有变化时刷新：重复作答同一答案、
                    # 且 via 也没变，不算修改。
                    changed = str(item.get("via") or "") != old_via
                    if item["status"] == "pending" and answer not in wrong_answers:
                        item["agree"] = int(item.get("agree") or 0) + 1
                        changed = True
                        if allow_adopt and item["agree"] >= 2:
                            item["status"] = "adopted"
                            item["conflict"] = False
                            item["level"] = _source_level(str(item.get("source") or source), "adopted")
                    if changed:
                        item["updated"] = time.time()
                    self._items[index] = item
                    self._dirty_ids.add(item_id)
                    self._save()
                    self._notify_change(before, item)
                    return dict(item)
                history = list(item.get("history") or [])
                if item["answer"]:
                    history.append({"answer": item["answer"], "source": item.get("source") or "", "at": time.time()})
                item["history"] = history[-20:]
                item["answer"] = answer
                item["source"] = source
                item["agree"] = 0
                item["conflict"] = True
                # 已采纳答案出现不一致时降级回待确认，不能继续秒回
                item["status"] = "pending"
                item["level"] = _source_level(source, "pending")
                item["updated"] = time.time()
                self._items[index] = item
                self._dirty_ids.add(item_id)
                self._save()
                self._notify_change(before, item)
                return dict(item)
            return None

    def add(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            item = self._normalize({**data, "id": _new_id(), "created": time.time()})
            self._items.append(item)
            self._dirty_ids.add(item["id"])
            self._save()
            return dict(item)

    def update(self, item_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            for index, item in enumerate(self._items):
                if item["id"] != item_id:
                    continue
                before = dict(item)
                data = dict(data or {})
                # 原始出处只在入库时定一次，更新接口不允许改写它。
                data.pop("origin", None)
                existing_source = str(item.get("source") or "manual")
                requested_source = str(data.get("source") or "").strip()
                if requested_source and requested_source not in _SOURCE_WRITE_RANK:
                    requested_source = "manual"
                existing_answer = str(item.get("answer") or "").strip()
                source_allowed = (
                    (not requested_source)
                    or (not existing_answer)
                    or _can_update_source(existing_source, requested_source)
                )
                if not source_allowed:
                    # 低优先级来源不能改答案，也不能改状态；只保留非敏感字段
                    data = {
                        key: value for key, value in data.items()
                        if key not in ("answer", "source", "status", "conflict")
                    }
                # P3：只改选项、不改答案时，字母答案必须跟着「选项内容」走。
                # 不翻译的话，同一个字母会静默指到新顺序里的另一个选项 ——
                # 这正是「按字母存答案、选项一换序就答错」的入库侧来源。
                translated_answer: str | None = None
                answer_lost = False
                if "options" in data and existing_answer and not str(data.get("answer") or "").strip():
                    old_options = list(item.get("options") or [])
                    new_options = parse_options(data.get("options"))
                    if new_options != old_options:
                        translated_answer = translate_answer(
                            existing_answer, old_options, new_options, item["type"])
                        if translated_answer is None:
                            answer_lost = True
                new_answer = str(data.get("answer") or "").strip()
                history = list(item.get("history") or [])
                conflict = bool(item.get("conflict"))
                agree = int(item.get("agree") or 0)
                changed_answer = bool(new_answer) and new_answer != item["answer"]
                if changed_answer:
                    history = (
                        history
                        + [{"answer": item["answer"], "source": item.get("source") or "", "at": time.time()}]
                    )[-20:]
                    conflict = True
                    agree = 0
                merged = {
                    **item,
                    **data,
                    "id": item_id,
                    "created": item.get("created") or time.time(),
                    "updated": time.time(),
                    "history": history,
                    "conflict": conflict,
                    "agree": agree,
                }
                if changed_answer:
                    merged["status"] = "adopted"  # 手动改答案视为已确认
                if "conflict" in data:
                    merged["conflict"] = bool(data["conflict"])
                if requested_source and source_allowed:
                    merged["source"] = requested_source
                elif changed_answer:
                    # 人工编辑：官方保持官方，其余升级为人工
                    merged["source"] = existing_source if existing_source == "official" else "manual"
                else:
                    merged["source"] = existing_source
                if answer_lost:
                    # 答案所指的选项内容在新选项里彻底没了：留着字母只会答错题。
                    # 清空答案、降为待确认并标冲突，让管理员看到并重填。
                    merged["answer"] = ""
                    merged["status"] = "pending"
                    merged["agree"] = 0
                    merged["conflict"] = True
                elif translated_answer is not None and translated_answer != existing_answer:
                    merged["answer"] = translated_answer
                self._items[index] = self._normalize(merged)
                self._dirty_ids.add(item_id)
                self._save()
                self._notify_change(before, self._items[index])
                return dict(self._items[index])
            return None

    def promote(self, item_id: str) -> dict[str, Any] | None:
        """人工采纳：来源升级为 manual；官方题保持官方，只改状态。"""
        item = self.get(item_id)
        if item and item.get("source") == "official":
            return self.update(item_id, {"status": "adopted"})
        return self.update(item_id, {"status": "adopted", "source": "manual"})

    def _official_peers(
        self, qtype: str, title: str, options: list[str], image_hash: str = ""
    ) -> list[dict[str, Any]]:
        """同一道题的全部候选记录；调用方必须已持有 ``self._lock``。

        闸门与 dedupe/canonical 一致：题干保留图片 URL 指纹、选项集合相同、
        不跨多个具体专业、图片题的选项内容指纹一致；反馈没带图片指纹而
        同题存在多个不同指纹时返回空（无法判断该改哪一条）。
        """
        target_key = (
            qtype,
            _norm_title_for_dedupe(title),
            self._norm_options_key(options, qtype),
        )
        peers = [
            item for item in self._items
            if self._canon_key_cached(item) == target_key
        ]
        specific = {
            str(item.get("subject") or "")
            for item in peers
            if str(item.get("subject") or "") != DEFAULT_SUBJECT
        }
        if len(specific) > 1:
            return []
        if any(_has_image(item.get("title")) or _has_image_options(item.get("options"))
               for item in peers):
            if not self._same_option_contents(peers):
                return []
        target_image = str(image_hash or "").strip()
        if target_image:
            return [item for item in peers
                    if _image_compatible(item, target_image)]
        hashes = {
            str(item.get("image_hash") or "")
            for item in peers
            if str(item.get("image_hash") or "")
        }
        if len(hashes) > 1:
            # 反馈页没给图片指纹，同题又存在多张不同的图：无法判断该改哪一条。
            return []
        return peers

    def page_official_answer(
        self,
        qtype_raw: Any,
        title: str,
        options_raw: Any,
        *,
        image_hash: str = "",
    ) -> dict[str, Any] | None:
        """同题来自结果页（page）的官方答案；paper 反馈据此判断是否让路。"""
        qtype = normalize_type(qtype_raw)
        if qtype is None:
            return None
        options = parse_options(options_raw)
        with self._lock:
            peers = self._official_peers(qtype, title, options, image_hash)
        pages = [
            item for item in peers
            if str(item.get("source") or "") == "official"
            and str(item.get("official_via") or "") == "page"
            and str(item.get("answer") or "").strip()
        ]
        if not pages:
            return None
        pages.sort(key=lambda i: (
            -(i.get("updated") or i.get("created") or 0),
            -(i.get("created") or 0),
        ))
        return dict(pages[0])

    def official_sibling_answer(
        self,
        qtype_raw: Any,
        title: str,
        options_raw: Any,
        *,
        exclude_ids: Any = (),
        image_hash: str = "",
    ) -> dict[str, Any] | None:
        """同题**其它**官方记录给出的唯一答案；供自动裁决冲突使用。

        安全闸门与 ``align_official`` 相同（``_official_peers``）。只收官方
        来源、答案非空、且不在 ``exclude_ids`` 里的记录，并把答案按目标选项
        顺序翻译成字母。多条官方记录指向不同答案时返回 ``None`` —— 官方自己
        都不一致，就不能自动改。
        """
        qtype = normalize_type(qtype_raw)
        if qtype is None:
            return None
        options = parse_options(options_raw)
        excluded = {str(item_id) for item_id in (exclude_ids or ())}
        with self._lock:
            peers = self._official_peers(qtype, title, options, image_hash)
            officials = [
                item for item in peers
                if str(item.get("source") or "") == "official"
                and str(item.get("answer") or "").strip()
                and str(item.get("id")) not in excluded
            ]
            if not officials:
                return None
            found: dict[str, dict[str, Any]] = {}
            for item in officials:
                translated = translate_answer(
                    str(item.get("answer") or ""),
                    list(item.get("options") or []),
                    options,
                    qtype,
                )
                if not translated:
                    continue
                found.setdefault(translated, item)
            if len(found) != 1:
                return None
            answer, item = next(iter(found.items()))
            return {
                "answer": answer,
                "record_id": item.get("id"),
                "official_via": item.get("official_via") or "",
            }

    def align_official(
        self,
        qtype_raw: Any,
        title: str,
        options_raw: Any,
        answer: str,
        *,
        via: str = "",
        image_hash: str = "",
    ) -> int:
        """把一条官方答案对齐到同一道题的全部官方记录，返回改动的记录数。

        OCS 同题常有多条入库记录（不同场次/不同来源各建一条）。反馈写入按
        ``find_exact`` 的列表顺序只更新命中的一条，而查询 ``find_official``
        按「非默认专业 + created 最新」挑另一条 —— 于是出现「记忆和题库都改了，
        下次却仍返回另一条旧官方答案」。这里把同题的所有官方记录统一成同一份
        「答案 + 选项顺序」，从源头消除这种分叉。

        安全闸门见 ``_official_peers``；paper 反馈不能覆盖已有的 page 结论
        （与 keep-page 规则一致）。
        """
        qtype = normalize_type(qtype_raw)
        answer = str(answer or "").strip()
        options = parse_options(options_raw)
        if qtype is None or not answer:
            return 0
        if qtype in ("single", "multiple") and len(options) < 2:
            return 0
        with self._lock:
            peers = self._official_peers(qtype, title, options, image_hash)
            if not peers:
                return 0
            officials = [
                item for item in peers
                if str(item.get("source") or "") == "official"
            ]
            if not officials:
                return 0
            target_semantic = _answer_semantic(answer, options, qtype)
            if via == "paper" and target_semantic:
                for item in officials:
                    if str(item.get("official_via") or "") != "page":
                        continue
                    page_semantic = (
                        str(item.get("answer_norm") or "").strip()
                        or _answer_semantic(
                            str(item.get("answer") or ""),
                            item.get("options") or [],
                            str(item.get("type") or ""),
                        )
                    )
                    if page_semantic and page_semantic != target_semantic:
                        # 结果页（page）比试卷接口（paper）更权威，保留 page 结论。
                        return 0
            changed = 0
            for item in officials:
                current_answer = str(item.get("answer") or "").strip()
                current_options = list(item.get("options") or [])
                if (
                    current_answer == answer
                    and current_options == options
                    and item.get("status") == "adopted"
                ):
                    continue
                payload: dict[str, Any] = {
                    "answer": answer,
                    "options": list(options),
                    "status": "adopted",
                    "source": "official",
                    "conflict": False,
                }
                if via and not str(item.get("official_via") or "").strip():
                    payload["official_via"] = via
                if self.update(item["id"], payload) is not None:
                    changed += 1
            # 官方答案一旦确定，同题的 AI/校准旧答案就不再需要人工裁决：
            # 清掉 conflict 标记，避免「答案回滚后冲突工作台仍挂着旧冲突」。
            # 只动机器来源（ai/calibrated）；manual/import 的异议仍留给人工。
            for item in peers:
                if str(item.get("source") or "") not in ("ai", "calibrated"):
                    continue
                if not item.get("conflict"):
                    continue
                if self.update(item["id"], {"conflict": False}) is not None:
                    changed += 1
            return changed

    @staticmethod
    def _norm_options_key(options: list[str], qtype: str | None = None) -> tuple[str, ...]:
        return tuple(sorted(_norm_option(o, qtype) for o in options))

    def _find_exact_unlocked(
        self, qtype: str, title: str, options: list[str], subject: Any, image_hash: str = ""
    ) -> dict[str, Any] | None:
        wanted = _norm_subject(subject)
        target_title = _norm_text(title)
        target_options = self._norm_options_key(options, qtype)
        for item in self._items:
            if item["type"] != qtype or item["subject"] != wanted:
                continue
            if not _image_compatible(item, image_hash):
                continue
            if self._norm_options_key(item["options"], qtype) != target_options:
                continue
            if _norm_text(item["title"]) == target_title:
                return item
        return None

    def find_answer_mismatch(
        self, qtype_raw: Any, title: str, options_raw: Any, subject: Any = None
    ) -> dict[str, Any] | None:
        """找出「题库里有这道题，但答案对不上当前选项」的记录。

        官方脚本（OCS 5.4.0）遇到这种情况会显示黄条：
        「⚠️ 给出的答案和选项不匹配，可能是题库答案错误」。
        我们这边以前只是"匹配失败 → 当作没找到"，用户看不出是答案错了
        还是库里真没有，排查成本很高。

        典型成因：选项内容变了（题目被改过）、库里存的是字母但选项被重排、
        选项里带了 ``A.`` 这类字母标签导致按内容映射不上。
        """
        qtype = normalize_type(qtype_raw)
        if qtype is None:
            return None
        options = _strip_placeholder_options(qtype, parse_options(options_raw))
        if len(options) < 2:
            return None
        target_title = _norm_text(title)
        target_options = self._norm_options_key(options, qtype)
        with self._lock:
            for item in self._items:
                if item["type"] != qtype:
                    continue
                if item.get("status") != "adopted":
                    continue
                answer = str(item.get("answer") or "").strip()
                if not answer:
                    continue
                if _norm_text(item["title"]) != target_title:
                    continue
                # 库里答案指向的那个选项内容，在当前选项里彻底找不到了
                # → 答案与选项不匹配（题目被改过，或题库答案过期）。
                #
                # 注意：**不能**用「选项顺序不同」当不匹配 —— OCS 每次出卷都会
                # 重排选项，那是正常情况，translate_answer 按内容映射能处理。
                # 真正的信号是「答案所指的内容消失了」。
                parts = [p.strip().upper() for p in answer.split("#") if p.strip()]
                if not parts or any(p not in _LETTERS for p in parts):
                    continue
                bank_options = item.get("options") or []
                target_contents = [_norm_option(o, qtype) for o in options]
                missing = False
                for part in parts:
                    index = _LETTERS.index(part)
                    if index >= len(bank_options):
                        missing = True
                        break
                    content = _norm_option(bank_options[index], qtype)
                    if content and content not in target_contents:
                        missing = True
                        break
                if missing:
                    return {
                        "id": item["id"], "title": item["title"], "answer": answer,
                        "source": item.get("source"), "options": item.get("options") or [],
                    }
        return None

    def find_official(
        self, qtype_raw: Any, title: str, options_raw: Any, image_hash: str = ""
    ) -> dict[str, Any] | None:
        """跨专业查找精确匹配的官方题：官方答案不受专业标签限制，只有它能跨专业复用它。"""
        qtype = normalize_type(qtype_raw)
        if qtype is None:
            return None
        options = _strip_placeholder_options(qtype, parse_options(options_raw))
        target_title = _norm_text(title)
        target_options = self._norm_options_key(options, qtype)
        target_image = str(image_hash or "").strip()
        target_skeleton = text_key(title)
        with self._lock:
            hits = []
            truncated_hits = []
            for item in self._items:
                if item["type"] != qtype or item.get("source") != "official":
                    continue
                if item.get("status") != "adopted":
                    continue
                if not _image_compatible(item, target_image):
                    continue
                if self._norm_options_key(item["options"], qtype) != target_options:
                    continue
                if _options_broken(item["options"], qtype):
                    # 选项是坏的（重复/空项），按内容映射答案必然出错，不作为权威答案
                    continue
                if _norm_text(item["title"]) == target_title:
                    hits.append(item)
                    continue
                # 我们早期把请求题干截到 300 字（不是官方 OCS 截的 —— 官方 v5.4.0
                # 与浏览器里实装的 v5.3.1 都原样传 title，全文无题干长度上限）；
                # 公式图片题的完整题干在库里更长；
                # 文字骨架上是「前缀」关系、且图片 URL 序列一致时视为同一道题。
                if same_question_truncated(title, item["title"]):
                    truncated_hits.append(item)
            if not hits:
                hits = truncated_hits
            if not hits:
                # 题干形态不同（LaTeX 文本 vs 图片 URL）但内容等价时，按规范题干再找一次。
                return self._find_by_canon(qtype, title, options, official_only=True)
            # 同题多条官方记录时，选「最近确认过」的：created 新不代表答案新
            # （旧记录 created 更晚是常见的事故，见 align_official 的注释）。
            hits.sort(key=lambda i: (
                i.get("subject") == DEFAULT_SUBJECT,
                -(i.get("updated") or i.get("created") or 0),
                -(i.get("created") or 0),
            ))
            best = hits[0]
            translated = translate_answer(best.get("answer", ""), best.get("options") or [], options, qtype)
            if translated is None:
                return None
            result = dict(best)
            result["answer"] = translated
            return result

    def find_exact(
        self, qtype_raw: Any, title: str, options_raw: Any, subject: Any = None, image_hash: str = ""
    ) -> dict[str, Any] | None:
        qtype = normalize_type(qtype_raw)
        if qtype is None:
            return None
        options = _strip_placeholder_options(qtype, parse_options(options_raw))
        with self._lock:
            item = self._find_exact_unlocked(qtype, title, options, subject, image_hash)
            if item is not None:
                return dict(item)
            return self._find_by_canon(qtype, title, options, subject)

    def upsert_calibrated(
        self,
        qtype_raw: Any,
        title: str,
        options_raw: Any,
        answer: str,
        subject: Any = None,
        adopted: bool = True,
        alt: str | None = None,
        source: str = "calibrated",
        via: str = "",
    ) -> dict[str, Any] | None:
        """把校准结果写成标准答案（adopted）或冲突待确认（pending）。

        ``via`` 记录实际作答的模型名，官方答案回流时才能把正确/错误记到对应模型头上。
        """
        qtype = normalize_type(qtype_raw)
        answer = str(answer or "").strip()
        if qtype is None or not answer:
            return None
        source = str(source or "calibrated").strip()
        if source not in _SOURCE_WRITE_RANK:
            source = "calibrated"
        options = parse_options(options_raw)
        with self._lock:
            item = self._find_exact_unlocked(qtype, title, options, subject, "")
            existing_answer = str((item or {}).get("answer") or "").strip()
            if existing_answer and not _can_overwrite_source(str(item.get("source") or "manual"), source):
                # 已有的官方/人工/导入答案不得被 AI/校准结果覆盖
                return dict(item)
            history: list[dict[str, Any]] = list((item or {}).get("history") or [])
            if item and item.get("answer") and item["answer"] != answer:
                history.append({"answer": item["answer"], "source": item.get("source") or "", "at": time.time()})
            alt = str(alt or "").strip()
            if alt and alt != answer:
                history.append({"answer": alt, "source": "calibrated", "at": time.time()})

            payload = {
                "type": qtype,
                "title": title,
                "options": options,
                "answer": answer,
                "subject": subject if subject else (item or {}).get("subject"),
                "source": source,
                "status": "adopted" if adopted else "pending",
                "conflict": not adopted,
                "agree": 2 if adopted else 0,
                "history": history[-20:],
                "image_hash": "",
            }
            if via:
                payload["via"] = str(via)[:80]
            if item:
                merged = {**item, **payload, "id": item["id"],
                          "created": item.get("created") or time.time(), "updated": time.time()}
                normalized = self._normalize(merged)
                for index, existing in enumerate(self._items):
                    if existing["id"] == item["id"]:
                        self._items[index] = normalized
                        break
                self._dirty_ids.add(normalized["id"])
                self._save()
                return dict(normalized)
            new_item = self._normalize({**payload, "id": _new_id(), "created": time.time()})
            self._items.append(new_item)
            self._dirty_ids.add(new_item["id"])
            self._save()
            return dict(new_item)

    def mark_wrong(self, item_id: str) -> dict[str, Any] | None:
        """标记错题：清空当前答案、回到待确认；旧答案进历史与错题集，重答不会再被自动采纳。"""
        with self._lock:
            for index, item in enumerate(self._items):
                if item["id"] != item_id:
                    continue
                before = dict(item)
                old = item.get("answer") or ""
                history = list(item.get("history") or [])
                if old:
                    history.append(
                        {"answer": old, "source": item.get("source") or "", "at": time.time(), "wrong": True}
                    )
                wrong = list(item.get("wrong_answers") or [])
                if old and old not in wrong:
                    wrong.append(old)
                item["history"] = history[-20:]
                item["wrong_answers"] = wrong[-20:]
                item["answer"] = ""
                item["status"] = "pending"
                item["level"] = _source_level(str(item.get("source") or "ai"), "pending")
                item["agree"] = 0
                item["conflict"] = True
                item["updated"] = time.time()
                self._items[index] = item
                self._dirty_ids.add(item_id)
                self._save()
                self._notify_change(before, item)
                return dict(item)
            return None

    def dedupe(self, dry_run: bool = True) -> dict[str, Any]:
        """合并完全重复的题（同题型+同题干+同选项集，跨专业也算重复）。

        保留优先级：官方 > 人工/导入 > 校准 > AI，已采纳 > 待确认，具体专业 > 默认；
        答案不一致且无官方答案的分组不合并；文本相同但图指纹不同的题保持独立。

        题干用 ``_norm_title_for_dedupe``（保留图片 URL 指纹）而不是 ``_norm_text``：
        后者会把 URL 抹成 ``http//edu-ima`` 这种残片，导致「公式图不同、文字相同」
        的题归到同一组被互相吞掉（2026-11-10 实测一轮删掉 297 条，含 6 条官方答案）。
        """
        with self._lock:
            groups = self._dedupe_groups_unlocked()
            result = self._merge_groups(groups, dry_run)
            return {"dry_run": dry_run, "mode": "exact", **result}

    def _canon_key_cached(self, item: dict[str, Any]) -> tuple:
        """分组用的归一化键，按题面/选项内容做缓存（内容没变就直接复用）。"""
        raw = (item["type"], item["title"], tuple(item["options"]))
        cached = self._canon_key_cache.get(item["id"])
        if cached is not None and cached[0] == raw:
            return cached[1]
        key = (
            item["type"],
            _norm_title_for_dedupe(item["title"]),
            self._norm_options_key(item["options"], item["type"]),
        )
        self._canon_key_cache[item["id"]] = (raw, key)
        return key

    def _dedupe_groups_unlocked(self) -> list[list[dict[str, Any]]]:
        """完全重复候选组：同题型 + 同「保留图片指纹的题干」+ 同选项集。

        返回值里既有真重复，也有需要靠 ``_canonical_linkable`` / ``_merge_groups``
        再筛的候选组（多专业、答案冲突、图片选项不一致）。调用方必须过闸门。
        """
        base_groups: dict[tuple, list[dict[str, Any]]] = {}
        for item in self._items:
            base_groups.setdefault(self._canon_key_cached(item), []).append(item)
        # 删除过大量记录后清一次缓存，避免键越攒越多（正常情况远达不到阈值）
        if len(self._canon_key_cache) > len(self._items) * 2 + 100:
            live = {item["id"] for item in self._items}
            self._canon_key_cache = {
                key: value for key, value in self._canon_key_cache.items() if key in live}

        groups: list[list[dict[str, Any]]] = []
        for items in base_groups.values():
            by_hash: dict[str, list[dict[str, Any]]] = {}
            empty_hash: list[dict[str, Any]] = []
            for item in items:
                image_hash = item.get("image_hash") or ""
                if image_hash:
                    by_hash.setdefault(image_hash, []).append(item)
                else:
                    empty_hash.append(item)
            if not by_hash:
                groups.append(empty_hash)
                continue
            # 缺图指纹的条目按文本归到最大同图分组；不同图指纹仍视为不同题
            target = max(by_hash.values(), key=len)
            target.extend(empty_hash)
            groups.extend(by_hash.values())
        return groups

    def dedupe_near(self, max_distance: int = 3, dry_run: bool = True) -> dict[str, Any]:
        """近似重复：同题型/同选项集/同图指纹，SimHash 汉明距离 ≤ max_distance 且题面 Dice ≥ 0.72。

        只处理「同一道题换了标点/空格/少量错字」的情况；真正改写过的题不会合并（交给人工）。
        """
        distance = max(0, min(16, int(max_distance or 3)))
        with self._lock:
            buckets: dict[tuple, list[dict[str, Any]]] = {}
            for item in self._items:
                key = (
                    item["type"],
                    self._norm_options_key(item["options"], item["type"]),
                    item.get("image_hash") or "",
                )
                buckets.setdefault(key, []).append(item)

            groups: list[list[dict[str, Any]]] = []
            for items in buckets.values():
                if len(items) < 2:
                    continue
                parent = list(range(len(items)))

                def find(x: int) -> int:
                    while parent[x] != x:
                        parent[x] = parent[parent[x]]
                        x = parent[x]
                    return x

                for i in range(len(items)):
                    for j in range(i + 1, len(items)):
                        left, right = items[i], items[j]
                        if hamming(left.get("sh") or "", right.get("sh") or "") > distance:
                            continue
                        # 题面比较同样要保留图片 URL 的区分度：文字骨架相同但公式图
                        # 不同的两道题，文字 Dice 会高达 1.0，必须靠 URL 指纹挡下来。
                        if _dice(
                            _norm_title_for_dedupe(left["title"]),
                            _norm_title_for_dedupe(right["title"]),
                        ) < 0.72:
                            continue
                        root_i, root_j = find(i), find(j)
                        if root_i != root_j:
                            parent[root_j] = root_i
                clusters: dict[int, list[dict[str, Any]]] = {}
                for index, item in enumerate(items):
                    clusters.setdefault(find(index), []).append(item)
                groups.extend(cluster for cluster in clusters.values() if len(cluster) > 1)

            result = self._merge_groups(groups, dry_run)
            preview = [
                {"titles": [i["title"][:80] for i in group[:4]], "ids": [i["id"] for i in group[:4]],
                 "count": len(group)}
                for group in groups[:30]
            ]
            return {"dry_run": dry_run, "mode": "near", "max_distance": distance,
                    "preview": preview, **result}

    @staticmethod
    def _rank_item(item: dict[str, Any]) -> tuple:
        source = _source_level(str(item.get("source") or ""), str(item.get("status") or ""))
        has_answer = 1 if str(item.get("answer") or "").strip() else 0
        adopted = 1 if item.get("status") == "adopted" else 0
        specific = 0 if item.get("subject") == DEFAULT_SUBJECT else 1
        return (1 if source >= 4 else 0, has_answer, adopted, source, specific, -(item.get("created") or 0))

    @staticmethod
    def _same_option_contents(items: list[dict[str, Any]]) -> bool:
        """组内所有记录的选项内容（按图片 URL 指纹归一）是否完全一致。

        图片题的选项常被整条存成图片 URL，同一道题在不同来源下 URL 可能不同，
        所以比较时要先把 URL 换成指纹；指纹相同才算同一份选项。
        """
        fingerprints: set[tuple[str, ...]] = set()
        for item in items:
            options = item.get("options") or []
            key = tuple(sorted(_norm_title_for_dedupe(o) for o in options))
            fingerprints.add(key)
        return len(fingerprints) == 1

    def _merge_groups(self, groups: list[list[dict[str, Any]]], dry_run: bool) -> dict[str, Any]:
        merged = 0
        removed = 0
        conflicts: list[dict[str, Any]] = []
        for items in groups:
            if len(items) < 2:
                continue
            specific = {i.get("subject") for i in items if i.get("subject") != DEFAULT_SUBJECT}
            if len(specific) > 1:
                conflicts.append(
                    {"title": items[0]["title"][:80], "type": items[0]["type"],
                     "reason": "multi-subject", "subjects": sorted(specific), "ids": [i["id"] for i in items]}
                )
                continue
            # 答案比较用语义键（P3）：同一内容因选项换序而存成不同字母，不算冲突。
            answers = {_answer_key(i) for i in items if _answer_key(i)}
            if len(answers) > 1:
                official = {
                    _answer_key(i)
                    for i in items
                    if i.get("source") == "official" and _answer_key(i)
                }
                if len(official) != 1:
                    if not dry_run:
                        # 答案不一致且无官方裁决：AI/校准题降为待确认；人工/导入/官方答案不自动降级
                        for item in items:
                            if str(item.get("source") or "") in ("official", "manual", "import"):
                                continue
                            self.update(item["id"], {"status": "pending", "conflict": True})
                    conflicts.append(
                        {"title": items[0]["title"][:80], "type": items[0]["type"],
                         "reason": "answers", "answers": sorted(answers), "ids": [i["id"] for i in items]}
                    )
                    continue
            # 安全闸门：题干/选项含图片的题，只在「选项内容完全一致」时才允许合并。
            # 公式题的题干文字可能完全一样而图不同（实测「设 http://…9ac47dce」
            # 与「设 http://…6d486bc1」文字骨架相同），合并就等于丢掉一道真题。
            if any(_has_image(i.get("title")) or _has_image_options(i.get("options")) for i in items):
                if not self._same_option_contents(items):
                    conflicts.append(
                        {"title": items[0]["title"][:80], "type": items[0]["type"],
                         "reason": "image-options", "ids": [i["id"] for i in items]}
                    )
                    continue
            ordered = sorted(items, key=self._rank_item, reverse=True)
            best = ordered[0]
            wrong: list[str] = list(best.get("wrong_answers") or [])
            history: list[dict[str, Any]] = list(best.get("history") or [])
            best_answer = str(best.get("answer") or "").strip()
            for extra in ordered[1:]:
                for value in extra.get("wrong_answers") or []:
                    if value and value not in wrong:
                        wrong.append(value)
                extra_answer = str(extra.get("answer") or "").strip()
                if extra_answer and extra_answer != best_answer:
                    history.append(
                        {"answer": extra_answer, "source": extra.get("source") or "",
                         "at": time.time(), "wrong": True}
                    )
                    if extra_answer not in wrong:
                        wrong.append(extra_answer)
                history.extend(extra.get("history") or [])
            if not dry_run:
                payload: dict[str, Any] = {"wrong_answers": wrong[-20:], "history": history[-20:]}
                if best.get("subject") == DEFAULT_SUBJECT:
                    promoted = next(
                        (e["subject"] for e in ordered[1:] if e.get("subject") != DEFAULT_SUBJECT), None
                    )
                    if promoted:
                        payload["subject"] = promoted
                for extra in ordered[1:]:
                    self.delete(extra["id"])
                self.update(best["id"], payload)
            merged += 1
            removed += len(ordered) - 1
        return {
            "groups": merged,
            "removed": removed,
            "conflicts": conflicts[:50],
            "conflict_count": len(conflicts),
        }

    def _canonical_linkable(self, items: list[dict[str, Any]]) -> bool:
        """这组记录能不能挂到同一条规范记录下（与 dedupe 的合并闸门一致）。

        不能挂的情况：
        - 跨多个具体专业（同一道题在不同专业下可能答案不同）；
        - 答案互相矛盾且没有唯一的官方答案可以裁决；
        - 题干/选项含图片，但选项内容指纹不一致（文字相同、公式图不同 = 两道题）。
        """
        specific = {i.get("subject") for i in items if i.get("subject") != DEFAULT_SUBJECT}
        if len(specific) > 1:
            return False
        answers = {_answer_key(i) for i in items if _answer_key(i)}
        if len(answers) > 1:
            official = {
                _answer_key(i) for i in items
                if i.get("source") == "official" and _answer_key(i)
            }
            if len(official) != 1:
                return False
        if any(_has_image(i.get("title")) or _has_image_options(i.get("options")) for i in items):
            if not self._same_option_contents(items):
                return False
        return True

    def recompute_canonicals(self) -> dict[str, Any]:
        """重算 ``canonical_id``：同一道题的多条记录只挂不删，全部指向规范记录。

        规范记录按 ``_rank_item`` 选：官方 > 人工/导入 > 校准 > AI，
        已采纳 > 待确认，有答案 > 无答案，具体专业 > 默认，同等条件下取更早的。

        **它只是标注，不改变任何匹配/答题行为**（P2 阶段先攒数据）。
        每条记录都写自己的 canonical_id：组内非规范记录指向规范记录，
        规范记录指向自己，所以 ``GROUP BY canonical_id`` 就能拿到整组。
        """
        with self._lock:
            assigned: dict[str, str] = {}
            linked_groups = 0
            linked_members = 0
            skipped = 0
            for items in self._dedupe_groups_unlocked():
                if len(items) < 2:
                    continue
                if not self._canonical_linkable(items):
                    skipped += 1
                    continue
                ordered = sorted(
                    items, key=lambda i: (self._rank_item(i), i["id"]), reverse=True)
                canon = ordered[0]["id"]
                for item in ordered:
                    assigned[item["id"]] = canon
                linked_groups += 1
                linked_members += len(items)

            changed = 0
            for item in self._items:
                new = assigned.get(item["id"], item["id"])
                if item.get("canonical_id") != new:
                    item["canonical_id"] = new
                    self._dirty_ids.add(item["id"])
                    changed += 1
            return {"groups": linked_groups, "members": linked_members,
                    "skipped": skipped, "changed": changed}

    def find_by_id(self, item_id: str) -> dict[str, Any] | None:
        with self._lock:
            for item in self._items:
                if item.get("id") == item_id:
                    return dict(item)
        return None

    def delete(self, item_id: str) -> bool:
        with self._lock:
            for index, item in enumerate(self._items):
                if item["id"] == item_id:
                    del self._items[index]
                    self._deleted_ids.add(item_id)
                    self._save()
                    self._notify_change(item, None)
                    return True
            return False

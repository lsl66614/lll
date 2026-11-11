"""答案质量策略：题型路由、历史正确率加权、置信度判定。

这里只放纯函数，不依赖 FastAPI 或全局状态，方便单测；由 ``main.py`` 调用。
历史正确率来自官方答案回流（``stats.quality``），样本少时用 Laplace 平滑，
权重稳定落在 ``[MIN_WEIGHT, MAX_WEIGHT]``，不会因为一两条记录就大起大落。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable, Sequence

from .mathcheck import math_equivalent

# OCS 只支持四种题型；image 是额外的图片题路由
TYPE_ROUTE_KEYS = ("single", "multiple", "judge", "completion")
ROUTE_KEYS = (*TYPE_ROUTE_KEYS, "image")

MAX_ROUTE_ITEMS = 8

# 先验：相当于「先看过 4 对 4」的 50% 正确率
_PRIOR_OK = 4.0
_PRIOR_BAD = 4.0
MIN_WEIGHT = 0.5
MAX_WEIGHT = 1.5


def normalize_routes(raw: Any) -> dict[str, list[str]]:
    """清洗 ``settings.routes``：只保留合法题型，去重、限长、丢弃空值。"""
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, list[str]] = {}
    for key in ROUTE_KEYS:
        value = raw.get(key)
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            continue
        items: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text and text not in items:
                items.append(text)
            if len(items) >= MAX_ROUTE_ITEMS:
                break
        if items:
            cleaned[key] = items
    return cleaned


def _route_lookup(providers: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按 id 和 name 建索引；同名时 id 优先。"""
    lookup: dict[str, dict[str, Any]] = {}
    for provider in providers:
        name = str(provider.get("name") or "").strip()
        if name:
            lookup.setdefault(name, provider)
    for provider in providers:
        pid = str(provider.get("id") or "").strip()
        if pid:
            lookup[pid] = provider
    return lookup


def route_providers(
    providers: Sequence[dict[str, Any]],
    routes: dict[str, list[str]] | None,
    qtype: str,
    has_images: bool = False,
) -> list[dict[str, Any]]:
    """按题型调整模型顺序。

    - 图片题优先读 ``image`` 路由，没配再退回该题型路由；
    - 路由里的模型排在最前，其余模型按原顺序兜底，避免路由写错后全盘不可用；
    - 没有配置路由时原样返回，行为与旧版本一致。
    """
    result = list(providers)
    if not routes:
        return result

    keys = ("image", qtype) if has_images else (qtype,)
    wanted: list[str] = []
    for key in keys:
        if routes.get(key):
            wanted = list(routes[key])
            break
    if not wanted:
        return result

    lookup = _route_lookup(result)
    ordered: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in wanted:
        provider = lookup.get(str(item))
        if provider is not None and id(provider) not in seen:
            ordered.append(provider)
            seen.add(id(provider))
    for provider in result:
        if id(provider) not in seen:
            ordered.append(provider)
            seen.add(id(provider))
    return ordered


def provider_weight(
    rows: dict[str, dict[str, Any]] | None,
    provider_name: str,
    prior_ok: float = _PRIOR_OK,
    prior_bad: float = _PRIOR_BAD,
) -> float:
    """按官方答案回流的正确率估算模型权重。

    ``rows`` 可以是 ``quality_snapshot()["providers"]``（name -> {ok,bad}），
    也可以是 ``Stats.quality`` 原始字典（``p:name`` -> {ok,bad}）。
    """
    if not rows or not provider_name:
        return 1.0
    row = rows.get(provider_name) or rows.get(f"p:{provider_name}") or {}
    try:
        ok = max(0.0, float(row.get("ok") or 0))
        bad = max(0.0, float(row.get("bad") or 0))
    except (TypeError, ValueError):
        return 1.0
    rate = (ok + prior_ok) / (ok + bad + prior_ok + prior_bad)
    return round(min(MAX_WEIGHT, max(MIN_WEIGHT, rate * 2.0)), 4)


def sample_score(
    rows: dict[str, dict[str, Any]] | None,
    provider_name: str,
    confidence: float | None,
    use_confidence: bool = True,
) -> float:
    """一次作答的票重 = 模型历史正确率权重 × 置信度系数（0.5~1.5）。"""
    weight = provider_weight(rows, provider_name)
    if use_confidence and confidence is not None:
        try:
            value = min(1.0, max(0.0, float(confidence)))
        except (TypeError, ValueError):
            value = 1.0
        weight *= 0.5 + value
    return weight


@dataclass(frozen=True)
class VoteResult:
    winner: str | None
    scores: dict[str, float]
    counts: dict[str, int]
    share: float
    total_weight: float


def weighted_choice(
    samples: Iterable[tuple[str, str, float | None]],
    rows: dict[str, dict[str, Any]] | None = None,
    use_confidence: bool = True,
) -> VoteResult:
    """加权多数票。

    ``samples`` 每项是 ``(answer, provider_name, confidence)``。
    置信度缺失按 1.0 处理；票数相同时先比加权分，再比原始票数，
    最后按首次出现顺序决定，保证结果可复现。
    """
    scores: dict[str, float] = {}
    counts: dict[str, int] = {}
    total = 0.0
    for answer, provider_name, confidence in samples:
        if not answer:
            continue
        score = sample_score(rows, provider_name, confidence, use_confidence)
        scores[answer] = scores.get(answer, 0.0) + score
        counts[answer] = counts.get(answer, 0) + 1
        total += score

    if not scores:
        return VoteResult(None, {}, {}, 0.0, 0.0)

    winner = max(scores, key=lambda answer: (scores[answer], counts[answer]))
    share = round(scores[winner] / total, 4) if total else 0.0
    return VoteResult(
        winner=winner,
        scores={key: round(value, 4) for key, value in scores.items()},
        counts=counts,
        share=share,
        total_weight=round(total, 4),
    )


def is_low_confidence(confidence: float | None, threshold: float | None) -> bool:
    """阈值 <= 0 表示关闭；没有置信度时不做判断，避免误伤旧模型。"""
    if confidence is None or not threshold or threshold <= 0:
        return False
    try:
        return float(confidence) < float(threshold)
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class OfficialCheck:
    """官方答案与 AI 单元投票的裁决结果。

    ``decision``：
      - ``agree``：AI 多数答案与官方一致，采用官方；
      - ``override``：AI 至少 ``min_agree`` 票一致且加权份额 >= ``min_share``，采用 AI；
      - ``weak``：AI 不同意但未形成足够共识，保留官方并标记风险；
      - ``unavailable``：没有可用的 AI 复核，保留官方。
    """

    decision: str
    answer: str
    official_answer: str
    ai_answer: str | None = None
    ai_agree: int = 0
    ai_share: float = 0.0
    reason: str = ""


_JUDGE_TRUE = {"正确", "对", "是", "真", "true", "t", "√", "✓", "yes", "y"}
_JUDGE_FALSE = {"错误", "错", "否", "假", "false", "f", "×", "✗", "no", "n"}

# 只有这些题型才允许用 Math-Verify 做符号等价判定：
# 选择题的 A/B/C/D 在数学解析器里是符号，容易被误判成同一个变量。
_COMPLETION_KINDS = ("completion", "fill", "blank", "填空")


def is_completion(qtype: Any) -> bool:
    return str(qtype or "").strip().lower() in _COMPLETION_KINDS


def canonical_answer(qtype: str, answer: Any) -> str:
    """投票用的答案归一键：只合并**语义等价**的写法。

    它不改变返回给调用方的答案，只用于把「1/2 / 0.5 / 0.50」这类
    同一个答案的不同写法合并成一票。判断/单选/多选在 ``parse_answer``
    阶段已经归一，这里主要兜底填空题。
    """
    text = str(answer or "").strip()
    if not text:
        return ""
    kind = str(qtype or "").strip().lower()
    if kind in ("judge", "judgement", "tf", "truefalse"):
        key = re.sub(r"\s+", "", text).casefold()
        # 先判否定，避免「不正确」被「正确」误伤
        if key in _JUDGE_FALSE or "不正确" in key or "错误" in key:
            return "错误"
        if key in _JUDGE_TRUE or "正确" in key:
            return "正确"
        return key
    if kind == "multiple":
        letters = sorted(set(re.findall(r"[A-Za-z]", text.upper())))
        return "#".join(letters) if letters else re.sub(r"\s+", "", text).casefold()
    if kind == "single":
        letters = re.findall(r"[A-Za-z]", text.upper())
        return letters[0] if letters else re.sub(r"\s+", "", text).casefold()
    # completion / 其它：全角→半角、去空白与首尾标点、数值/分数精确归一
    norm = re.sub(r"[！-～]", lambda m: chr(ord(m.group()) - 0xFEE0), text)
    norm = norm.replace("$", "").replace("\\left", "").replace("\\right", "")
    norm = re.sub(r"\s+", "", norm).strip("。；;，,、")
    if not norm:
        return ""
    percent = norm.endswith("%")
    if percent:
        norm = norm[:-1]
    try:
        value = Fraction(norm)
        if percent:
            value = value / 100
        return "#num:" + str(value)
    except (ValueError, ZeroDivisionError, TypeError):
        return norm.casefold()


def _answer_key(value: Any) -> str:
    """答案比对只忽略空白和大小写，不做语义等价，避免把不同答案误判为一致。"""
    return "".join(str(value or "").split()).casefold()


def check_official(
    official_answer: Any,
    ai_answer: Any,
    ai_agree: int = 0,
    ai_share: float = 0.0,
    min_agree: int = 2,
    min_share: float = 0.5,
    qtype: str = "",
) -> OfficialCheck:
    """官方与 AI 各占一票的保守裁决。

    官方没有置信度，所以不能让单个 AI 凭一次作答覆盖官方；只有 AI 侧形成
    “至少两个模型一致 + 加权份额不低于一半”的共识时才覆盖。这样既不会
    无条件相信官方，也不会被单个模型的幻觉带偏。

    传入 ``qtype`` 时按题型做等价比较（``1/2`` 与 ``0.5``、``对`` 与 ``正确``
    视为同一个答案），等价时保留官方答案的原始写法，不触发覆盖。
    """
    official = str(official_answer or "").strip()
    ai = str(ai_answer or "").strip()
    if not official:
        return OfficialCheck("unavailable", official, official, None, 0, 0.0, "no-official")
    if not ai:
        return OfficialCheck("unavailable", official, official, None, 0, 0.0, "no-ai")

    agree = max(0, int(ai_agree or 0))
    share = float(ai_share or 0.0)
    if qtype:
        canon_ai = canonical_answer(qtype, ai)
        canon_official = canonical_answer(qtype, official)
        same = bool(canon_ai) and canon_ai == canon_official
        if not same and is_completion(qtype):
            # ``\frac{1}{2}`` / ``0.5`` / ``1/2`` 这类写法归一后字符串仍不同，
            # 但数学上是同一个答案 —— 交给 Math-Verify（未安装时返回 False）。
            same = math_equivalent(ai, official)
    else:
        same = _answer_key(ai) == _answer_key(official)
    if same:
        return OfficialCheck("agree", official, official, ai, agree, share, "same-answer")
    if agree >= max(1, int(min_agree or 1)) and share >= float(min_share or 0.0):
        return OfficialCheck("override", ai, official, ai, agree, share, "ai-consensus")
    return OfficialCheck("weak", official, official, ai, agree, share, "no-consensus")

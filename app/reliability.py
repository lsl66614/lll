"""答案可靠性分级与弃答判断。

可靠性只描述「这条答案有多少独立证据」，不评价答案本身对不对：

- ``verified``：官方/人工/导入来源，或独立模型共识，或已被两次确认采纳；
- ``unverified``：单模型自答、选项位置猜测、官方复核有争议、低置信。

弃答是可选策略（默认关闭）。开启后，``unverified`` 的答案不会返回给 OCS，
而是返回非 0 错误码，让脚本跳过这道题。这样做的目标是把「答满」换成
「少错」，避免把一个没有证据的猜测填进试卷。
"""

from __future__ import annotations

from typing import Any

ABSTAIN_CODE = 50003
VERIFIED = "verified"
UNVERIFIED = "unverified"

_TRUSTED_SOURCES = ("official", "manual", "import")
_CONFIRMED_SOURCES = ("ai", "calibrated", "bank")
_POSITIONAL_MATCHES = ("positional", "official-positional")


def _text(value: Any) -> str:
    return str(value or "").strip().lower()


def _flag(value: Any) -> bool:
    return bool(value)


def assess_answer(
    data: dict[str, Any],
    *,
    min_confidence: float = 0.0,
    include_positional: bool = True,
) -> tuple[str, str]:
    """返回 ``(reliability, reason)``；reason 为空表示没有发现具体风险。"""
    source = _text(data.get("source"))
    status = _text(data.get("status"))
    match = _text(data.get("match"))
    official_check = _text(data.get("official_check"))
    cross = _text(data.get("cross"))
    consensus = _text(data.get("consensus"))

    verified = False
    if source in _TRUSTED_SOURCES:
        # 官方答案被 AI 复核标成 weak/override 时，本次返回的仍是官方值，
        # 但证据已经不一致，不能再算 verified。
        verified = official_check not in ("weak", "override", "unavailable")
    elif source == "memory":
        # 记忆召回只允许 official/feedback/manual 等可信来源，已经在召回层筛过。
        verified = True
    elif consensus in ("unit", "cross", "official-agree"):
        verified = True
    elif cross in ("agree", "voted-agree", "confidence-voted"):
        verified = True
    elif status == "adopted" and source in _CONFIRMED_SOURCES:
        verified = True

    reason = ""
    if _flag(data.get("risk")):
        reason = "标记为有风险"
    elif _flag(data.get("low_confidence")):
        reason = "模型自报低置信"
    elif official_check == "weak":
        reason = "官方答案与 AI 复核不一致"
    elif include_positional and match in _POSITIONAL_MATCHES:
        reason = "按选项位置匹配，可能错位"
    elif not verified:
        reason = "未形成独立模型共识"

    if min_confidence > 0:
        confidence = data.get("confidence")
        if confidence is not None:
            try:
                if float(confidence) < float(min_confidence):
                    verified = False
                    reason = reason or "置信度低于弃答阈值"
            except (TypeError, ValueError):
                pass

    if reason:
        verified = False
    return (VERIFIED if verified else UNVERIFIED), reason


def should_abstain(reliability: str, opts: dict[str, Any]) -> bool:
    """弃答策略只作用于 unverified；默认关闭。"""
    return bool(opts.get("abstain_enabled")) and reliability == UNVERIFIED


def annotate(
    data: dict[str, Any],
    opts: dict[str, Any],
) -> tuple[str, str, bool]:
    """给响应数据补可靠性字段，并返回 ``(reliability, reason, reject)``。"""
    reliability, reason = assess_answer(
        data,
        min_confidence=float(opts.get("abstain_min_confidence") or 0),
        include_positional=bool(opts.get("abstain_include_positional", True)),
    )
    data["reliability"] = reliability
    if reason:
        data["reliability_reason"] = reason
    return reliability, reason, should_abstain(reliability, opts)

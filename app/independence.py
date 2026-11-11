"""投票用的模型独立性判断。

同一模型配多个 Key、或同一模型挂在不同 Base URL 下，都应该只算一票。
否则「两个模型一致」会退化成「同一个模型说了两次」，共识阈值失去意义。

这里只处理投票/交叉验证；顺序故障切换仍然保留重复条目，因为换 Key 重试
本身是有价值的。
"""

from __future__ import annotations

from typing import Any, Sequence


def provider_identity(provider: dict[str, Any]) -> str:
    """模型身份：优先用模型名，缺失时退回 Base URL，再退回 id/name。

    模型名是跨供应商最稳定的身份。同一模型的不同 Key、不同网关都归为同一组。
    """
    model = str(provider.get("model") or "").strip().lower()
    if model:
        return "model:" + model
    base = str(provider.get("base_url") or "").strip().lower().rstrip("/")
    if base:
        return "base:" + base
    return "id:" + str(provider.get("id") or provider.get("name") or "")


def independent_providers(providers: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """按模型身份去重，保留首次出现的顺序。"""
    seen: set[str] = set()
    picked: list[dict[str, Any]] = []
    for provider in providers:
        key = provider_identity(provider)
        if key in seen:
            continue
        seen.add(key)
        picked.append(provider)
    return picked

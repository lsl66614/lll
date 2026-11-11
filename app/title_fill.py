"""请求题干补全。

背景（2026-11-10 实测）：
- **我们自己的网关**早期把请求题干截到 300 字再发给模型；公式图片题的完整题干常超
  300 字（题库存的是 478 字）。
  （注：**不是官方 OCS 脚本截的**。官方 v5.4.0 和浏览器里实际装的 v5.3.1 都
  原样传 `env.title = params.title`，全文没有任何题干长度上限。）
- 截断掉的正是**尾部**——那里往往还有「则行列式…的值为」「则…可逆，且…」这类
  决定答案的关键信息。模型拿到半截题干，答案自然错。

补全办法：题库里往往存有同一道题的完整题干（官方答案回流时写进去的）。
用「文字骨架前缀 + 图片 URL 序列前缀」确认是同一道题后，把完整题干取出来，
让模型看到完整信息。

注意：只补题干，不补答案 —— 答案仍由题库/模型各自负责。
"""
from __future__ import annotations

from typing import Any

from .shape_key import image_urls, text_key, truncated_match


def is_truncated_request(title: str, limit: int) -> bool:
    """请求题干是否疑似被截断（顶到上限）。"""
    return bool(title) and len(title) >= limit


def find_full_title(
    items: list[dict[str, Any]], qtype: str, title: str, subject: str | None = None
) -> tuple[str, dict[str, Any]] | None:
    """在题库里找同一道题的完整题干。

    判据（都要满足）：
    1. 题型一致；
    2. 请求的文字骨架是库记录文字骨架的前缀（尾部被截断）；
    3. 请求里完整的图片 URL 序列是库记录图片序列的前缀（同一批图）。

    返回 (完整题干, 命中的记录)。
    """
    if not title:
        return None
    req_skeleton = text_key(title)
    if not req_skeleton:
        return None
    req_urls = image_urls(title)
    wanted = (subject or "").strip()

    best: tuple[str, dict[str, Any]] | None = None
    for item in items:
        if item.get("type") != qtype:
            continue
        if wanted and item.get("subject") not in ("", wanted, "默认"):
            continue
        stored = item.get("title") or ""
        if len(stored) <= len(title):
            continue  # 库里的不比请求长，补不了
        if not truncated_match(req_skeleton, text_key(stored)):
            continue
        stored_urls = image_urls(stored)
        if len(req_urls) > len(stored_urls):
            continue
        if stored_urls[: len(req_urls)] != req_urls:
            continue
        # 取最长的那条（信息最全）
        if best is None or len(stored) > len(best[0]):
            best = (stored, item)
    return best

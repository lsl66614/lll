"""视觉能力升级：把题干里的图片 URL 真正送进模型。

现状（2026-11-09 实测）：
- 题干内嵌图片 URL 时，网关只把 RapidOCR 的文字转写喂给模型；
  公式图是 alpha 掩膜，RapidOCR 只能认出边上的字母（626/1004 条 ≤20 字），
  模型收到「图片 4 = A」→ 答不出或瞎猜。
- A/B 实测 6 道官方答案已入库的图题：只给 OCR 文字命中 0/6；
  改成「图 + OCR 文字」命中 5/6。

本模块负责把题干/选项里的图片 URL 抓下来、flatten 到白底、作为
image_url 随题目一起发给模型。
"""
from __future__ import annotations

import asyncio
import base64
import re
from typing import Any

import httpx

from .image_fix import flatten_bytes

# 只抓常见公式图后缀；带 query 的 imageView 参数也要覆盖
IMAGE_URL_RE = re.compile(
    r"https?://[^\s\"'<>]+?\.(?:png|jpe?g|gif|bmp|webp)(?:\?[^\s\"'<>]*)?", re.IGNORECASE
)

MAX_INLINE_IMAGES = 6
MAX_INLINE_BYTES = 3 * 1024 * 1024
FETCH_TIMEOUT = 12.0


def extract_urls(texts: list[str] | str) -> list[str]:
    """从题干/选项里提取图片 URL，去重保序。

    传单个字符串时也能用（内部按 list 处理）—— 早先只接受 list，
    传字符串会逐字符迭代导致提取不到任何 URL。
    """
    if isinstance(texts, str):
        texts = [texts]
    seen: dict[str, None] = {}
    for text in texts:
        for url in IMAGE_URL_RE.findall(str(text or "")):
            seen.setdefault(url, None)
    return list(seen)


async def _fetch(url: str) -> bytes | None:
    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            resp = await client.get(url)
            if resp.status_code >= 400:
                return None
            if len(resp.content) > MAX_INLINE_BYTES:
                return None
            return resp.content
    except Exception:  # noqa: BLE001 - 单张图失败不影响整题
        return None


async def fetch_inline_images(urls: list[str]) -> list[str]:
    """下载并 flatten 图片，返回可直接放进 image_url 的 data URL 列表。"""
    if not urls:
        return []
    urls = urls[:MAX_INLINE_IMAGES]
    raws = await asyncio.gather(*[_fetch(url) for url in urls])
    out: list[str] = []
    for raw in raws:
        if not raw:
            continue
        fixed = flatten_bytes(raw) or raw
        out.append("data:image/png;base64," + base64.b64encode(fixed).decode())
    return out


def build_image_content(text: str, data_urls: list[str]) -> list[dict[str, Any]]:
    """把文本 + 图片组装成多模态 content 数组。"""
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for url in data_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})
    return content

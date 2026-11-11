"""把「选项图」与「选项字母」对应起来送进模型。

背景（2026-11-10 实测）：题库 915 条里有 77 条的**选项本身就是图片**
（64 条题干+选项都有图，13 条只有选项是图）。模型看到一堆图，却不知道
第几张图对应 A、第几张对应 B —— 照样答不出。

做法：在选项文字里用 ``[选项A的图1]`` 这样的占位标出图片归属，再把图片
按顺序附在消息里。题干里的图排在选项图之前，编号与题干里的 ``[图片N]``
保持一致，避免两套编号打架。
"""
from __future__ import annotations

import re
from typing import Any

from .inline_images import IMAGE_URL_RE

_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _urls_of(text: Any) -> list[str]:
    return IMAGE_URL_RE.findall(str(text or ""))


def annotate_options(options: list[str]) -> tuple[list[str], list[str]]:
    """给带图的选项加图片占位标记。

    返回 (处理后的选项文本列表, 按顺序排列的图片 URL 列表)。
    图片编号从 1 开始，与实际发送顺序一致。
    """
    urls: list[str] = []
    annotated: list[str] = []
    for index, option in enumerate(options):
        letter = _LETTERS[index] if index < len(_LETTERS) else str(index + 1)
        option_urls = _urls_of(option)
        if not option_urls:
            annotated.append(str(option))
            continue
        marks = []
        for url in option_urls:
            urls.append(url)
            marks.append(f"[选项{letter}的图{len(urls)}]")
        text = str(option)
        # 用占位标记替换 URL，模型才知道每张图属于哪个选项
        for url in option_urls:
            text = text.replace(url, marks[option_urls.index(url)], 1)
        annotated.append(text)
    return annotated, urls


def option_image_urls(options: list[str]) -> list[str]:
    """只取选项里的图片 URL（按选项顺序）。"""
    urls: list[str] = []
    for option in options:
        urls.extend(_urls_of(option))
    return urls


def strip_option_urls(options: list[str]) -> list[str]:
    """把选项里的图片 URL 换成一个中性占位（题干里已有编号说明时用）。"""
    out = []
    for option in options:
        text = str(option)
        for url in _urls_of(option):
            text = text.replace(url, "[图片]", 1)
        out.append(text)
    return out


_OPTION_URL_RE = re.compile(r"\[选项([A-Z]|\d+)的图(\d+)\]")

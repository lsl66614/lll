"""图片预处理：透明公式图合成白底并适度放大，供视觉模型识别。"""

from __future__ import annotations

import base64
import io


def _split_data_url(data_url: str) -> tuple[str, bytes]:
    try:
        if data_url.startswith("data:"):
            head, _, payload = data_url.partition(",")
            return head, base64.b64decode(payload)
        return "data:image/png;base64", base64.b64decode(data_url)
    except Exception:  # noqa: BLE001 - 非法 base64 按原样保留
        return "data:image/png;base64", b""


def flatten_on_white(data_url: str) -> str:
    """把带 alpha 通道的图片压到白底上（MOOC 公式图是纯 alpha 掩膜，直接读会全黑）。

    高度不足 96px 的碎片放大到约 96px，避免视觉模型拒绝过小的图。失败时原样返回。
    """
    _head, raw = _split_data_url(data_url)
    fixed = flatten_bytes(raw)
    if fixed is None:
        return data_url
    return "data:image/png;base64," + base64.b64encode(fixed).decode()


def flatten_bytes(raw: bytes) -> bytes | None:
    """把图片字节压白底并放大，返回 PNG 字节；失败返回 None。"""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        image = Image.open(io.BytesIO(raw))
        if image.mode in ("RGBA", "LA") or "transparency" in image.info:
            image = image.convert("RGBA")
            background = Image.new("RGBA", image.size, (255, 255, 255, 255))
            image = Image.alpha_composite(background, image).convert("RGB")
        else:
            image = image.convert("RGB")
        width, height = image.size
        if height < 96:
            scale = min(8, max(2, (96 + height - 1) // height))
            image = image.resize((width * scale, height * scale), Image.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()
    except Exception:  # noqa: BLE001
        return None

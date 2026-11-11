"""可选本地 OCR：优先 Pix2Text（公式→LaTeX），退回 RapidOCR（纯文本）。

两个引擎都是懒加载；未安装时 engine 返回 None，调用方应回退到视觉模型。
"""

from __future__ import annotations

import base64
import threading
from typing import Any


def _decode_data_url(value: str) -> bytes | None:
    text = str(value or "").strip()
    if not text.startswith("data:"):
        return None
    _, _, payload = text.partition(",")
    try:
        return base64.b64decode(payload)
    except Exception:  # noqa: BLE001
        return None


class FormulaOCR:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pix2text = None
        self._rapid = None
        self._tried = {"pix2text": False, "rapidocr": False}
        self._run_lock = threading.Lock()

    def _load_pix2text(self):
        with self._lock:
            if self._tried["pix2text"]:
                return self._pix2text
            self._tried["pix2text"] = True
            try:
                from pix2text import Pix2Text

                try:
                    import torch

                    torch.set_num_threads(1)
                except Exception:  # noqa: BLE001 - thread limit is best-effort
                    pass
                self._pix2text = Pix2Text.from_config()
            except Exception:  # noqa: BLE001
                self._pix2text = None
            return self._pix2text

    def _load_rapidocr(self):
        with self._lock:
            if self._tried["rapidocr"]:
                return self._rapid
            self._tried["rapidocr"] = True
            try:
                from rapidocr_onnxruntime import RapidOCR

                self._rapid = RapidOCR()
            except Exception:  # noqa: BLE001
                self._rapid = None
            return self._rapid

    def engine_name(self) -> str | None:
        if self._load_pix2text() is not None:
            return "pix2text"
        if self._load_rapidocr() is not None:
            return "rapidocr"
        return None

    def _extract_one(self, raw: bytes) -> tuple[str, str]:
        pix2text = self._load_pix2text()
        if pix2text is not None:
            try:
                from PIL import Image
                import io as _io

                image = Image.open(_io.BytesIO(raw)).convert("RGB")
                result = pix2text.recognize(image, return_text=True)
                if isinstance(result, str):
                    return result.strip(), "pix2text"
                if isinstance(result, dict):
                    text = result.get("text") or result.get("markdown") or ""
                    if not text and result.get("latex"):
                        text = f"$${result['latex']}$$"
                    return str(text).strip(), "pix2text"
            except Exception:  # noqa: BLE001
                pass

        rapid = self._load_rapidocr()
        if rapid is not None:
            try:
                import numpy as np
                from PIL import Image
                import io as _io

                image = Image.open(_io.BytesIO(raw)).convert("RGB")
                lines, _ = rapid(np.array(image))
                if lines:
                    return "\n".join(str(line[1]) for line in lines if len(line) > 1).strip(), "rapidocr"
            except Exception:  # noqa: BLE001
                pass
        return "", ""

    def extract(self, data_urls: list[str]) -> tuple[str, str]:
        parts: list[str] = []
        engine = ""
        with self._run_lock:
            for index, url in enumerate(data_urls or [], start=1):
                raw = _decode_data_url(url)
                if not raw:
                    continue
                text, used = self._extract_one(raw)
                if text:
                    engine = engine or used
                    parts.append(f"[图片{index}]\n{text}")
        return "\n\n".join(parts).strip(), engine


ocr = FormulaOCR()

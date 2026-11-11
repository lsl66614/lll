"""题目图片的原始字节仓库：按 sha256 落盘，URL → 资产索引。

为什么需要它：OCR 文本只解决了「读出来一次」，但原图 URL 可能过期、
CDN 换域名、或者某次 OCR 时视觉模型刚好 429。把原始图片留在本地后，
同一张图可以离线重跑 OCR、换模型复核，也能在 URL 失效后继续用。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any
from urllib.parse import urlparse

MAX_ASSET_BYTES = 12 * 1024 * 1024

_EXT_BY_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
}
_ALLOWED_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tif", ".tiff", ".bin",
}


class ImageAssetStore:
    """URL → sha256 → 原始图片字节。"""

    def __init__(self, root: str) -> None:
        self.root = root
        self.index_path = os.path.join(root, "index.json")
        self._lock = threading.RLock()
        self._items: dict[str, dict[str, Any]] = {}
        os.makedirs(root, exist_ok=True)
        self._load()

    def _load(self) -> None:
        try:
            with open(self.index_path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if isinstance(data, dict):
                self._items = {
                    str(key): value for key, value in data.items() if isinstance(value, dict)
                }
        except (OSError, json.JSONDecodeError, ValueError):
            self._items = {}

    def _save(self) -> None:
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(self._items, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, self.index_path)

    def _asset_name(self, url: str, content_type: str, digest: str) -> str:
        ctype = str(content_type or "").split(";")[0].strip().lower()
        ext = _EXT_BY_TYPE.get(ctype) or os.path.splitext(urlparse(url).path)[1].lower()
        if ext == ".jpeg":
            ext = ".jpg"
        if ext not in _ALLOWED_EXT:
            ext = ".bin"
        return digest + ext

    def put_bytes(self, url: str, raw: bytes, content_type: str = "") -> str:
        """保存原始字节；同一内容只写一次。返回 sha256，失败返回空串。"""
        if not url or not raw or len(raw) > MAX_ASSET_BYTES:
            return ""
        digest = hashlib.sha256(raw).hexdigest()
        name = self._asset_name(url, content_type, digest)
        path = os.path.join(self.root, name)
        ctype = str(content_type or "").split(";")[0].strip().lower()
        with self._lock:
            if not os.path.exists(path):
                tmp = path + ".tmp"
                with open(tmp, "wb") as fp:
                    fp.write(raw)
                os.replace(tmp, path)
            self._items[url] = {
                "sha256": digest,
                "path": name,
                "content_type": ctype,
                "size": len(raw),
                "at": time.time(),
            }
            self._save()
        return digest

    def get(self, url: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._items.get(str(url or "")) or {})

    def get_path(self, url: str) -> str | None:
        item = self.get(url)
        name = str(item.get("path") or "")
        if not name:
            return None
        path = os.path.join(self.root, name)
        return path if os.path.exists(path) else None

    def get_bytes(self, url: str) -> bytes | None:
        path = self.get_path(url)
        if not path:
            return None
        try:
            with open(path, "rb") as fp:
                return fp.read(MAX_ASSET_BYTES + 1)
        except OSError:
            return None

    def content_type(self, url: str) -> str:
        return str(self.get(url).get("content_type") or "")

    def drop(self, url: str) -> None:
        """丢掉某个 URL 的索引（文件按内容哈希保留，供其它 URL 复用）。"""
        with self._lock:
            if self._items.pop(str(url or ""), None) is not None:
                self._save()

    def stats(self) -> dict[str, int]:
        with self._lock:
            items = list(self._items.values())
        present = sum(
            1 for item in items
            if item.get("path") and os.path.exists(os.path.join(self.root, str(item["path"])))
        )
        return {
            "urls": len(items),
            "present": present,
            "bytes": sum(int(item.get("size") or 0) for item in items),
        }

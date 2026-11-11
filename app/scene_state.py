"""当前答题场景（随堂测验 / 单元测验）的短时状态。

由油猴脚本在测验页上报，/query 读取后决定严格程度；无法判断时按 settings 处理。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

ALLOWED = {"quick", "unit"}
DEFAULT_TTL = 20 * 60


class SceneState:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._scene = ""
        self._at = 0.0
        self._url = ""
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            scene = str(data.get("scene") or "")
            if scene in ALLOWED:
                self._scene = scene
                self._at = float(data.get("at") or 0)
                self._url = str(data.get("url") or "")
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump({"scene": self._scene, "at": self._at, "url": self._url}, fp, ensure_ascii=False)
        os.replace(tmp, self.path)

    def set(self, scene: str, url: str = "") -> dict[str, Any]:
        scene = str(scene or "").strip().lower()
        if scene not in ALLOWED:
            scene = ""
        with self._lock:
            self._scene = scene
            self._at = time.time()
            self._url = str(url or "")[:300]
            self._save()
            return {"scene": scene, "at": self._at, "url": self._url}

    def get(self, ttl: int = DEFAULT_TTL) -> str:
        with self._lock:
            if not self._scene or time.time() - self._at > ttl:
                return ""
            return self._scene

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"scene": self._scene, "at": self._at, "url": self._url}

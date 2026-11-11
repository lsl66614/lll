"""AI 服务商配置的本地存储。

用一个 JSON 文件保存，写入时先写临时文件再原子替换，避免断电写坏。
服务是单进程运行的，用一把可重入锁保证并发安全。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any

DEFAULT_TIMEOUT = 30


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def default_provider(
    name: str = "默认 AI",
    base_url: str = "https://api.deepseek.com",
    api_key: str = "",
    model: str = "deepseek-chat",
) -> dict[str, Any]:
    return {
        "id": _new_id(),
        "name": name,
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "api_type": "chat_completions",
        "temperature": None,
        "timeout": DEFAULT_TIMEOUT,
        "vision": "auto",
        "system_prompt": "",
        "max_tokens": None,
        "enabled": True,
        "created_at": time.time(),
    }


class ProviderStore:
    def __init__(self, path: str, seed: dict[str, Any] | None = None) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._providers: list[dict[str, Any]] = []
        self._load(seed)

    # ---------------- 持久化 ----------------

    def _load(self, seed: dict[str, Any] | None) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                providers = data.get("providers", [])
                if isinstance(providers, list):
                    self._providers = [self._normalize(p) for p in providers if isinstance(p, dict)]
                    return
            except (OSError, json.JSONDecodeError, ValueError):
                # 文件损坏时不要直接崩掉服务，备份后重建
                try:
                    os.replace(self.path, self.path + ".broken")
                except OSError:
                    pass

        if seed and seed.get("api_key"):
            self._providers = [self._normalize(default_provider(**seed))]
            self._save()
        else:
            self._providers = []
            self._save()

    def _save(self) -> None:
        payload = {"version": 1, "providers": self._providers}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    @staticmethod
    def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
        provider = {
            "id": str(raw.get("id") or _new_id()),
            "name": str(raw.get("name") or "未命名 AI")[:60],
            "base_url": str(raw.get("base_url") or "").strip(),
            "api_key": str(raw.get("api_key") or "").strip(),
            "model": str(raw.get("model") or "").strip(),
            "api_type": "responses" if str(raw.get("api_type")) == "responses" else "chat_completions",
            "temperature": raw.get("temperature"),
            "timeout": int(raw.get("timeout") or DEFAULT_TIMEOUT),
            "vision": "yes" if str(raw.get("vision")) == "yes" else ("no" if str(raw.get("vision")) == "no" else "auto"),
            "system_prompt": str(raw.get("system_prompt") or "").strip()[:2000],
            "max_tokens": None,
            "enabled": bool(raw.get("enabled", True)),
            "created_at": float(raw.get("created_at") or time.time()),
        }
        if provider["temperature"] in ("", None):
            provider["temperature"] = None
        else:
            try:
                provider["temperature"] = max(0.0, min(2.0, float(provider["temperature"])))
            except (TypeError, ValueError):
                provider["temperature"] = None
        provider["timeout"] = max(5, min(180, provider["timeout"]))
        raw_max = raw.get("max_tokens")
        if raw_max in ("", None):
            provider["max_tokens"] = None
        else:
            try:
                provider["max_tokens"] = max(1, min(8192, int(raw_max)))
            except (TypeError, ValueError):
                provider["max_tokens"] = None
        return provider

    # ---------------- 查询 ----------------

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(p) for p in self._providers]

    def enabled(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(p) for p in self._providers if p["enabled"]]

    def get(self, pid: str) -> dict[str, Any] | None:
        with self._lock:
            for provider in self._providers:
                if provider["id"] == pid:
                    return dict(provider)
        return None

    # ---------------- 修改 ----------------

    def add(self, data: dict[str, Any]) -> dict[str, Any]:
        provider = self._normalize({**data, "id": _new_id(), "created_at": time.time()})
        with self._lock:
            self._providers.append(provider)
            self._save()
        return dict(provider)

    def update(self, pid: str, data: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            for index, provider in enumerate(self._providers):
                if provider["id"] != pid:
                    continue
                merged = {**provider, **data, "id": pid}
                # api_key 留空表示"保持原值"，避免前端掩码回写把 Key 清掉
                if not str(data.get("api_key") or "").strip():
                    merged["api_key"] = provider["api_key"]
                self._providers[index] = self._normalize(merged)
                self._save()
                return dict(self._providers[index])
        return None

    def delete(self, pid: str) -> bool:
        with self._lock:
            before = len(self._providers)
            self._providers = [p for p in self._providers if p["id"] != pid]
            if len(self._providers) == before:
                return False
            self._save()
            return True

    def move(self, pid: str, direction: int) -> bool:
        with self._lock:
            ids = [p["id"] for p in self._providers]
            if pid not in ids:
                return False
            index = ids.index(pid)
            target = index + (1 if direction > 0 else -1)
            if target < 0 or target >= len(self._providers):
                return False
            self._providers[index], self._providers[target] = (
                self._providers[target],
                self._providers[index],
            )
            self._save()
            return True

    def toggle(self, pid: str, enabled: bool | None = None) -> dict[str, Any] | None:
        with self._lock:
            for provider in self._providers:
                if provider["id"] == pid:
                    provider["enabled"] = (not provider["enabled"]) if enabled is None else bool(enabled)
                    self._save()
                    return dict(provider)
        return None

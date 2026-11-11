"""通知推送：Server酱 / Bark / 通用 webhook，用于定时任务报告。"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

import httpx

from .ops import JsonStore

DEFAULT = {
    "enabled": False,
    "provider": "serverchan",  # serverchan | bark | webhook
    "key": "",
    "url": "",
    "events": {"daily": True},
}


class NotifyConfig:
    def __init__(self, path: str) -> None:
        self.store = JsonStore(path, dict(DEFAULT))

    def get(self) -> dict[str, Any]:
        data = self.store.read()
        merged = dict(DEFAULT)
        if isinstance(data, dict):
            merged.update({k: v for k, v in data.items() if k in DEFAULT})
            if not isinstance(merged.get("events"), dict):
                merged["events"] = dict(DEFAULT["events"])
        return merged

    def update(self, patch: Any) -> dict[str, Any]:
        current = self.get()
        if isinstance(patch, dict):
            for key in DEFAULT:
                if key in patch:
                    current[key] = patch[key]
        self.store.write(current)
        return current

    def masked(self) -> dict[str, Any]:
        data = self.get()
        key = str(data.get("key") or "")
        if key:
            data["key"] = key[:4] + "****" + key[-2:] if len(key) > 8 else "****"
        return data


def _build_request(config: dict[str, Any], title: str, body: str) -> tuple[str, str, dict[str, Any]] | None:
    provider = str(config.get("provider") or "serverchan")
    key = str(config.get("key") or "").strip()
    url = str(config.get("url") or "").strip()
    if provider == "serverchan":
        if not key:
            return None
        return ("POST", "https://sctapi.ftqq.com/" + key + ".send", {"title": title, "desp": body})
    if provider == "bark":
        target = url or "https://api.day.app/push"
        payload = {"title": title, "body": body}
        if key:
            payload["device_key"] = key
        return ("POST", target, payload)
    if provider == "webhook":
        if not url:
            return None
        return ("POST", url, {"title": title, "text": body, "at": time.time()})
    return None


def send(config: dict[str, Any], title: str, body: str, timeout: float = 10.0) -> dict[str, Any]:
    """同步发送一条通知；返回 {ok, status, error}。"""
    if not config.get("enabled"):
        return {"ok": False, "skipped": True, "error": "通知未启用"}
    request = _build_request(config, title, body)
    if request is None:
        return {"ok": False, "error": "通知配置不完整（provider/key/url）"}
    method, url, payload = request
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(method, url, json=payload)
        ok = 200 <= response.status_code < 300
        return {"ok": ok, "status": response.status_code, "error": "" if ok else response.text[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


def format_daily(detail: dict[str, Any]) -> tuple[str, str]:
    """把每日任务结果整理成标题 + 正文。"""
    title = "OCS 网关日报 " + time.strftime("%m-%d %H:%M")
    lines = []
    if detail.get("backup"):
        lines.append("备份：" + str(detail["backup"]) + f"（{detail.get('backup_bytes', 0)} 字节）")
    near = detail.get("near_dedupe") or {}
    if near:
        lines.append(f"近似重复：{near.get('groups', 0)} 组，可删 {near.get('removed', 0)} 条")
    formula = detail.get("formula") or {}
    if formula:
        lines.append(f"公式索引：{formula.get('urls', 0)} 个图，缺 {formula.get('missing', 0)}")
    if detail.get("conflicts") is not None:
        lines.append(f"答案冲突：{detail['conflicts']} 条")
    vote = detail.get("vote") or {}
    if vote:
        lines.append(f"投票预演：{vote.get('total', 0)} 题")
    offline = detail.get("probes_offline") or []
    if offline:
        lines.append("离线探针：" + "、".join(offline))
    return title, "\n".join(lines) or "本次任务无数据"

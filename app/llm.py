"""调用 OpenAI 兼容接口。

只用 httpx，不依赖官方 SDK，这样 base_url / 超时 / 温度都能按条配置。
"""

from __future__ import annotations

import contextvars
import json
import os
from typing import Any

import httpx

# 最近一次调用的 usage。_call_one 仍走 call_provider（便于测试 mock），
# 通过这个 contextvar 旁路拿到 token；并发任务各自持有独立上下文。
_last_usage: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar(
    "llm_last_usage", default=None
)

_last_reasoning: contextvars.ContextVar[str] = contextvars.ContextVar(
    "llm_last_reasoning", default=""
)


def reset_usage() -> None:
    _last_usage.set(None)
    _last_reasoning.set("")


def last_usage() -> dict[str, int] | None:
    return _last_usage.get()


def last_reasoning() -> str:
    """最近一次调用里模型返回的思考过程；接口没给就是空串。"""
    return _last_reasoning.get()


def _sanitize_no_proxy() -> None:
    """httpx 会把 NO_PROXY 的每一项当 URL 解析，`[::1]` 这类带方括号的项会抛 InvalidURL。"""
    for name in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(name)
        if not raw:
            continue
        kept = [p.strip() for p in raw.split(",") if p.strip() and "[" not in p and "]" not in p]
        os.environ[name] = ",".join(kept)


_sanitize_no_proxy()


class ProviderError(Exception):
    """单个 AI 调用失败（会触发切换到下一个 AI）。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _short(text: str, limit: int = 200) -> str:
    text = " ".join(str(text).split())
    return text[:limit] + ("..." if len(text) > limit else "")


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ERROR_BODY_BYTES = 64 * 1024


def _extract_error_text(status: int, text: str) -> str:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return f"HTTP {status}: {_short(text)}"

    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code") or error
            return f"HTTP {status}: {_short(message)}"
        if isinstance(error, str):
            return f"HTTP {status}: {_short(error)}"
        if data.get("message"):
            return f"HTTP {status}: {_short(data['message'])}"
    return f"HTTP {status}: {_short(text)}"


async def _read_limited(response: httpx.Response, limit: int, truncate: bool = False) -> bytes:
    """按块读取响应体，超过 limit 时抛错；错误响应可只截取前 limit 字节。"""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            if truncate:
                remaining = limit - (total - len(chunk))
                if remaining > 0:
                    chunks.append(chunk[:remaining])
                break
            raise ProviderError(f"模型响应过大（>{limit // (1024 * 1024)} MiB）")
        chunks.append(chunk)
    return b"".join(chunks)


def _endpoint(base_url: str, api_type: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ProviderError("Base URL 为空，请在管理台填写")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    return base + ("/responses" if api_type == "responses" else "/chat/completions")


def _payload(provider: dict[str, Any], messages: list[dict], api_type: str) -> dict[str, Any]:
    model = (provider.get("model") or "").strip()
    if not model:
        raise ProviderError("模型名称为空，请在管理台填写")

    payload: dict[str, Any] = {"model": model}
    if api_type == "responses":
        parts: list[dict[str, Any]] = []
        for item in messages:
            content = item["content"]
            if isinstance(content, str):
                parts.append({"type": "input_text", "text": content})
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    parts.append({"type": "input_text", "text": part.get("text", "")})
                elif part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url", "")
                    if url:
                        parts.append({"type": "input_image", "image_url": url})
        payload["input"] = [{"role": "user", "content": parts}]
    else:
        payload["messages"] = messages
        payload["stream"] = False

    if provider.get("temperature") is not None:
        payload["temperature"] = provider["temperature"]
    if provider.get("max_tokens"):
        key = "max_output_tokens" if api_type == "responses" else "max_tokens"
        payload[key] = int(provider["max_tokens"])
    return payload


def _extract_usage(data: dict[str, Any], api_type: str) -> dict[str, int] | None:
    """把两种 API 的 usage 统一成 prompt/completion/total 三个整数。"""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    if api_type == "responses":
        prompt = usage.get("input_tokens")
        completion = usage.get("output_tokens")
    else:
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    out: dict[str, int] = {}
    for key, value in (
        ("prompt_tokens", prompt),
        ("completion_tokens", completion),
        ("total_tokens", total),
    ):
        if isinstance(value, (int, float)) and value >= 0:
            out[key] = int(value)
    if "total_tokens" not in out and out:
        out["total_tokens"] = out.get("prompt_tokens", 0) + out.get("completion_tokens", 0)
    return out or None


def _extract_text(data: dict[str, Any], api_type: str) -> str:
    if api_type == "responses":
        chunks: list[str] = []
        for item in data.get("output", []) or []:
            for content in item.get("content", []) or []:
                if content.get("type") in ("output_text", "text") and content.get("text"):
                    chunks.append(content["text"])
        return "".join(chunks).strip()

    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        # 兼容 content 为分段数组的实现
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        ).strip()
    return (content or "").strip()


MAX_REASONING = 4000


def _join_reasoning(parts: Any) -> str:
    """把 reasoning 的几种常见形状拼成纯文本，兼容 summary/content 数组。"""
    chunks: list[str] = []
    if isinstance(parts, str):
        return parts.strip()
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("summary_text") or ""
                if isinstance(text, str) and text.strip():
                    chunks.append(text.strip())
    return "\n".join(chunks).strip()


def _extract_reasoning(data: dict[str, Any], api_type: str) -> str:
    """不同上游把思考过程放在不同字段，这里做一层归一：

    - chat/completions：message.reasoning_content（DeepSeek/硅基流动）、
      message.reasoning（OpenRouter）、message.thinking
    - responses：output[] 里 type=reasoning 的 summary / content
    """
    text = ""
    if api_type == "responses":
        parts = []
        for item in data.get("output", []) or []:
            if not isinstance(item, dict) or item.get("type") != "reasoning":
                continue
            chunk = _join_reasoning(item.get("summary")) or _join_reasoning(item.get("content"))
            if chunk:
                parts.append(chunk)
        text = "\n\n".join(parts)
    else:
        choices = data.get("choices") or []
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            if isinstance(message, dict):
                for key in ("reasoning_content", "reasoning", "thinking"):
                    text = _join_reasoning(message.get(key))
                    if text:
                        break
                if not text:
                    text = _join_reasoning(message.get("content")) if isinstance(
                        message.get("content"), list
                    ) else ""
    return text[:MAX_REASONING]


async def call_provider_detail(
    provider: dict[str, Any], messages: list[dict]
) -> tuple[str, dict[str, int] | None]:
    """返回 (文本, usage)。usage 供 trace 展示 token，不影响答题逻辑。"""
    api_type = provider.get("api_type") or "chat_completions"
    url = _endpoint(provider.get("base_url", ""), api_type)
    system_prompt = (provider.get("system_prompt") or "").strip()
    if system_prompt:
        messages = [dict(message) for message in messages]
        for index, message in enumerate(messages):
            if message.get("role") == "system":
                messages[index] = {**message, "content": system_prompt}
                break
    payload = _payload(provider, messages, api_type)

    headers = {"Content-Type": "application/json"}
    api_key = (provider.get("api_key") or "").strip()
    if api_key:
        try:
            api_key.encode("ascii")
        except UnicodeEncodeError:
            raise ProviderError("API Key 含有中文或全角字符，请重新复制粘贴") from None
        headers["Authorization"] = f"Bearer {api_key}"

    timeout = httpx.Timeout(float(provider.get("timeout") or 30))

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code >= 400:
                    body = await _read_limited(response, ERROR_BODY_BYTES, truncate=True)
                    raise ProviderError(
                        _extract_error_text(response.status_code, body.decode("utf-8", "replace")),
                        response.status_code,
                    )
                body = await _read_limited(response, MAX_RESPONSE_BYTES)
    except httpx.TimeoutException:
        raise ProviderError(f"请求超时（>{provider.get('timeout')}s）") from None
    except httpx.HTTPError as exc:
        raise ProviderError(f"网络错误: {_short(type(exc).__name__ + ': ' + str(exc))}") from None
    except ProviderError:
        raise
    except Exception as exc:  # noqa: BLE001 - 兜底，避免单个 AI 的问题把整个请求打挂
        raise ProviderError(f"请求失败: {_short(type(exc).__name__ + ': ' + str(exc))}") from None

    try:
        data = json.loads(body.decode("utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise ProviderError("返回内容不是 JSON，可能 Base URL 填错了") from None

    text = _extract_text(data, api_type)
    if not text:
        raise ProviderError("模型返回空内容")
    usage = _extract_usage(data, api_type)
    reasoning = _extract_reasoning(data, api_type)
    _last_usage.set(usage)
    _last_reasoning.set(reasoning)
    return text, usage


async def call_provider(provider: dict[str, Any], messages: list[dict]) -> str:
    """兼容旧调用点：只要文本时用这个。"""
    text, _usage = await call_provider_detail(provider, messages)
    return text

"""答题过程 trace：记录一次 /query 的决策链路，供 /trace 页面实时查看。

三条设计原则：

1. **零侵入**：所有公开方法都吞异常。trace 坏掉最多是页面空白，绝不能让答题失败。
2. **只留内存**：最近 N 条环形缓冲，重启即清空。答题是热路径，不为了可视化多写一次盘。
3. **contextvar 绑定**：请求入口 begin 后，``asyncio.create_task`` 派生的模型调用
   会自动继承当前 trace，因此 ``_call_one`` 不需要把 trace 一路当参数传下去。
"""

from __future__ import annotations

import contextvars
import json
import threading
import time
import uuid
from collections import deque
from typing import Any

MAX_TRACES = 50
MAX_MODELS = 12
MAX_STAGES = 40
MAX_TEXT = 500
MAX_TITLE_TEXT = 4000
MAX_REASON_TEXT = 4000
MAX_IMAGE_TEXT = 4000


def _clip(value: Any, limit: int = MAX_TEXT) -> str:
    return str(value or "").strip()[:limit]


def _jsonable(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, (list, tuple)):
        return all(_jsonable(item) for item in value[:20])
    if isinstance(value, dict):
        return all(isinstance(key, str) and _jsonable(item) for key, item in list(value.items())[:20])
    return False


class Trace:
    """一条答题轨迹。字段只增不改，前端按 seq 轮询。"""

    def __init__(
        self,
        seq: int,
        title: str = "",
        qtype: str = "",
        options: list[str] | None = None,
        subject: str = "",
        context: str = "",
        input_title_len: int = 0,
        input_options: int = 0,
        input_images: int = 0,
        model_title_len: int = 0,
        model_options: int = 0,
    ) -> None:
        self.id = f"t{seq}-{uuid.uuid4().hex[:6]}"
        self.seq = seq
        self.started = time.time()
        # perf_counter 基准：调用方可以用「开始时刻」补记一个已经耗时的阶段，
        # 让瀑布图把它画在正确的区间，而不是记录时刻之后。
        self._perf_started = time.perf_counter()
        self.title = _clip(title)
        self.title_full = _clip(title, MAX_TITLE_TEXT)
        self.qtype = _clip(qtype, 40)
        self.options = [_clip(item, 300) for item in (options or [])][:10]
        self.input_title_len = int(input_title_len or len(self.title_full))
        self.input_options = int(input_options or len(self.options))
        self.input_images = int(input_images or 0)
        self.model_title_len = int(model_title_len or len(self.title_full))
        self.model_options = int(model_options or len(self.options))
        self.subject = _clip(subject, 80)
        self.context = _clip(context, 200)
        self.scene = ""
        self.image_text = ""
        self.image_source = ""
        self.stages: list[dict[str, Any]] = []
        self.models: list[dict[str, Any]] = []
        self.vote: dict[str, Any] | None = None
        self.result: dict[str, Any] | None = None
        self.status = "running"
        self.error = ""
        self.finished: float | None = None
        self.elapsed_ms: int | None = None
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- 写入

    def _elapsed(self) -> int:
        end = self.finished or time.time()
        return int((end - self.started) * 1000)

    def update(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                if not hasattr(self, key) or key.startswith("_"):
                    continue
                if isinstance(value, str):
                    value = _clip(value, 200)
                setattr(self, key, value)

    def set_image_text(self, text: str, source: str = "") -> None:
        with self._lock:
            self.image_text = _clip(text, MAX_IMAGE_TEXT)
            self.image_source = _clip(source, 120)

    def stage(
        self, name: str, status: str = "done",
        started_at: float | None = None, **extra: Any,
    ) -> None:
        with self._lock:
            if len(self.stages) >= MAX_STAGES:
                return
            started_ms = self._elapsed()
            if started_at is not None:
                started_ms = max(0, int((started_at - self._perf_started) * 1000))
            item: dict[str, Any] = {
                "name": _clip(name, 40),
                "status": _clip(status, 20),
                "t": started_ms,
                "at": time.time(),
            }
            for key, value in extra.items():
                if _jsonable(value):
                    item[_clip(key, 40)] = value
            self.stages.append(item)

    def model_start(self, provider: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            if len(self.models) >= MAX_MODELS:
                return None
            raw_name = _clip(provider.get("name") or provider.get("id") or "unknown", 120)
            model = _clip(provider.get("model"), 120)
            # 管理台里只填了序号（1/2/3）时，显示真实模型名更有辨识度。
            display = model if (model and (not raw_name or raw_name.isdigit())) else raw_name
            entry = {
                "id": _clip(provider.get("id"), 80),
                "name": display,
                "provider_name": raw_name,
                "model": model,
                "status": "running",
                "answer": None,
                "confidence": None,
                "elapsed_ms": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "reasoning": "",
                "error": "",
                "t": self._elapsed(),
                "t_end": None,
            }
            self.models.append(entry)
            return entry

    def model_end(
        self,
        entry: dict[str, Any] | None,
        answer: str | None = None,
        confidence: float | None = None,
        elapsed_ms: int | None = None,
        error: str = "",
        usage: dict[str, int] | None = None,
        reasoning: str | None = None,
    ) -> None:
        if entry is None:
            return
        with self._lock:
            entry["status"] = "error" if error else "ok"
            entry["answer"] = None if answer is None else _clip(answer, 120)
            entry["confidence"] = confidence if isinstance(confidence, (int, float)) else None
            entry["elapsed_ms"] = int(elapsed_ms) if isinstance(elapsed_ms, (int, float)) else None
            entry["error"] = _clip(error, 300)
            if reasoning:
                entry["reasoning"] = _clip(reasoning, MAX_REASON_TEXT)
            entry["t_end"] = self._elapsed()
            if isinstance(usage, dict):
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    value = usage.get(key)
                    if isinstance(value, (int, float)) and value >= 0:
                        entry[key] = int(value)

    def set_vote(self, data: dict[str, Any] | None) -> None:
        if not isinstance(data, dict):
            return
        clean: dict[str, Any] = {}
        for key, value in data.items():
            if _jsonable(value):
                clean[_clip(key, 40)] = value
        if clean:
            with self._lock:
                self.vote = clean

    def finish(self, result: dict[str, Any] | None = None, error: str = "") -> None:
        with self._lock:
            if self.finished is not None:
                return
            self.finished = time.time()
            self.elapsed_ms = self._elapsed()
            # 交叉验证只取前两个成功模型，剩下的并发任务会被取消；
            # 被取消的协程走不到 model_end，这里统一收尾，避免页面永远显示「思考中」。
            for entry in self.models:
                if entry.get("status") != "running":
                    continue
                entry["status"] = "cancelled"
                entry["t_end"] = self.elapsed_ms
                entry["elapsed_ms"] = max(0, self.elapsed_ms - int(entry.get("t") or 0))
                entry["error"] = "未参与最终裁决（请求已取消）"
            if error:
                self.status = "error"
                self.error = _clip(error, 300)
            else:
                self.status = "ok"
                self.result = result
            self.stage("finish", "error" if error else "ok")

    # ---------------------------------------------------------------- 读取

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            elapsed = self.elapsed_ms if self.elapsed_ms is not None else self._elapsed()
            stages = [dict(item) for item in self.stages]
            models = [dict(item) for item in self.models]
            max_t = max(
                [elapsed]
                + [int(item.get("t") or 0) for item in stages]
                + [int(item.get("t_end") or item.get("t") or 0) for item in models]
            )
            # 每个阶段持续到下一个阶段开始；瀑布图直接用它画时间条。
            for index, item in enumerate(stages):
                end = stages[index + 1].get("t", max_t) if index + 1 < len(stages) else max_t
                item["span_ms"] = max(0, int(end) - int(item.get("t") or 0))
            return {
                "id": self.id,
                "seq": self.seq,
                "started": self.started,
                "finished": self.finished,
                "elapsed_ms": elapsed,
                "max_t": max_t,
                "status": self.status,
                "error": self.error,
                "title": self.title,
                "title_full": self.title_full,
                "input": {
                    "title_len": self.input_title_len,
                    "title_stored": len(self.title_full),
                    "title_model": self.model_title_len,
                    "title_clipped": self.input_title_len > len(self.title_full),
                    "options": self.input_options,
                    "options_stored": len(self.options),
                    "options_model": self.model_options,
                    "options_clipped": self.input_options > len(self.options),
                    "images": self.input_images,
                },
                "qtype": self.qtype,
                "options": list(self.options),
                "image_text": self.image_text,
                "image_source": self.image_source,
                "subject": self.subject,
                "context": self.context,
                "scene": self.scene,
                "stages": stages,
                "models": models,
                "total_tokens": sum(int(item.get("total_tokens") or 0) for item in models),
                "vote": dict(self.vote) if self.vote else None,
                "result": dict(self.result) if self.result else None,
            }


class TraceStore:
    def __init__(self, max_traces: int = MAX_TRACES) -> None:
        self._traces: deque[Trace] = deque(maxlen=max(1, int(max_traces)))
        self._lock = threading.RLock()
        self._seq = 0
        self._current: contextvars.ContextVar[Trace | None] = contextvars.ContextVar(
            "ocs_answer_trace", default=None
        )

    # ---------------------------------------------------------------- 生命周期

    def begin(
        self,
        title: str = "",
        qtype: str = "",
        options: list[str] | None = None,
        subject: str = "",
        context: str = "",
        input_title_len: int = 0,
        input_options: int = 0,
        input_images: int = 0,
        model_title_len: int = 0,
        model_options: int = 0,
    ) -> Trace:
        try:
            with self._lock:
                self._seq += 1
                trace = Trace(
                    self._seq, title, qtype, options, subject, context,
                    input_title_len=input_title_len,
                    input_options=input_options,
                    input_images=input_images,
                    model_title_len=model_title_len,
                    model_options=model_options,
                )
                self._traces.append(trace)
            self._current.set(trace)
            return trace
        except Exception:  # noqa: BLE001 - trace 永不阻断答题
            trace = Trace(0)
            self._current.set(trace)
            return trace

    def current(self) -> Trace | None:
        try:
            return self._current.get()
        except Exception:  # noqa: BLE001
            return None

    def set_image_text(self, text: str, source: str = "") -> None:
        trace = self.current()
        if trace is None:
            return
        try:
            trace.set_image_text(text, source)
        except Exception:  # noqa: BLE001
            pass

    def reset(self, trace: Trace | None = None) -> None:
        try:
            if trace is None or self._current.get() is trace:
                self._current.set(None)
        except Exception:  # noqa: BLE001
            pass

    def finish(self, trace: Trace | None, response: Any = None, error: str = "") -> None:
        if trace is None:
            return
        result = None
        if response is not None and not error:
            try:
                payload = json.loads(bytes(response.body).decode("utf-8"))
                code = payload.get("code") if isinstance(payload, dict) else None
                data = payload.get("data") if isinstance(payload, dict) else None
                if code == 0 and isinstance(data, dict):
                    result = {
                        "answer": _clip(data.get("answer"), 200),
                        "source": _clip(data.get("source"), 40),
                        "provider": _clip(data.get("provider"), 120),
                        "match": _clip(data.get("match"), 60),
                        "confidence": data.get("confidence"),
                        "consensus": _clip(data.get("consensus"), 40),
                        "risk": data.get("risk"),
                        "cached": bool(data.get("cached")),
                        "elapsed_ms": data.get("elapsed_ms"),
                    }
                else:
                    error = _clip(
                        (payload or {}).get("message") if isinstance(payload, dict) else "",
                        300,
                    ) or f"code={code}"
            except Exception as exc:  # noqa: BLE001 - 解析失败不影响返回
                error = f"trace 解析响应失败: {type(exc).__name__}"
        try:
            trace.finish(result, error)
        except Exception:  # noqa: BLE001
            pass
        self.reset(trace)

    # ---------------------------------------------------------------- 埋点快捷方式

    def stage(
        self, name: str, status: str = "done",
        started_at: float | None = None, **extra: Any,
    ) -> None:
        try:
            trace = self.current()
            if trace is not None:
                trace.stage(name, status, started_at=started_at, **extra)
        except Exception:  # noqa: BLE001
            pass

    def update(self, **fields: Any) -> None:
        try:
            trace = self.current()
            if trace is not None:
                trace.update(**fields)
        except Exception:  # noqa: BLE001
            pass

    def model_start(self, provider: dict[str, Any]) -> dict[str, Any] | None:
        try:
            trace = self.current()
            if trace is not None:
                return trace.model_start(provider)
        except Exception:  # noqa: BLE001
            pass
        return None

    def model_end(self, entry: dict[str, Any] | None, **kwargs: Any) -> None:
        try:
            trace = self.current()
            if trace is not None:
                trace.model_end(entry, **kwargs)
        except Exception:  # noqa: BLE001
            pass

    def set_vote(self, data: dict[str, Any] | None) -> None:
        try:
            trace = self.current()
            if trace is not None:
                trace.set_vote(data)
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- 读取

    def recent(self, limit: int = 20, after_seq: int = 0) -> dict[str, Any]:
        try:
            limit = max(1, min(int(limit), MAX_TRACES))
        except Exception:  # noqa: BLE001
            limit = 20
        try:
            after = max(0, int(after_seq))
        except Exception:  # noqa: BLE001
            after = 0
        with self._lock:
            traces = [item for item in self._traces if item.seq > after]
            traces = traces[-limit:]
            latest = self._seq
        ordered = sorted(traces, key=lambda item: item.seq, reverse=True)
        payload = []
        for item in ordered:
            try:
                payload.append(item.to_dict())
            except Exception:  # noqa: BLE001 - 单条坏掉不影响其余
                continue
        return {
            "latest": latest,
            "max_traces": self._traces.maxlen,
            "traces": payload,
        }

    def clear(self) -> None:
        with self._lock:
            self._traces.clear()
            self._seq = 0


trace_store = TraceStore()

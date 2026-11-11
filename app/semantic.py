"""可选语义召回：本地 bge-small-zh-v1.5（ONNX）。

模型或依赖缺失时 available() 返回 False，调用方自动退回字符相似度，不影响启动。
向量缓存存在 sqlite（data/semantic.db），按题目 id + SimHash 复用。
"""

from __future__ import annotations

import array
import math
import os
import sqlite3
import threading
from contextlib import closing
from typing import Any

MODEL_FILES = ("model.onnx", "tokenizer.json")
SEMANTIC_THRESHOLD = 0.88


class SemanticStore:
    def __init__(self, model_dir: str, db_path: str, threshold: float = SEMANTIC_THRESHOLD) -> None:
        self.model_dir = model_dir
        self.db_path = db_path
        self.threshold = threshold
        self._lock = threading.RLock()
        self._session = None
        self._tokenizer = None
        self._tried = False
        self._cache: dict[str, tuple[str, list[float]]] = {}
        self._init_db()

    def _init_db(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS vectors (id TEXT PRIMARY KEY, sh TEXT, vec BLOB)"
                )
        except OSError:
            pass

    def _ensure(self):
        if self._tried:
            return self._session
        self._tried = True
        if not all(os.path.exists(os.path.join(self.model_dir, name)) for name in MODEL_FILES):
            return None
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except Exception:  # noqa: BLE001 - 缺依赖就当不可用
            return None
        try:
            tokenizer = Tokenizer.from_file(os.path.join(self.model_dir, "tokenizer.json"))
            tokenizer.enable_truncation(max_length=512)
            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            session = ort.InferenceSession(
                os.path.join(self.model_dir, "model.onnx"), options, providers=["CPUExecutionProvider"]
            )
            self._tokenizer = tokenizer
            self._session = session
        except Exception:  # noqa: BLE001
            self._session = None
        return self._session

    def available(self) -> bool:
        return self._ensure() is not None

    def embed(self, text: str) -> list[float] | None:
        if not text or self._ensure() is None:
            return None
        try:
            import numpy as np

            encoded = self._tokenizer.encode(text[:512])
            input_ids = np.array([encoded.ids], dtype=np.int64)
            attention = np.array([encoded.attention_mask], dtype=np.int64)
            feeds: dict[str, Any] = {"input_ids": input_ids, "attention_mask": attention}
            names = {item.name for item in self._session.get_inputs()}
            if "token_type_ids" in names:
                feeds["token_type_ids"] = np.zeros_like(input_ids)
            feeds = {key: value for key, value in feeds.items() if key in names}
            outputs = self._session.run(None, feeds)
            output_names = [item.name for item in self._session.get_outputs()]
            if "sentence_embedding" in output_names:
                vec = outputs[output_names.index("sentence_embedding")][0]
            else:
                hidden = outputs[output_names.index("last_hidden_state") if "last_hidden_state" in output_names else 0]
                vec = hidden[0][0]  # BGE 用 CLS 池化
            norm = float(np.linalg.norm(vec)) or 1.0
            return [float(x) / norm for x in vec]
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _pack(vec: list[float]) -> bytes:
        return array.array("f", vec).tobytes()

    @staticmethod
    def _unpack(blob: bytes) -> list[float]:
        arr = array.array("f")
        arr.frombytes(blob)
        return list(arr)

    def get(self, item_id: str, sh: str) -> list[float] | None:
        cached = self._cache.get(item_id)
        if cached and cached[0] == sh:
            return cached[1]
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                row = conn.execute("SELECT sh, vec FROM vectors WHERE id = ?", (item_id,)).fetchone()
            if not row or row[0] != sh:
                return None
            vec = self._unpack(row[1])
            self._cache[item_id] = (row[0], vec)
            return vec
        except (sqlite3.Error, ValueError):
            return None

    def warm_cache(self, items: list[dict[str, Any]]) -> None:
        """一批题目一次性读入向量缓存，避免逐题开 sqlite 连接。"""
        pending = [item["id"] for item in items if item.get("id") not in self._cache]
        if not pending:
            return
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                for start in range(0, len(pending), 400):
                    chunk = pending[start:start + 400]
                    placeholders = ",".join("?" for _ in chunk)
                    rows = conn.execute(
                        f"SELECT id, sh, vec FROM vectors WHERE id IN ({placeholders})", chunk
                    ).fetchall()
                    for item_id, sh, blob in rows:
                        try:
                            self._cache[item_id] = (sh, self._unpack(blob))
                        except ValueError:
                            continue
        except sqlite3.Error:
            pass

    def put(self, item_id: str, sh: str, vec: list[float]) -> None:
        self._cache[item_id] = (sh, vec)
        try:
            with self._lock, closing(sqlite3.connect(self.db_path)) as conn, conn:
                conn.execute(
                    "INSERT INTO vectors (id, sh, vec) VALUES (?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET sh = excluded.sh, vec = excluded.vec",
                    (item_id, sh, self._pack(vec)),
                )
        except sqlite3.Error:
            pass

    def forget(self, item_id: str) -> None:
        self._cache.pop(item_id, None)
        try:
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                conn.execute("DELETE FROM vectors WHERE id = ?", (item_id,))
        except sqlite3.Error:
            pass

    @staticmethod
    def cosine(left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        return float(sum(a * b for a, b in zip(left, right)))

    def ensure_vector(self, item: dict[str, Any]) -> list[float] | None:
        cached = self.get(item["id"], item.get("sh") or "")
        if cached is not None:
            return cached
        vec = self.embed(item.get("title") or "")
        if vec is not None:
            self.put(item["id"], item.get("sh") or "", vec)
        return vec

    def best_match(self, query_text: str, items: list[dict[str, Any]]) -> tuple[dict[str, Any], float] | None:
        query = self.embed(query_text)
        if query is None:
            return None
        self.warm_cache(items)
        best: tuple[dict[str, Any], float] | None = None
        for item in items:
            vec = self.ensure_vector(item)
            if vec is None:
                continue
            score = self.cosine(query, vec)
            if best is None or score > best[1]:
                best = (item, score)
        if best is None or best[1] < self.threshold:
            return None
        return best

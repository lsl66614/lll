"""运行策略设置（交叉验证、图片两阶段等）。"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from . import config
from .quality import normalize_routes

DEFAULTS: dict[str, Any] = {
    "cross_check": False,
    "image_two_stage": False,
    "inline_images": True,
    "title_fill": True,
    "multiple_conservative": False,
    "strict_official": False,
    "official_cross_check": True,
    "low_conf_vote": False,
    "semantic_match": False,
    "local_ocr": False,
    "confidence_weight": True,
    "confidence_threshold": 0.0,
    "abstain_enabled": False,
    "abstain_min_confidence": 0.0,
    "abstain_include_positional": True,
    "web_learn": False,
    "web_daily_budget": 200,
    "web_max_results": 5,
    "routes": {},
    "auto_calibrate": False,
    "auto_calibrate_limit": 50,
    "auto_calibrate_cross": False,
    "auto_vote": False,
    "auto_vote_limit": 20,
    "auto_vote_write": False,
    "auto_resolve": False,
    "batch_enabled": config.BATCH_ENABLED,
    "cache_retry_probability": config.CACHE_RETRY_PROBABILITY,
    "cache_retry_min_confidence": config.CACHE_RETRY_MIN_CONFIDENCE,
}

_BOOL_KEYS = ("cross_check", "image_two_stage", "inline_images", "title_fill",
              "multiple_conservative", "strict_official", "official_cross_check",
              "low_conf_vote", "semantic_match", "local_ocr", "confidence_weight",
              "abstain_enabled", "abstain_include_positional",
              "auto_calibrate", "auto_calibrate_cross", "auto_vote",
              "auto_vote_write", "auto_resolve", "web_learn", "batch_enabled")
_INT_KEYS = {"auto_calibrate_limit": (1, 500), "auto_vote_limit": (1, 200),
             "web_daily_budget": (1, 5000), "web_max_results": (1, 10)}
_FLOAT_KEYS = {"confidence_threshold": (0.0, 1.0),
               "abstain_min_confidence": (0.0, 1.0),
               "cache_retry_probability": (0.0, 1.0),
               "cache_retry_min_confidence": (0.0, 1.0)}


class Settings:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = dict(DEFAULTS)
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            self._save()
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if isinstance(data, dict):
                for key in _BOOL_KEYS:
                    if key in data:
                        self._data[key] = bool(data[key])
                for key, (low, high) in _INT_KEYS.items():
                    if key in data:
                        try:
                            self._data[key] = min(high, max(low, int(data[key])))
                        except (TypeError, ValueError):
                            pass
                for key, (low, high) in _FLOAT_KEYS.items():
                    if key in data:
                        try:
                            self._data[key] = round(min(high, max(low, float(data[key]))), 4)
                        except (TypeError, ValueError):
                            pass
                if "routes" in data:
                    self._data["routes"] = normalize_routes(data["routes"])
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(self._data, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def all(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def update(self, data: Any) -> dict[str, Any]:
        with self._lock:
            if isinstance(data, dict):
                for key in _BOOL_KEYS:
                    if key in data:
                        self._data[key] = bool(data[key])
                for key, (low, high) in _INT_KEYS.items():
                    if key in data:
                        try:
                            self._data[key] = min(high, max(low, int(data[key])))
                        except (TypeError, ValueError):
                            pass
                for key, (low, high) in _FLOAT_KEYS.items():
                    if key in data:
                        try:
                            self._data[key] = round(min(high, max(low, float(data[key]))), 4)
                        except (TypeError, ValueError):
                            pass
                if "routes" in data:
                    self._data["routes"] = normalize_routes(data["routes"])
                self._save()
            return dict(self._data)

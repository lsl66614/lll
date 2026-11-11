"""课程自动分类：按关键词规则从题干/选项推断专业。"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

DEFAULT_SUBJECT = "默认"

DEFAULT_RULES: list[dict[str, Any]] = [
    {"name": "线性代数", "keywords": ["行列式", "矩阵", "向量", "秩", "线性方程组", "初等变换", "特征值", "克拉默"]},
    {"name": "计算机组成原理", "keywords": ["CPU", "指令", "寻址", "寄存器", "流水线", "Cache", "存储器", "总线", "中断"]},
    {"name": "数据结构", "keywords": ["二叉树", "链表", "栈", "队列", "哈希", "排序", "时间复杂度", "结点", "遍历"]},
    {"name": "概率论与数理统计", "keywords": ["概率", "随机变量", "期望", "方差", "分布", "假设检验", "协方差"]},
    {"name": "大学英语", "keywords": ["passage", "according to the passage", "choose the best", "blank"]},
]


def _clean_rules(raw: Any) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return rules
    seen: set[str] = set()
    for entry in raw[:50]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()[:40]
        if not name or name in seen:
            continue
        keywords = entry.get("keywords")
        if not isinstance(keywords, list):
            continue
        cleaned = []
        for keyword in keywords[:80]:
            text = str(keyword or "").strip()
            if text and text not in cleaned:
                cleaned.append(text[:60])
        if not cleaned:
            continue
        seen.add(name)
        rules.append({"name": name, "keywords": cleaned})
    return rules


class SubjectRules:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._rules: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                rules = data.get("rules") if isinstance(data, dict) else data
                cleaned = _clean_rules(rules)
                if cleaned:
                    self._rules = cleaned
                    return
            except (OSError, json.JSONDecodeError, ValueError):
                pass
        self._rules = [dict(rule) for rule in DEFAULT_RULES]
        self._save()

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump({"rules": self._rules}, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(rule) for rule in self._rules]

    def replace(self, rules: Any) -> list[dict[str, Any]]:
        cleaned = _clean_rules(rules)
        with self._lock:
            self._rules = cleaned or [dict(rule) for rule in DEFAULT_RULES]
            self._save()
            return [dict(rule) for rule in self._rules]

    def infer(self, title: str, options: list[str]) -> tuple[str, int]:
        text = (str(title or "") + " " + " ".join(str(o) for o in options or [])).lower()
        best_name, best_score = "", 0
        with self._lock:
            rules = list(self._rules)
        for rule in rules:
            score = sum(1 for keyword in rule["keywords"] if keyword.lower() in text)
            if score > best_score:
                best_name, best_score = rule["name"], score
        return (best_name, best_score) if best_score else ("", 0)

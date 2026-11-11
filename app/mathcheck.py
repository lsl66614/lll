"""数学答案等价判定（可选 Math-Verify）。

只用于**纯数学表达式**：数值、分数、百分数、LaTeX 公式。中文答案（判断题、
中文填空）不进入这里，避免 ``3.5米`` 与 ``3.5秒`` 被剥掉单位后误判成同一个答案。

Windows 上必须显式关闭 Math-Verify 自带的超时：它在 Windows 用
``multiprocessing.Process`` 实现超时，而本机用户目录含中文时 spawn 会直接
失败（``OSError: [WinError 87]``）。关掉后解析本身是毫秒级，超时由调用方
的整体预算兜底。

未安装 ``math-verify`` 时所有函数安全降级：``available()`` 返回 False，
``math_equivalent()`` 返回 False，原有文本/数值逻辑不受影响。
"""
from __future__ import annotations

import functools
import logging
import re
import threading

# 中文（含全角标点）不交给 Math-Verify：单位会被剥掉，导致不同答案被判等。
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
# 出现这些记号时按 LaTeX 处理（Math-Verify 只认 $...$ 里的公式）。
_LATEX_HINT_RE = re.compile(r"\\[a-zA-Z]+|[\^_{}]")
_FULLWIDTH_RE = re.compile(r"[！-～]")
_MINUS_MAP = str.maketrans({"−": "-", "–": "-", "—": "-", "×": "*", "÷": "/"})
_MAX_LEN = 200

_load_lock = threading.Lock()
_loaded = False
_parser = None
_verifier = None


def _load():
    """懒加载 Math-Verify；导入失败或环境不支持时返回 (None, None)。"""
    global _loaded, _parser, _verifier
    if _loaded:
        return _parser, _verifier
    with _load_lock:
        if _loaded:
            return _parser, _verifier
        _loaded = True
        try:
            from math_verify import parse, verify
        except Exception:  # noqa: BLE001 - 可选依赖，缺失时静默降级
            return None, None
        # 关掉它自己的 Windows 多进程超时（中文用户名下 spawn 会失败）。
        logging.getLogger("math_verify.parser").setLevel(logging.ERROR)
        logging.getLogger("math_verify.grader").setLevel(logging.ERROR)
        _parser = parse
        _verifier = verify
        return _parser, _verifier


def available() -> bool:
    parser, verifier = _load()
    return parser is not None and verifier is not None


def _halfwidth(text: str) -> str:
    return _FULLWIDTH_RE.sub(lambda m: chr(ord(m.group()) - 0xFEE0), text)


def _text_key(text: str) -> str:
    """只去空白，不改大小写 —— 数学里 X 与 x 可能是不同变量。"""
    return "".join(_halfwidth(str(text or "")).split())


def _prepare(text: str) -> str | None:
    """把答案整理成 Math-Verify 能吃的形式；不适合数学判定的返回 None。"""
    value = str(text or "").strip()
    if not value:
        return None
    value = _halfwidth(value).translate(_MINUS_MAP).strip("。；;，,、")
    if not value or len(value) > _MAX_LEN:
        return None
    if _CJK_RE.search(value):
        return None
    if "$" in value:
        return value
    if _LATEX_HINT_RE.search(value):
        return f"${value}$"
    return value


@functools.lru_cache(maxsize=4096)
def _equivalent_cached(left: str, right: str) -> bool:
    parser, verifier = _load()
    if parser is None or verifier is None:
        return False
    try:
        parsed_left = parser(left, parsing_timeout=None)
        parsed_right = parser(right, parsing_timeout=None)
    except Exception:  # noqa: BLE001 - 解析失败按“不等价”处理
        return False
    if not parsed_left or not parsed_right:
        return False
    try:
        # verify 不是严格对称的（gold 侧有特殊处理），两个方向都试。
        return bool(verifier(parsed_right, parsed_left, timeout_seconds=None)) or bool(
            verifier(parsed_left, parsed_right, timeout_seconds=None)
        )
    except Exception:  # noqa: BLE001
        return False


def math_equivalent(first: object, second: object) -> bool:
    """判断两个答案是否**数学等价**。

    - ``1/2`` / ``0.5`` / ``\\frac{1}{2}`` → True
    - ``0.0833`` / ``1/12`` → False（近似不等于精确）
    - ``3.5米`` / ``3.5秒`` → False（含中文，不交给数学解析）
    - 未安装 Math-Verify → False（调用方回退原有逻辑）
    """
    left_raw = str(first or "").strip()
    right_raw = str(second or "").strip()
    if not left_raw or not right_raw:
        return False
    if _text_key(left_raw) == _text_key(right_raw):
        return True
    # 库不可用时直接返回，不进 lru_cache —— 否则「不可用」的 False 会被缓存，
    # 之后即使装上依赖也仍然得到 False。
    parser, verifier = _load()
    if parser is None or verifier is None:
        return False
    left = _prepare(left_raw)
    right = _prepare(right_raw)
    if left is None or right is None:
        return False
    return _equivalent_cached(left, right)


def cache_clear() -> None:
    """测试用：清空等价缓存。"""
    _equivalent_cached.cache_clear()

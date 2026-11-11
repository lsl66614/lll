"""题库配置（answerer wrapper）的风险扫描。

背景（2026-11-11 实测，参考官方 OCS 脚本 v5.4.0 的 scanAnswerWrapperRisks）：

官方脚本里「题库」是可配置的：用户填一个 URL，再填一段 ``handler`` 代码，
这段代码由 ``Function(...)`` 动态构造、**以用户登录身份在课程页面上执行**。
所以一份从第三方抄来的题库配置，等于让第三方的代码在你的账号上下文里跑。

官方为此做了一道「导入前扫描」：用一张特征表扫 handler 源码，命中就弹窗
列出可疑点，让用户确认。我们管理台生成的 v2 配置里同样有 handler 字段，
而且用户可以自己粘任意题库配置 —— 这个缺口必须补上。

扫描不是"判定恶意"，只是把**值得用户多看一眼**的地方标出来。
"""
from __future__ import annotations

import re
from typing import Any

# 每条：(标识, 说明, 正则)。命中不等于恶意，只是让人确认一下。
DANGEROUS_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("eval", "动态执行字符串（eval / new Function）",
     re.compile(r"\beval\s*\(|\bnew\s+Function\s*\(")),
    ("function", "Function() 动态构造代码",
     re.compile(r"\bFunction\s*\(")),
    ("fetch", "发起网络请求（fetch / XMLHttpRequest）",
     re.compile(r"\bfetch\s*\(|XMLHttpRequest|\.open\s*\(\s*['\"]")),
    ("gm-request", "油猴跨域请求（GM_xmlhttpRequest 等）",
     re.compile(r"\bGM_(?:xmlhttpRequest|setClipboard|download)\b")),
    ("cookie", "读写 Cookie", re.compile(r"\bdocument\.cookie\b|\bcookieStore\b")),
    ("storage", "读写本地存储（localStorage / sessionStorage / indexedDB）",
     re.compile(r"\b(?:local|session)Storage\b|\bindexedDB\b")),
    ("beacon", "上报数据（sendBeacon / WebSocket）",
     re.compile(r"\bsendBeacon\b|\bnew\s+WebSocket\b")),
    ("navigation", "跳转或修改页面地址（location / window.open）",
     re.compile(r"\blocation\s*[.=]|\bwindow\.open\s*\(")),
    ("dom", "读写页面 DOM", re.compile(r"\bdocument\.(?!cookie)\w+|\bquerySelector\w*\s*\(")),
    ("prototype", "触及原型链（__proto__ / prototype / constructor）",
     re.compile(r"__proto__|\bprototype\b|\.constructor\b")),
    ("charcode", "字符编码转换（fromCharCode，可能在藏混淆代码）",
     re.compile(r"fromCharCode|charCodeAt\s*\(\s*\d+\s*\)")),
    ("computed", "动态属性访问（obj[key]，可能藏混淆代码）",
     re.compile(r"\[\s*[^'\"\[\]]+\s*\]\s*\(")),
    ("import", "动态加载代码（import / require）",
     re.compile(r"\bimport\s*\(|\brequire\s*\(")),
    ("postmessage", "跨窗口通信（postMessage）", re.compile(r"\bpostMessage\b")),
)

# 这些是"正常题库配置会用到"的，不该报。
_ALLOWED = {
    # 我们自己生成的 v2 handler 就是这个形状
    "function": True,
}


def _collect_handlers(config: Any, path: str = "handler") -> list[tuple[str, str]]:
    """从题库配置里把 handler 源码都找出来：[(路径, 源码)]。"""
    found: list[tuple[str, str]] = []
    if isinstance(config, dict):
        for key, value in config.items():
            if key == "handler" and isinstance(value, str):
                found.append((f"{path}", value))
            elif isinstance(value, (dict, list)):
                found.extend(_collect_handlers(value, f"{path}.{key}"))
    elif isinstance(config, list):
        for index, value in enumerate(config):
            found.extend(_collect_handlers(value, f"{path}[{index}]"))
    return found


def scan_config_risks(config: Any) -> list[dict[str, Any]]:
    """扫描一份题库配置，返回命中的风险项。

    返回的每项：``{"id", "label", "path", "detail"}``。
    """
    risks: list[dict[str, Any]] = []
    for path, source in _collect_handlers(config):
        if not source.strip():
            continue
        for rid, label, pattern in DANGEROUS_PATTERNS:
            if rid in _ALLOWED:
                continue
            match = pattern.search(source)
            if not match:
                continue
            risks.append({
                "id": rid,
                "label": label,
                "path": path,
                "detail": match.group(0)[:60],
            })
    return risks


def scan_single_handler(source: str) -> list[dict[str, Any]]:
    """只扫一段 handler 源码（管理台实时校验用）。"""
    return scan_config_risks({"handler": source})


def risk_summary(risks: list[dict[str, Any]]) -> str:
    """给人看的一句话摘要。"""
    if not risks:
        return ""
    labels = sorted({r["label"] for r in risks})
    return "这份题库配置会在页面上执行以下操作：" + "；".join(labels)

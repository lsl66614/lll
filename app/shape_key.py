"""题干「文字骨架」与截断容错匹配。

背景（2026-11-09 实测）：
- **我们自己的网关**早期把请求题干截到 300 字再发给模型，公式图片题的题干常超
  300 字（题库存的是 478 字完整题干）。
  （注：**不是官方 OCS 脚本截的**。官方 v5.4.0 和浏览器里实际装的 v5.3.1 都
  原样传 `env.title = params.title`，全文没有任何题干长度上限。）
- 同一道题的图片 URL 还会随出卷时间变化（bab9cac5 → 5031cf4b）。

结果：完整题干的官方记录永远匹配不上请求，被截断的同名 AI 记录反而
「完全匹配」（dice=1.000）抢走答案 —— 实测一道官方答案 6 的题返回了
AI 猜的 4。

思路：题干里的图片 URL 无论是完整的还是被截断的半截，本质上都是
「噪声」；真正区分题目的是**文字部分**。把 URL 连同其残留碎片一起去掉，
只留文字骨架，就能让「完整题干」与「截断题干」对上。

截断点落在 URL 中间时，截断后的尾巴（``httpedui...``）需要一并去掉：
做法是把文字按「URL 出现位置」切成片段，只保留第一段完整文字 + 后续
以中文/字母数字开头的片段，丢弃以 http 开头的那段。
"""
from __future__ import annotations

import re
from typing import Any

IMAGE_URL_RE = re.compile(
    r"https?://[^\s\"'<>]+?\.(?:png|jpe?g|gif|bmp|webp)(?:\?[^\s\"'<>]*)?", re.IGNORECASE
)
# 被截断的 URL 碎片：形如 http...（没有完整后缀），出现在文本中
URL_FRAGMENT_RE = re.compile(r"https?://\S*", re.IGNORECASE)

# 骨架匹配的最小长度：短于此值不比对，避免「1+1=?」这类短题干乱匹配
SKELETON_MIN_LEN = 10
# 短骨架至少要覆盖长骨架这么多比例
SKELETON_MIN_RATIO = 0.55


def strip_urls(text: Any) -> str:
    """去掉完整图片 URL 与被截断的 URL 碎片，只留文字。"""
    value = str(text or "")
    value = IMAGE_URL_RE.sub(" ", value)
    value = URL_FRAGMENT_RE.sub(" ", value)
    return value


def text_key(text: Any) -> str:
    """文字骨架（只留字母数字与中文）。"""
    return "".join(ch for ch in strip_urls(text).lower() if ch.isalnum())


def truncated_match(short_key: str, long_key: str) -> bool:
    """短题干是否是长题干被截断后的版本（文字骨架层面）。

    判据（都要满足）：
    1. 短骨架是长骨架的前缀；
    2. 长度比例不低于阈值（太短的题干不参与，避免乱匹配）。

    ``same_question_truncated`` 是更严的版本，会额外校验图片 URL 序列，
    优先用它。
    """
    if not short_key or not long_key:
        return False
    if short_key == long_key:
        return True
    if len(short_key) >= len(long_key):
        return False
    if len(short_key) < SKELETON_MIN_LEN:
        return False
    if len(short_key) / len(long_key) < SKELETON_MIN_RATIO:
        return False
    return long_key.startswith(short_key)


def has_image(text: Any) -> bool:
    """题干里是否含图片（完整 URL 或被截断的片段）。"""
    value = str(text or "")
    return bool(IMAGE_URL_RE.search(value)) or "http" in value.lower()


def image_urls(text: Any) -> list[str]:
    """题干里完整的图片 URL（按出现顺序）。"""
    return IMAGE_URL_RE.findall(str(text or ""))


def url_fingerprint(url: str) -> str:
    """图片 URL → 稳定指纹：去掉协议和 CDN host，只留文件名主干。

    同一张公式图会被不同出卷版本换成不同 host / 不同查询串，
    但文件名主干（hash 部分）不变，所以它才是「这张图是谁」的稳定标识。
    """
    path = str(url or "").split("?", 1)[0]
    path = re.sub(r"^https?://", "", path)
    path = re.sub(
        r"^(?:[a-z0-9-]+\.)*(?:nosdn|127\.net|edu-image|mooc-image|img)[^/]*/", "", path)
    tail = path.rsplit("/", 1)[-1]
    # 大小写不敏感：真实 URL 里是 `_PhotoUploadUtils_`，而题库判重那条链路
    # 会先把整段题干 lower() 再处理 —— 这里不做 IGNORECASE 就永远剥不掉前缀。
    # 注意 `_?` 必须罩住**所有**分支：旧写法把无下划线的分支单独放，
    # 于是 `_PhotoUploadUtils_…` 三条分支全落空，前缀从来没被剥掉过。
    tail = re.sub(
        r"^_?(?:photon?uploadutils|uploadutils)_?", "", tail, flags=re.IGNORECASE)
    tail = re.sub(r"\.(?:png|jpe?g|gif|bmp|webp)$", "", tail, flags=re.IGNORECASE)
    # 统一小写：同一张图的 hash/GUID 大小写不敏感，统一后才能跨来源比对
    return tail.lower() or "empty"


def image_key(text: Any) -> str:
    """题干里图片的身份：按出现顺序的 URL 指纹，用 ``|`` 连接；无图返回空串。

    这是题库记录一直缺的那把钥匙：**同一段文字配不同公式图是不同的题**，
    而记录一旦丢了图片 URL（早期结果页解析器会把图压成空括号），
    就再也区分不开是哪一道，只能靠文字骨架猜 —— 实测 12 个骨架对应多道题、
    覆盖 46/434 条判断题用例。

    用它做题库派生列后，「同文字不同图」第一次有了可比对的依据。
    注意：**它不是 ``image_hash``** —— 后者是客户端上传 base64 图片的
    sha256，只在请求带图时才有，库里 99.5% 为空。
    """
    return "|".join(url_fingerprint(u) for u in image_urls(text))


def same_question_truncated(request: str, stored: str) -> bool:
    """请求题干是否是库记录被截断后的同一道题。

    判据（都要满足）：
    1. 两者都含图片（纯文字题走原有的相似度匹配）；
    2. 文字骨架上，请求是库记录的前缀；
    3. 请求里**完整**的图片 URL 序列是库记录图片序列的前缀 —— 这样
       「文字相同但图不同」的两道题不会被混为一谈（那会导致拿错官方答案）。

    文字骨架很短时（不足 10 字），第 2 条改用「骨架是前缀 且 图片序列
    完全一致且长度相同」这种更严的判断，避免短题干乱匹配。

    **例外：库存记录的图被压成了空括号**。
    2026-11-10 实测：早期版本的结果页解析器会把图片 URL 抹掉，库存里留下
    ``则P( )= （ ）`` 这种形状。这样的记录 ``has_image(stored)`` 是 False，
    第 1 条直接把它拒了 —— 于是「官方答案 B」的记录匹配不上带图请求，
    反倒让一条 ``calibrated`` 的错误答案 C 顶上来（实测同一道题答错）。

    这种情况没法比 URL（库存里没有 URL 了），只能比文字骨架：骨架**完全相同**
    才认。骨架相同意味着题干文字一字不差，只差图的表示方式。
    """
    if not request or not stored:
        return False
    req_has_image = has_image(request)
    stored_has_image = has_image(stored)
    if not (req_has_image and stored_has_image):
        # 库存里的图被压成空括号：退化成「文字骨架完全相同」
        if not req_has_image or stored_has_image:
            return False
        req_skeleton = text_key(request)
        stored_skeleton = text_key(stored)
        return bool(req_skeleton) and req_skeleton == stored_skeleton
    req_urls = image_urls(request)
    stored_urls = image_urls(stored)
    if len(req_urls) > len(stored_urls):
        return False
    if stored_urls[: len(req_urls)] != req_urls:
        return False
    req_skeleton = text_key(request)
    long_skeleton = text_key(stored)
    if req_skeleton == long_skeleton:
        return True
    if len(req_skeleton) >= len(long_skeleton):
        return False
    if len(req_skeleton) < SKELETON_MIN_LEN:
        # 骨架太短：只在图片数量也一致时才认（否则「1+1=?」这类会乱匹配）
        return len(req_urls) == len(stored_urls) and long_skeleton.startswith(req_skeleton)
    return truncated_match(req_skeleton, long_skeleton)

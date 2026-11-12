"""解析中国大学MOOC 测验回看/随堂结果页 DOM，并把对错反馈应用到本地题库。"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any

from .bank import DEFAULT_SUBJECT, translate_answer, validate_item
from .parsing import normalize_type, parse_answer, parse_options

_ZW = re.compile(r"[\u200B-\u200F\u202A-\u202E\u2060\uFEFF]")
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _text(fragment: str) -> str:
    """HTML 转文本，图片保留原 URL（与 OCS 请求、官方卷 paper 通道的写法一致）。

    图片题的内容全在公式图里，去掉 URL 会让不同来源的同题记录对不上：
    请求侧带 URL、paper 通道带 URL，只有结果页曾把 URL 抹掉。
    """
    raw = re.sub(r'<img[^>]*src="([^"]+)"[^>]*>', r" \1 ", fragment or "", flags=re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html_lib.unescape(raw)
    raw = _ZW.sub("", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _plain_text(fragment: str) -> str:
    """HTML 转纯文本，图片整体丢弃（用于「正确答案」「你选对了」这类无图文本）。"""
    raw = re.sub(r"<[^>]+>", " ", fragment or "")
    raw = html_lib.unescape(raw)
    raw = _ZW.sub("", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _option_body(body: str) -> str:
    """取选项正文：只认 optionCnt 内部。

    结果页里 <li> 除了选项正文，还有 score 区块（0.00/10.00 的对错图标），
    整段搜 u-icon-correct 会把「得分图标」当成选项内容，把公式图片选项
    写成「正确」。这里只取 optionCnt 的内容，图标只从正文内部认。
    """
    cnt = re.search(r'class="[^"]*optionCnt[^"]*"[^>]*>(.*?)</div>', body, re.S)
    return cnt.group(1) if cnt else ""


def _option_glyph(inner: str) -> str:
    """选项正文里的对错图标（判断题用 ✓/✗ 渲染）。"""
    if "u-icon-correct" in inner:
        return "正确"
    if "u-icon-wrong" in inner:
        return "错误"
    return ""


def _option_text(inner: str) -> str:
    """单个选项的入库文本：图标优先，其次正文文字（含图片 URL）。"""
    glyph = _option_glyph(inner)
    if glyph:
        return glyph
    return _text(inner)


def _answer_text(item: dict[str, Any]) -> str | None:
    qtype = normalize_type(item.get("type"))
    options = parse_options(item.get("options"))
    letters = [str(x).upper() for x in (item.get("correct_letters") or []) if str(x).strip()]
    correct_text = str(item.get("correct_text") or "").strip()
    if qtype == "judge":
        if correct_text in ("正确", "错误"):
            return correct_text
        if letters:
            index = _LETTERS.index(letters[0])
            if 0 <= index < len(options):
                return options[index]
        return None
    if qtype == "single":
        return letters[0] if letters else None
    if qtype == "multiple":
        picked = sorted({_LETTERS.index(x) for x in letters if x in _LETTERS})
        return "#".join(_LETTERS[i] for i in picked) if picked else None
    if qtype == "completion":
        # 填空题的答案就是 tt2 里的原文（解析器放在 correct_text）
        return correct_text or str(item.get("answer") or "").strip() or None
    return None


def parse_result_html(html: str) -> list[dict[str, Any]]:
    """从一个结果页 HTML 里抽取题目、正确选项与对错。"""
    parts = re.split(r'(?=<div[^>]*class="[^"]*u-questionItem)', html or "")
    items: list[dict[str, Any]] = []
    for block in parts:
        if "u-questionItem" not in block[:300]:
            continue
        title_match = re.search(
            r'class="[^"]*j-richTxt[^"]*"[^>]*>(.*?)</div>\s*<div class="qaMark', block, re.S
        ) or re.search(r'class="[^"]*j-richTxt[^"]*"[^>]*>(.*?)</div>', block, re.S)
        title = _text(title_match.group(1)) if title_match else ""
        if not title:
            continue

        options: list[str] = []
        selected: list[str] = []
        for li_match in re.finditer(r"<li([^>]*)>(.*?)</li>", block, re.S):
            attrs, body = li_match.group(1), li_match.group(2)
            pos = re.search(r'class="[^"]*optionPos[^"]*"[^>]*>([^<]*)<', body)
            letter = ""
            if pos:
                letters = re.findall(r"[A-Za-z]", pos.group(1))
                letter = letters[0].upper() if letters else ""
            if not letter:
                letter = _LETTERS[len(options)] if len(options) < len(_LETTERS) else ""
            options.append(_option_text(_option_body(body)))
            if "checked" in attrs or re.search(r"<input[^>]*checked", body):
                selected.append(letter)

        # 答题页（examMode）也带一个隐藏的 analysisInfo 答案块，里面的 tt2
        # 是未提交时的占位/错位值：实测同一道题答题页写 2、提交后的结果页写 3，
        # 而且整页的 tt2 还会整体错位（第 9 题拿到第 8 题的答案）。只有
        # 「不是 examMode」且 analysisInfo 没有 f-dn 隐藏类时，tt2 才是平台
        # 确认的正确答案。否则一律不产出答案，避免把答题页当官方反馈写库。
        cls_match = re.search(r'class="([^"]*)"', block)
        block_class = cls_match.group(1) if cls_match else ""
        analysis_match = re.search(
            r'<div class="([^"]*analysisInfo[^"]*)"([^>]*)>', block
        )
        analysis_hidden = bool(
            analysis_match
            and "examMode" not in block_class
            and re.search(r"(^|\s)f-dn(\s|$)", analysis_match.group(1))
        )
        # f-dn 只代表 CSS 隐藏，不代表答案不可信：实测 quizscore 结果页里，
        # 填空题的正确答案就留在 analysisInfo.answrong.f-dn 里（选择/判断
        # 则根本不渲染答案节点）。是否采纳交给调用方（allow_hidden）；
        # examMode 一律不产出，因为它的隐藏值本身就是占位/错位的。
        analysis_visible = bool(
            analysis_match and "examMode" not in block_class and not analysis_hidden
        )
        tt2 = tt3 = None
        if analysis_visible or analysis_hidden:
            end = block.find("</div>", analysis_match.end())
            segment = block[analysis_match.start():end if end >= 0 else len(block)]
            tt2 = re.search(r'class="[^"]*tt2[^"]*"[^>]*>([^<]*)<', segment)
            tt3 = re.search(r'class="[^"]*tt3[^"]*"[^>]*>([^<]*)<', segment)
        correct_text_raw = _plain_text(tt2.group(1)) if tt2 else ""
        correct_letters = [x.upper() for x in re.findall(r"[A-Za-z]", correct_text_raw)]
        status_text = _plain_text(tt3.group(1)) if tt3 else ""

        # 填空题：页面里是 textarea + 「你没有填写答案」的提示，没有 <li> 选项。
        # 不认出来的话会被当成 single，而 tt2 里就是答案本身（不是选项字母），
        # 于是 correct_letters 解析成空 → 官方答案写不进题库。
        is_completion = bool(
            re.search(r'class="[^"]*j-textarea[^"]*"', block)
            or re.search(r'class="[^"]*inputtxt[^"]*"', block)
            or "请输入答案" in block
            or "你没有填写答案" in block
        )
        if is_completion:
            qtype = "completion"
        elif 'type="checkbox"' in block:
            qtype = "multiple"
        elif len(options) == 2 and all(opt in ("正确", "错误") for opt in options):
            qtype = "judge"
        else:
            qtype = "single"

        correct_text = ""
        if qtype == "judge" and len(correct_letters) == 1:
            index = _LETTERS.index(correct_letters[0])
            if 0 <= index < len(options):
                correct_text = options[index]
        elif qtype == "completion":
            # 填空题的 tt2 就是答案原文（可能是 "-1"、"6"、"0.72" 这种）
            correct_text = correct_text_raw.strip()
            correct_letters = []

        items.append(
            {
                "type": qtype,
                "title": title,
                "options": options,
                "selected_letters": selected,
                "correct_letters": correct_letters,
                "correct_text": correct_text,
                "is_correct": "选对" in status_text,
                "blank": "没有选择" in status_text or not selected,
                "status_text": status_text,
                # 是否为「答案块可见」的结果页；False 表示答案块被隐藏或页
                # 面是答题页。隐藏块默认只用于诊断，除非调用方显式允许。
                "analysis_visible": analysis_visible,
                # 答案块存在但被 CSS 隐藏（quizscore 结果页的填空题会这样）。
                # 默认仍然跳过；只有分数页回捞时才允许写库。
                "analysis_hidden": analysis_hidden,
            }
        )
    return items


def _answer_fields(qtype: str, answer: str | None, options: list[str],
                   existing: dict[str, Any] | None) -> dict[str, Any]:
    """生成入库字段：答案字母必须和它所依据的选项顺序一起写。

    单选/多选的答案是「字母」，只有放进当时的选项顺序里才有意义。
    OCS 同一道题在不同场次会换选项顺序（实测 46/135 题选项集变过），
    所以只写字母、不写顺序，字母就会指向另一个选项——题库看着「有官方答案」，
    实际给出去的是错的（实测 5 道单选全错，全是这种陈旧字母）。

    因此：写答案时把选项一并写成这次的顺序，字母与顺序永远自洽。
    判断题存的是语义词（正确/错误）、填空题存的是原文，都不受换序影响，无需带选项。
    """
    fields: dict[str, Any] = {}
    if not answer:
        return fields
    if qtype in ("single", "multiple") and options:
        fields["answer"] = answer
        fields["options"] = list(options)
    return fields


def _answers_match(qtype: str, old: str, official: str, options: list[str]) -> bool:
    """比较 AI 旧答案与官方答案；先按题型归一化，避免格式差异被记成错误。

    例：多选 "C#A" 与 "A#C"、判断 "对" 与 "正确"、填空的多余空格/末尾标点，
    在精确比较下都会被判错，进而低估模型正确率。
    """
    old = str(old or "").strip()
    official = str(official or "").strip()
    if not old or not official:
        return False
    if old == official:
        return True

    def norm(text: str) -> str:
        value = parse_answer(qtype, text, options) or text
        value = re.sub(r"\s+", "", str(value)).strip("。；;，,")
        return value.upper()

    left, right = norm(old), norm(official)
    if qtype == "multiple" and left != right:
        def order(text: str) -> str:
            parts = [part for part in re.split(r"[#、,，;；/|]+", text) if part]
            return "#".join(sorted(parts))
        return order(left) == order(right)
    return left == right


def apply_feedback(bank, items: list[dict[str, Any]], subject: str | None = None, dry_run: bool = False,
                   on_verdict=None, on_official=None, scene: str = "", via: str = "",
                   allow_hidden: bool = False) -> dict[str, Any]:
    result = {"applied": 0, "created": 0, "updated": 0, "marked_wrong": 0,
              "aligned": 0, "skipped": 0, "details": []}
    subj = str(subject or "").strip() or DEFAULT_SUBJECT

    def verdict(existing_item, official_answer) -> None:
        """官方答案到手时，回看此前 AI 答案是否命中（用于模型正确率统计）。"""
        if not on_verdict or not existing_item or not official_answer:
            return
        old = str(existing_item.get("answer") or "").strip()
        if not old:
            return
        try:
            same = _answers_match(
                str(existing_item.get("type") or ""),
                old,
                str(official_answer),
                parse_options(existing_item.get("options")) or [],
            )
        except Exception:  # noqa: BLE001 - 归一化失败退回精确比较
            same = old == str(official_answer).strip()
        try:
            on_verdict(existing_item, official_answer, same, scene)
        except Exception:  # noqa: BLE001 - 统计失败不影响回流
            pass

    def official(item, official_answer, source: str | None = None) -> None:
        """官方答案写回答题记忆；不依赖题库里是否已有这道题、旧答案是否为空。"""
        if not on_official or not item or not official_answer:
            return
        try:
            on_official(item, official_answer, source or via or "feedback", scene)
        except Exception:  # noqa: BLE001 - 记忆写失败不影响回流
            pass

    for raw in items:
        if not isinstance(raw, dict):
            result["skipped"] += 1
            continue
        if raw.get("analysis_visible") is False and not (
                allow_hidden and raw.get("analysis_hidden")):
            # 答题页的隐藏答案块，不是官方反馈；只用于诊断，禁止写库。
            # 唯一的例外是分数页回捞（allow_hidden=True），且只放行
            # parse_result_html 标记为 analysis_hidden 的块。
            result["skipped"] += 1
            continue
        qtype = normalize_type(raw.get("type"))
        title = str(raw.get("title") or "").strip()
        options = parse_options(raw.get("options"))
        if qtype is None or not title:
            result["skipped"] += 1
            continue
        if qtype in ("single", "multiple") and len(options) < 2:
            result["skipped"] += 1
            continue

        existing = bank.find_exact(qtype, title, options, subj) or bank.match(qtype, title, options, subj)
        if not existing:
            for candidate_subject in bank.subjects():
                hit = bank.find_exact(qtype, title, options, candidate_subject)
                if hit:
                    existing = hit
                    subj = candidate_subject
                    break
        if existing:
            subj = existing.get("subject") or subj
        answer = _answer_text(raw)
        if answer is None and raw.get("is_correct"):
            selected = [str(x).upper() for x in (raw.get("selected_letters") or [])]
            answer = _answer_text({**raw, "correct_letters": selected,
                                   "correct_text": raw.get("correct_text") or ""})
        if answer is not None and qtype == "judge":
            answer = parse_answer("judge", answer, options) or answer
        if answer is not None and validate_item({"type": qtype, "title": title, "options": options, "answer": answer}) is not None:
            answer = None

        detail = {"title": title[:80], "type": qtype, "answer": answer,
                  "is_correct": bool(raw.get("is_correct")), "blank": bool(raw.get("blank")),
                  "action": "skip"}
        if dry_run:
            result["details"].append(detail)
            continue

        official(
            {
                "type": qtype,
                "title": title,
                "options": options,
                "subject": subj,
                "image_hash": str(raw.get("image_hash") or ""),
            },
            answer,
        )

        if raw.get("is_correct"):
            verdict(existing, answer)
            if answer:
                # 结果页（page）比试卷接口（paper）更权威：同题只要存在一条
                # page 官方记录且与 paper 答案不同，就保留 page，并把整组官方
                # 记录与答题记忆都对齐到 page，避免「改了一条、查询读到另一条」。
                page_hit = None
                if via == "paper":
                    page_hit = bank.page_official_answer(
                        qtype, title, options,
                        image_hash=str(raw.get("image_hash") or ""),
                    )
                    if page_hit is None and existing \
                            and existing.get("source") == "official" \
                            and existing.get("official_via") == "page" \
                            and str(existing.get("answer") or "").strip():
                        page_hit = bank.get(existing["id"]) or existing
                if page_hit is not None:
                    page_options = parse_options(page_hit.get("options"))
                    page_answer = str(page_hit.get("answer") or "").strip()
                    paper_in_page = translate_answer(answer, options, page_options, qtype)
                    try:
                        same_as_page = bool(paper_in_page) and _answers_match(
                            qtype, paper_in_page, page_answer, page_options)
                    except Exception:  # noqa: BLE001 - 归一化失败退回精确比较
                        same_as_page = paper_in_page == page_answer
                    if not same_as_page:
                        detail["action"] = "keep-page"
                        aligned = bank.align_official(
                            qtype, title, page_options, page_answer,
                            via="page", image_hash=str(raw.get("image_hash") or ""),
                        )
                        if aligned:
                            result["aligned"] += aligned
                            detail["aligned"] = aligned
                        official(
                            {
                                "type": qtype,
                                "title": title,
                                "options": page_options,
                                "subject": subj,
                                "image_hash": str(raw.get("image_hash") or ""),
                            },
                            page_answer,
                            "page",
                        )
                        result["applied"] += 1
                        result["details"].append(detail)
                        continue
                if existing:
                    via_fields = {}
                    if via:
                        old_via = str(existing.get("official_via") or "")
                        if not (old_via == "page" and via != "page"):
                            via_fields["official_via"] = via
                    bank.update(existing["id"], {**_answer_fields(qtype, answer, options, existing),
                                                 "status": "adopted",
                                                 "source": "official", "conflict": False,
                                                 **via_fields})
                    result["updated"] += 1
                else:
                    created = bank.upsert_calibrated(qtype, title, options, answer, subj, adopted=True, source="official")
                    if created and via:
                        bank.update(created["id"], {"official_via": via})
                    result["created"] += 1
                aligned = bank.align_official(
                    qtype, title, options, answer,
                    via=via, image_hash=str(raw.get("image_hash") or ""),
                )
                if aligned:
                    result["aligned"] += aligned
                    detail["aligned"] = aligned
                detail["action"] = "adopt"
            elif existing:
                bank.promote(existing["id"])
                result["updated"] += 1
                detail["action"] = "promote"
            else:
                result["skipped"] += 1
                result["details"].append(detail)
                continue
            result["applied"] += 1
        else:
            verdict(existing, answer)
            picked = "#".join(sorted({str(x).upper() for x in (raw.get("selected_letters") or []) if str(x).strip()}))
            old_answer = str((existing or {}).get("answer") or "")
            wrong_answers = list((existing or {}).get("wrong_answers") or [])
            # 没解析出正确答案时无法判定库里的答案错了，只记录错答，不标记。
            # 旧实现先用 mark_wrong 清空答案、再 update 写新答案：两次落盘之间
            # 进程被杀/update 被来源优先级拦下，题库就只剩一条 pending 空记录，
            # 下次查询直接退化成 AI。改成「先算好、校验过、一次写入」，
            # 旧答案由 update 自己进 history，语义不变但不丢数据。
            if old_answer and answer and old_answer != answer and old_answer not in wrong_answers:
                wrong_answers.append(old_answer)
            if picked and picked != answer and picked not in wrong_answers:
                wrong_answers.append(picked)
            if answer:
                fields = _answer_fields(qtype, answer, options, existing)
                fields.update({
                    "status": "adopted",
                    "source": "official",
                    "conflict": False,
                })
                if wrong_answers:
                    fields["wrong_answers"] = wrong_answers[-20:]
                if existing:
                    updated = bank.update(existing["id"], fields)
                    if updated is None:
                        # 记录在写入前消失（并发删除/重载）：旧答案原样保留。
                        result["skipped"] += 1
                        detail["action"] = "update-failed"
                        result["details"].append(detail)
                        continue
                    if old_answer and old_answer != answer:
                        result["marked_wrong"] += 1
                    result["updated"] += 1
                else:
                    created_item = bank.upsert_calibrated(qtype, title, options, answer, subj, adopted=True, source="official")
                    result["created"] += 1
                    if created_item and wrong_answers:
                        bank.update(created_item["id"], {"wrong_answers": wrong_answers[-20:]})
                aligned = bank.align_official(
                    qtype, title, options, answer,
                    via=via, image_hash=str(raw.get("image_hash") or ""),
                )
                if aligned:
                    result["aligned"] += aligned
                    detail["aligned"] = aligned
                detail["action"] = "fix"
                result["applied"] += 1
            elif existing:
                # 有错答记录、但没有解析出正确答案：不动答案，只把这次选错的
                # 字母记进 wrong_answers，给管理员/AI 复盘用。
                if picked and picked != old_answer and picked not in wrong_answers:
                    wrong_answers.append(picked)
                if wrong_answers:
                    bank.update(existing["id"], {"wrong_answers": wrong_answers[-20:]})
                detail["action"] = "wrong"
                result["applied"] += 1
            else:
                result["skipped"] += 1
        result["details"].append(detail)

    result["details"] = result["details"][:40]
    return result

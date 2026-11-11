"""从扫描版《高职线性代数100题》裁出带公式的题目图，生成图片公式题评测集。

PDF 有文本层，题号坐标可用 ``page.search_for`` 精确定位；按题号区间裁剪，
渲染 3 倍缩放（约 216 DPI），保证矩阵与上下标可读。
"""

from __future__ import annotations

import json
import os

import pymupdf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF = r"C:\Users\刘帅良\AppData\Local\Temp\ocs_eval\pdf\linalg100.pdf"
OUT_DIR = os.path.join(ROOT, "tools", "eval_images_20261110")
OUT_JSON = os.path.join(ROOT, "tools", "eval_image_questions_20261110.json")

# (id, 页码, 裁剪顶 y, 裁剪底 y, 选项, 答案, 原题号)
# 顶边不能用题号基线减固定值：PDF 里矩阵公式块可能比题号高 30~40pt，
# 必须按 get_text("dict") 的实际 block 边界取（见每题上方注释）。
ITEMS = [
    ("I01", 1, 101.4, 163.1, ["-3", "-1", "1", "3"], "D", 1),
    ("I02", 1, 456.0, 551.5, ["-2", "-1", "1", "2"], "C", 5),
    ("I03", 2, 186.0, 262.0, ["-15", "-6", "6", "15"], "C", 9),
    ("I04", 2, 277.3, 391.3, ["1", "12", "3", "1/6"], "D", 10),
    ("I05", 2, 582.4, 663.0, ["a=1", "a=2", "a=1 或 a=-2", "a=-1 或 a=-2"], "C", 14),
    ("I06", 5, 245.5, 344.8, ["只有零解", "有无穷多组解", "有唯一的非零解", "无解"], "B", 40),
]

# 需要额外做「OCR 乱文 + 原图」对照的题
OCR_VARIANT = {"I01": "T01", "I03": "T02", "I06": "T03"}


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    doc = pymupdf.open(PDF)
    questions = []

    for qid, page_no, top_y, bottom_y, options, answer, orig in ITEMS:
        page = doc[page_no - 1]
        rect = pymupdf.Rect(20, top_y, page.rect.width - 20, bottom_y)
        pix = page.get_pixmap(clip=rect, matrix=pymupdf.Matrix(3, 3), alpha=False)
        png = os.path.join(OUT_DIR, f"{qid}.png")
        pix.save(png)
        rel = os.path.relpath(png, ROOT).replace("\\", "/")

        questions.append({
            "id": qid,
            "type": "single",
            "group": "image-vision",
            "title": f"请阅读图片中的第 {orig} 题并选出唯一正确的选项（题干与公式以图片为准）。",
            "options": options,
            "answer": answer,
            "image_files": [rel],
            "source": f"高职线性代数100题 扫描件 Q{orig}（文本层答案键）",
        })

        alias = OCR_VARIANT.get(qid)
        if alias:
            ocr_text = page.get_text("text", clip=rect).strip()
            ocr_text = " ".join(ocr_text.split())[:600]
            png2 = os.path.join(OUT_DIR, f"{alias}.png")
            pix.save(png2)
            rel2 = os.path.relpath(png2, ROOT).replace("\\", "/")
            questions.append({
                "id": alias,
                "type": "single",
                "group": "image-ocr-vision",
                "title": f"页面提取的文字如下（公式可能错乱）：{ocr_text}",
                "options": options,
                "answer": answer,
                "image_files": [rel2],
                "source": f"高职线性代数100题 扫描件 Q{orig}（OCR 乱文 + 原图）",
            })

    payload = {
        "name": "ocs-gateway-image-eval-20261110",
        "created": "2026-11-10",
        "notes": "图片公式题：题干与矩阵只出现在扫描图里，选项为文本；T 组额外附带 PDF 文本层的乱序 OCR 文字。",
        "questions": questions,
    }
    with open(OUT_JSON, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    print(f"生成 {len(questions)} 题 -> {OUT_JSON}")
    for q in questions:
        print(" ", q["id"], q["group"], q["image_files"][0], "answer", q["answer"])


if __name__ == "__main__":
    main()

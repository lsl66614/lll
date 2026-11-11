# 官方记录重复/矛盾 dry-run 报告

- 生成时间：2026-11-10 17:00:46
- 线上库快照：`C:\Users\刘帅良\Documents\Qoder\2026-11-08\62711a50\ocs-ai-gateway\data\_merged_bank.db`（sqlite backup，只读）
- 题库记录：943 条；官方答案非空：680 条

## 结论摘要

| 口径 | 含义 | 多记录组 | 答案矛盾组 |
|---|---|---|---|
| A 查询命中 | 同一次查询会同时命中（find_official） | 20 | 0 |
| B 统一写入 | align_official 的作用范围 | 21 | 0 |
| C 文字骨架 | 题干+选项去掉图片 URL 后相同 | 131 | 1（真同题 0 / 无法判断 1 / OCR 证明不同题 19） |

- 选项损坏（重复/空项）的官方非空记录：0 条

## A 查询命中口径 — 矛盾组 0 个

无。

## B 统一写入口径 — 矛盾组 0 个

无。

## C 文字骨架口径 — 文字相同、图片不同的组 20 个

- OCR 证明是不同公式（不同题，答案不同正常）：19 组
- OCR 证明公式相同、答案矛盾（真同题分叉）：0 组
- OCR 不足、无法判断：1 组

### 无法判断：缺少 OCR

### 1. [single] 设 http://edu-image.nosdn.127.net/_PhotoUploadUtils_75cfffda-ebdd-4d57-

- 答案语义分叉：http//edu-imagenosdn127net/photouploadutils811fead1-ef89-473d-b540-2d25eda58d71png可由http//edu-imagenosdn127net/photouploadutils75cfffda-ebdd-4d57-9033-3d7e1237d840png线性表示且表示式唯一 / 可由线性表示且表示式唯一
- 专业：线性代数 / 默认
- 图片指纹：(空)

| id | subject | answer | 语义 | via | 状态 | 更新时间 | 选项 |
|---|---|---|---|---|---|---|---|
| 2caf635b | 线性代数 | A | http//edu-imagenosdn127net/photouploadutils811fead1-ef89-473d-b540-2d25eda58d71png可由http//edu-imagenosdn127net/photouploadutils75cfffda-ebdd-4d57-9033-3d7e1237d840png线性表示且表示式唯一 | - | adopted | 11-10 16:54 | http://edu-image.nosdn.127.net/_PhotoUploadUtils_811fead1-ef89-473d-b540-2d25eda58d71.png 可由 http://edu-image.nosdn.127.net/_PhotoUploadUtil |
| 81476968 | 默认 | A | 可由线性表示且表示式唯一 | - | adopted | 11-09 01:12 | 可由 线性表示，且表示式唯一。 / 可由 线性表示，但表示式不唯一。 / 不一定能够由 线性表示。 / 一定与 中的某个向量成比例。 |

- OCR 2caf635b: alpha1alpha2ldotsalpham beta alpha1alpha2ldotsalpham beta β alpha1alph…
- OCR 81476968: NONE

## 执行说明（尚未执行）

- 本报告只读生成，线上库与答题记忆均未改动。
- A/B 口径的矛盾组 = 新代码 `align_official` 的修复对象：
  把组内全部官方记录统一成同一份「答案 + 选项顺序」（page 优先）。
- C 口径的组不会在查询时互相顶替（题干图片 URL 不同）。
  报告用 `data/image_text.json` 的 OCR 文本判断公式是否相同：
  OCR 不同 = 本来就是不同的题，答案不同正常，不需要处理；
  只有 OCR 证明公式相同而答案矛盾的组才需要人工确认。

# 答题盲测基准

这个目录存放盲测集和运行说明。盲测回答一个问题：**在一批没有为它写过题库、
缓存或记忆的题目上，网关首答能对多少。**

`sample.json` 只是冒烟样例，题目过于简单，不能引用它的准确率。

## 一条用例的格式

```json
{
  "id": "S01",
  "type": "single",
  "title": "题干",
  "options": ["选项 A 的内容", "选项 B 的内容"],
  "answer": "A",
  "source": "2026 秋 线性代数 期末",
  "subject": "线性代数",
  "context": "第三章 单元测验"
}
```

- `type` 支持 `single` / `multiple` / `judge` / `completion`，也接受 OCS 的别名。
- 选择题的 `answer` 写字母；多选写 `A#C` 或 `["A", "C"]`；判断题写 `正确`/`错误`；
  填空题写答案原文。
- `id` 必填且在文件内唯一。`source` 记录题目出处，报告会按来源拆分准确率。
- `images` 可选，格式与 `/query` 的 `images` 一致。

## 怎么建一批可信的盲测集

1. 选两门**没有在本网关答过题**的新课，或至少是题库、缓存、答题记忆里都没有的章节。
2. 从课程页抓 150~300 道题，保存题干、选项、题型。
3. 先人工核对标准答案，冻结成 JSON；核对完成前不要跑网关。
4. 跑之前清空或隔离题库/缓存/记忆，或者用影子实例（空 `data/` + 空题库）。
5. 跑完把报告和用例一起存档。下一次改匹配或提示词时，用同一批题复测。

用已有题库回放只能算回归测试，准确率会虚高。仓库里 `docs/ocs-upgrade-plan.md`
记录的 hold-out 也来自同一批快照，不能替代新课程盲测。

## 运行

```bash
# 先校验用例
uv run python scripts/run_benchmark.py benchmark/blind-2026-11.json --dry-run

# 跑网关
uv run python scripts/run_benchmark.py benchmark/blind-2026-11.json \
  --base-url http://127.0.0.1:8000 \
  --concurrency 4 \
  --out-dir reports

# 低于阈值时退出码 2，可接 CI 或脚本
uv run python scripts/run_benchmark.py benchmark/blind-2026-11.json --fail-under 0.9
```

有 `GATEWAY_TOKEN` 时通过环境变量传入，或加 `--token`。

## 报告怎么看

- **准确率（有效作答）** = 正确 / (正确 + 错误)。弃答、失败、空答案不计入分母。
- **覆盖率** = 有效作答 / 总题数。弃答多时准确率会好看，必须同时看覆盖率。
- **按题型**：填空题通常明显低于选择题，分开看才有意义。
- **按来源**：`official` / `bank` / `memory` / `ai` / `cache` 各自的正确率，用来判断
  该信哪一层。
- **延迟**：p50 / p95；上游 503 会显著拉高 p95。

## 注意

- 盲测不检查答案的「推理过程」，只比对最终答案；填空题沿用四档比对
  （精确、数学等价、数值容差、前缀），前缀档可能放宽个别题。
- 同一批题重复跑时，第二次会命中缓存，准确率不再代表首答。跑之前清缓存，
  或用影子实例。
- 报告不包含 API Key；用例里也不要写 Key。

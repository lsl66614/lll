# LEARNINGS

## 2026-11-10 — Math-Verify 在本机的三个硬约束

- **Pattern-Key:** deps/math-verify/windows-timeout-and-parsing
- **Category:** knowledge_gap
- **What happened:** 接入 Math-Verify 做填空题符号判分时连续踩坑：① Windows 上它默认用 `multiprocessing.Process` 实现超时，中文用户名路径下 spawn 直接 `OSError: [WinError 87]`；② 裸 `(x+1)^2` 解析为空，必须包成 `$...$`；③ 中文会被剥掉单位，`3.5米` 与 `3.5秒` 会被判成相等。
- **Do differently:** 调用时显式传 `parsing_timeout=None` / `timeout_seconds=None`；含 LaTeX 记号或 `^_{}` 的表达式自动包 `$...$`；含 CJK 的答案直接不走 Math-Verify，保留原有文本/数值比较。封装在 `app/mathcheck.py`，可选依赖缺失时静默降级。
- **Where:** `app/mathcheck.py`、`app/evaluation.py`（judge_completion）、`app/quality.py`（check_official）

## 2026-11-10 — 答题页隐藏答案被当成官方反馈

- **Pattern-Key:** ocs/snapshot/hidden-analysis-answers
- **Category:** correction
- **What happened:** OCS 答题页（examMode）的 `analysisInfo` 带 `f-dn` 隐藏类，其中 `tt2` 是占位值且整页错位；快照入口按「含正确答案字样」判定结果页，把隐藏值写进题库/记忆（0.98 trusted）。后续轮询又把已回滚的正确答案覆盖，冲突工作台里 AI 旧答案的 `conflict` 永远不清。
- **Do differently:** 判定答案块必须先看可见性（非 examMode 且 analysisInfo 无 f-dn），并让下游 `apply_feedback` 对不可见项直接跳过；「官方答案落定」时同步清同题机器来源的冲突标记。任何批量回灌先做跨文件一致性（≥2 份快照一致）与题库精确命中的双重闸门，单文件证据不动库。
- **Where:** `app/mooc_result.py`（parse_result_html / apply_feedback）、`app/main.py`（/api/snapshot、快照回灌）、`app/bank.py`（align_official）

## 2026-11-10 — provider 原地换模型后统计未清零导致新模型被永久排除

- **Pattern-Key:** ocs/stats/provider-model-slot
- **Category:** best_practice
- **What happened:** `_unit_ready` 按 provider id 的历史失败率过滤（`calls>=20 且 fails*2>calls`）。管理台原地换模型时 `stats.per` 不清零，旧模型的 21/21 失败记录让新模型永远进不了单元投票；被排除后又无法积累成功记录，形成死锁。
- **Do differently:** 统计槽按「id + model」识别，换模型自动清零，过滤只针对当前模型。已落 `Stats._slot` 与 `test_stats_reset_when_provider_model_changes`。
- **Where:** `app/main.py`（Stats._slot / success / failure）

## 2026-11-10 — 单元测验首答：答案归一化 + 独立模型共识

- **Pattern-Key:** ocs/unit-vote/canonical-consensus
- **Category:** best_practice
- **What happened:** 单元测验只能提交一次时，首答即终答。原来的投票按原始字符串计数（`1/2` vs `0.5` 算两票），且 `agree>=2` 允许同一模型两轮重复采样凑成「共识」；官方题 2 票即可覆盖官方。
- **Do differently:** 投票前用 `canonical_answer` 归一（填空数值/分数/百分数、判断对错、多选排序）；共识必须 ≥2 个**独立模型**（新增 `unit_models`）且 share≥0.5；首轮无共识时三个模型各再答一次（`unit_rounds=2`）；官方题只有全部模型一致才允许覆盖。图片题在单元模式下保留原图 + OCR 文本，避免三模型共用一份错误转写。
- **Where:** `app/quality.py`（canonical_answer / check_official）、`app/main.py`（_unit_vote / _unit_consensus / _unit_ask / unit 分支 / _official_check / _prepare_prompt）

## 2026-11-10 — 待确认题复核分支漏改共识口径（实测发现）

- **Pattern-Key:** ocs/unit-vote/pending-branch-consensus
- **Category:** correction
- **What happened:** 题库里状态为 `pending` 的题会走 `pending_local` 复核分支，该分支沿用了旧的 `unit_agree >= 2` 判定；同一模型在无共识时两轮各答一次就能凑出 2 票，`consensus` 被标成 `unit`，且返回的 `vote` 缺少 `models/share/rounds`。33 题实测中，冒烟测试产生的待确认题第二次作答正好走了这条路径。
- **Do differently:** 复核分支与首次作答分支共用同一口径：`unit_models >= 2 且 unit_share >= 0.5` 才算共识；`vote` 补齐 `models/share/rounds`。新增 `test_unit_pending_branch_requires_independent_consensus`。
- **Where:** `app/main.py`（pending_local 分支）、`tests/test_gateway.py`

## 2026-11-10 — 影子网关实测法（空题库 + 逐题重设 scene）

- **Pattern-Key:** ocs/eval/shadow-gateway
- **Category:** best_practice
- **What happened:** 需要在线上数据零污染的前提下测「首答准确率」。用 `OCS_DATA_DIR=<临时目录>` + `PORT=8010` + `GATEWAY_TOKEN=` 起影子实例，复制 `providers.json` 即可；`.env` 里已有变量不会被 `load_dotenv` 覆盖。`scene_state` 的 TTL 20 分钟且 `get()` 不续期，逐题重设 `POST /api/scene {"scene":"unit"}` 才能保证全程走单元投票。
- **Do differently:** 判分复用 `app.quality.canonical_answer`，与网关投票口径一致；上游 429 要单独统计（本次 glm-5.3-flash 7/9 次 429，投票实际只有 2 个模型参与），别把「2 模型 100%」写成「3 模型 100%」。
- **Where:** `tools/eval_run.py`、`tools/eval_questions_20261110.json`、`reports/eval-unit-*.md`

## 2026-11-11 — Orca 里的 Codex 对话既不是 terminal 也不是 worker

- **Pattern-Key:** orca/chat/supervision-path
- **Category:** knowledge_gap
- **What happened:** 用户说「orca 中那个 codex 对话」，我按 `orca terminal list` / `orchestration worker-list` 都为空，判断该会话不在 Orca，判断错了。实际依据在会话日志头部：`~/.codex/sessions/<日期>/rollout-*.jsonl` 的 `session_meta.payload.originator` 写着 `orca_desktop` —— Orca 的 agent chat 由 codex app-server 驱动，不占 terminal、也不是受监督 worker；Orca CLI 没有任何 chat 读取/发送命令（325 个命令里只有 `chat rename`）。
- **Do differently:** 判断一个 Codex 会话归谁管，先看 rollout 的 `session_meta.originator`，不要用 terminal/worker 列表反推。既有 chat 只能只读监督（解析 rollout jsonl）；要能 send/stop 必须用 `orca orchestration worker-start --agent codex` 另起受监督 worker，不能指望接管已有 chat。
- **Where:** `~/.codex/sessions/`、`orca orchestration`、`orca terminal list`

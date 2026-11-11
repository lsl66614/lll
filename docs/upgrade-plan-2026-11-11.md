# 批量答题与解析加固升级计划（2026-11-11）

> 依据：对 `LynnGuo666/ocsjs-ai-answer-service`、`amlkiller/llm_answerer`、
> `Trashwbin/MultiAI-Answer-cx` 三个仓库的源码逐文件阅读（本地副本
> `C:\temp\ocs\refs\`），以及本项目现有 45 个 py / 16,057 行的实测现状。
> 标注「实测」的为读代码或跑命令确认；标注「推断」的为设计判断。

## 0. 结论先行

| 优先级 | 事项 | 工期 | 需要改客户端 |
|---|---|---|---|
| P0 | 多题 JSON 容错解析（`app/batch_parse.py`） | 1.0 天 | 否 |
| P0 | 缓存低置信随机重问（`app/cache.py`） | 0.5 天 | 否 |
| P0 | 投票平票规则显式化 + `tie_break` 字段 | 0.5 天 | 否 |
| P1 | 批量提示词与批量调用（`_call_batch_one`） | 1.5 天 | 否 |
| P1 | `POST /api/batch/query`（answer / prefetch 双模式） | 2.0 天 | 否 |
| P1 | userscript 页面级预取（不接管官方流程） | 1.5 天 | 是（仅面板） |
| P2 | trace 批次分组（每题一条完整记录） | 1.0 天 | 否 |
| P2 | 文档、灰度开关、E2E 回归 | 0.5 天 | 否 |

单人总工期 **6.5~8 天**。若只有 3 天，只做 P0 三项 + 批量解析，收益/风险比最高。

**核心判断（推断）**：本项目真正的缺口不是"再多模型"，而是
**一次页面有 20 题时仍要发 20 次单题请求**。批量接口的最大收益来自
**预取（prefetch）**——userscript 扫当前页题目批量预解并写缓存，官方 OCS 脚本
随后逐题调用 `/ocs/search` 时直接命中缓存。这样**不修改官方答题流程**，
零兼容风险（依据：`app/main.py:2680` 的 `/ocs/search` 是逐题契约）。

## 1. 现状基线（实测）

- 单题入口：`GET /query`（`app/main.py:2632`）、`POST /query`（`:2648`）、
  `POST /ocs/search`（`:2680`，OCS 官方脚本契约，返回 `[[question, answer, extra]]`）。
- 统一调度：`_resolve`（`:2271`）→ `_singleflight` → `_resolve_core`（`:1273`）
  → `_resolve_core_impl`（`:1310`），阶段顺序 official → memory → bank → AI。
- 缓存：`app/cache.py`，SQLite，键含 `qtype/title/options/subject/context/image_hash`
  （`:59-69`），TTL 168h、填空 24h。**不含 question_type 的坑我们没有**
  （对照 `llm_answerer.py:90-93` 的 `md5(title|options)`）。
- 缓存读写点：`app/main.py:1974`（`cache.get`）、`:2127`（`cache.set`）。
- 多模型投票：`_unit_vote`（`:985`）→ `weighted_choice`（`app/quality.py:156`），
  已按历史准确率 + 置信度加权；票数已回传 `unit_votes/unit_share/unit_weights`（`:1097-1112`）。
- Trace：`TraceStore`（`app/trace.py:274`）为**单例当前 trace**，`begin`（`:285`）、
  `recent`（`:421`）；已有 `/trace` 页与 `/api/trace/recent`（`app/main.py:669`/`:677`）。
- 并发：`MAX_CONCURRENCY`（`app/config.py:107`，默认 3，上限 20）。
- 测试：20 个文件 / 6,904 行 / 498 用例；CI 用 `unittest discover`（`.github/workflows/test.yml`）。
- **不存在**：多题批量接口、批量提示词、批量 JSON 解析、缓存随机重问、批级 trace。

## 2. 目标与验收指标

以"一个 10 题的作业页"为基准场景：

| 指标 | 现状（实测口径） | 目标 |
|---|---|---|
| 客户端请求数 | 10 次 `/ocs/search` | 1 次 `/api/batch/query` + 0 次（全部命中缓存） |
| AI 调用次数 | 10~30 次（含 cross/复答） | ≤ 3 次（批量 2~3 片） |
| 缓存命中时延 | — | < 50 ms（SQLite 本地读） |
| 批量解析成功率 | 不适用 | ≥ 99%（12 个坏样本 + 真实回放） |
| 单题接口回归 | 498 用例全绿 | 不变，`/ocs/search` 响应结构零改动 |

## 3. Phase 0 — 基线与开关（0.5 天）

**改动**
- `app/config.py` 新增：
  - `BATCH_ENABLED`（默认 `0`）
  - `BATCH_MAX_ITEMS`（默认 `8`）
  - `BATCH_MAX_CHARS`（默认 `12000`，按题干+选项字符预算分片）
  - `BATCH_RATE_LIMIT`（默认 `10` 次/分钟/客户端）
  - `CACHE_RETRY_PROBABILITY`（默认 `0`，灰度后开 `0.1`）
  - `CACHE_RETRY_MIN_CONFIDENCE`（默认 `0.75`）
- `app/settings.py` 暴露同名开关到 `/api/settings`，供后台面板切换。

**验收**
- 不设环境变量时，所有新接口返回 `404`/`403`（功能关闭），旧用例 498/498 通过。
- 记录 `/query` 当前 p50/p95 作为对照基线（用 `scripts/` 下脚本或 20 次 curl）。

## 4. Phase 1 — 多题 JSON 容错解析（P0，1 天）

**新文件** `app/batch_parse.py`（不动 `app/parsing.py` 既有单题路径）

```python
def parse_answer_array(raw: str, expected: list[str]) -> tuple[dict[str, dict], list[str]]
    """返回 ({id: {answer, confidence}}, missing_ids)。"""
```

五级兜底（照抄 MultiAI 的思路，实现按 Python 重写）：
1. `json.loads` 直解；
2. 剥 ```json / ``` 围栏后直解；
3. 找第一个 `{` 与最后一个 `}` 之间切片再解（**平衡括号扫描**，不能像现有
   `_extract_answer_field`（`app/parsing.py:185`）那样用 `\{[^{}]*\}`，遇到数组/嵌套就失效）；
4. 修复常见错误后重试：尾逗号、单引号、中文引号、未加引号的 key；
5. 正则兜底：按 `"id"\s*:\s*"..."` 切块，块内抓 `answer`/`confidence`。

结构兼容：`{"answers":[...]}` / `{"data":[...]}` / 顶层数组 / `{"1":"A","2":"B"}` 映射。

答案归一化（**不重写**，复用现有能力）：
- 每题交给 `parse_answer_detail(qtype, text, options, strict=False)`（`app/parsing.py:402`）
  做题型校验，单选取字母、多选 `#` 连接、判断转 `正确/错误`；
- 数组答案 join `#`，`null`/布尔/数字转字符串，空串视为未答；
- 与 `canonical_answer`（`app/quality.py:234`）保持一致，避免批量和单题两套规范。

**测试** `tests/test_batch_parse.py`（≥ 14 例）
- 正常 JSON / 代码块包裹 / 前后废话 / 尾逗号 / 中文引号 / 缺 id / 重复 id /
  `null` 答案 / 数字答案 / 数组答案 / 被截断的 JSON / `{id: answer}` 映射 /
  完全非 JSON 的自然语言 / 部分题缺失。

**验收**
- `uv run python -m unittest tests.test_batch_parse -v` 全绿；
- 12 个真实坏样本（从三个参照项目 README/issue 与本地回放中取）解析成功率 100%；
- 不改变 `parse_answer_detail` 行为（`tests/test_gateway.py` 相关用例不变）。

## 5. Phase 2 — 批量提示词与批量调用（P1，1.5 天）

**`app/parsing.py` 新增** `build_batch_messages(items, context="")`

system 模板：

```
你是严谨的考试答题助手。下面是若干道题，请逐题作答。
只输出一个 JSON 对象，不要任何解释、注释或 markdown 围栏：
{"answers":[{"id":"题目ID","answer":"答案","confidence":0.9}]}
关键约束：
1. 必须回答全部题目，不得遗漏；
2. id 必须原样返回，不得改写、不得自增；
3. 单选题只回字母；多选题按字母顺序用 # 连接；判断题回 正确/错误；
4. 填空题多个空按顺序用 # 连接；
5. 无法确定时 answer 为空字符串，不要编造。
```

user：每题一段 `【id】题型：... 题干：... 选项：A. ...`，题间空行分隔。
附 1 个 2 题 few-shot（覆盖单选 + 多选）。

**`app/main.py` 新增** `_call_batch_one(provider, items)`
- 复用 `call_provider_detail`（`app/llm.py:249`）+ `asyncio.timeout`
  （超时按现有 `min(provider["timeout"], REQUEST_TIMEOUT_SECONDS)`）；
- 解析走 `parse_answer_array`，再把每题过 `parse_answer_detail`；
- **部分失败不整批丢弃**：返回 `(hits, missing, error)`，`missing` 的题回退到
  `_call_one`（`:2438`）逐题补答——保证批量路径永远不会比单题路径更差；
- 同一 provider 批请求串行（per-provider `asyncio.Lock`），避免触发上游 429。

**分片**
- 按 `BATCH_MAX_ITEMS` 与累计字符数 `BATCH_MAX_CHARS` 双阈值切分；
- 同一批内题型混杂时仍用同一提示词（题型写在每题里），不做题型分组——
  分组会让请求数回升，收益抵消。

**验收**
- 用一个 provider 跑 8 题混合题型，`hits=8`；
- 故意让模型返回坏 JSON（mock），`missing` 正确回退且最终 8/8 有答案；
- 单批 token 用量对比单题 8 次，下降 ≥ 50%（用 `last_usage()` 记录）。

## 6. Phase 3 — 批量接口 `POST /api/batch/query`（P1，2 天）

**请求**

```json
{
  "mode": "answer",
  "items": [
    {"id": "q1", "title": "...", "type": "single", "options": ["...", "..."],
     "images": [], "subject": "", "context": ""}
  ],
  "token": "..."
}
```

- `mode`: `answer`（缺题就解）| `prefetch`（只解缺的，全部写缓存，返回摘要）；
- 上限 30 题/请求；`title` ≤ `MAX_TITLE_LENGTH`（2000）；`options` ≤ 26 项；
- 鉴权：`require_token`（`app/main.py:533`）+ `BATCH_RATE_LIMIT`；
- `mode=prefetch` 时不返回答案正文，只返回 `cached/answered/failed` 计数，
  防止预取把答案直接暴露给页面（按需）。

**响应**

```json
{
  "code": 0,
  "data": {
    "results": [
      {"id": "q1", "answer": "A", "confidence": 0.93,
       "source": "cache|bank|memory|official|ai", "provider": "xxx",
       "elapsed_ms": 812, "error": ""}
    ],
    "stats": {"total": 10, "cached": 6, "answered": 4, "failed": 0, "elapsed_ms": 3120}
  }
}
```

**实现要点**
- **按 `id` 对齐，禁止按下标**。缺失的 id 显式返回 `error: "model_missing"`。
- 每题先走"便宜"路径：`cache.get`（`:1974` 同款键）→ bank（`_resolve_bank:1728`）
  → memory（`_resolve_memory:1683`）→ official（`_resolve_official:1553`）；
  只有剩下的才进 AI 批。
- 结果写回：`cache.set`（`:2127` 同款参数）+ `answer_memory.record`（`:2313`）
  + `failures.resolve`（`:2302`）。把 `_resolve`（`:2292-2333`）里这段记账逻辑抽成
  `_record_answer(...)`，单题/批量共用，**禁止复制一份**。
- 单飞：批内逐题仍走 `_singleflight`（`:2212`），键用 `_flight_key`（`:2170`），
  避免预取与官方 `/ocs/search` 同时解同一题。
- 降级：`BATCH_ENABLED=0` 时接口 404；批量整体异常时返回 200 + 每题
  `error`，不抛 500（客户端可逐题重试）。

**验收**
- 10 题（6 题已在缓存）→ `cached=6, answered=4`，AI 调用 ≤ 2 批；
- 并发两次相同批请求，单飞生效（AI 调用次数不翻倍）；
- 关闭开关后接口 404，旧流程不受影响；
- 新增 `tests/test_batch_api.py` ≥ 8 例（含鉴权、超限、空 items、重复 id、
  部分失败、prefetch 不泄露答案）。

## 7. Phase 4 — 客户端预取，不接管官方流程（P1，1.5 天）

**改** `userscript/ocs-assistant.user.js`（仅面板，49,855 字节）
- 监听题目列表页 DOM，收集当前页题目（复用现有 `qs/qsa` 工具，`:275-278`）；
- 顶部面板加按钮「本页预取 N 题」→ `POST /api/batch/query` `mode=prefetch`；
- 进度：`已缓存 6 / 共 10`，失败题列出原因 + 单题重试；
- **不修改** OCS 官方 `defaultAnswerWrapperHandler`，官方脚本继续逐题调
  `/ocs/search`，命中缓存后秒回。

**为什么这样做（推断）**
- 官方脚本是逐题契约（`app/main.py:2684-2703` 的 docstring 明确说明），
  替换它要改 OCS 配置，升级即失效；
- 预取对官方流程是"无感加速"，最坏情况只是预取失败，答题仍走原路径。

**验收**
- Playwright（`scripts/e2e_userscript.py`，`e2e` 可选依赖）跑一遍：
  预取 10 题 → 官方逐题请求平均耗时 < 80 ms（命中缓存）；
- 断网时预取失败，官方流程仍能单题求解。

## 8. Phase 5 — 缓存低置信随机重问（P0，0.5 天）

**改** `app/cache.py`

```python
def get_for_answer(self, key: str) -> dict | None
    # 命中后：source in ("ai","unit-vote") 且 confidence < CACHE_RETRY_MIN_CONFIDENCE
    # 且 random.random() < CACHE_RETRY_PROBABILITY → 返回 None（触发重解）
```

**调用点**：`app/main.py:1974` 改用 `get_for_answer`；重解结果照常 `cache.set` 覆盖。

**注意**
- 只对 AI 来源的低置信记录生效；bank/official 命中永不重问（那是权威答案）；
- 概率默认 0，灰度观察一周再开 0.1；
- 重问导致答案变化时，在 `answer_memory` 里记 `meta.retried=true`，便于回溯。

**验收**
- 单测：mock `random.random` 返回 0.05/0.5，验证"低置信被重问、高置信不重问"；
- 概率 0 时行为与现状完全一致（498 用例不回归）。

## 9. Phase 6 — 投票平票规则显式化（P0，0.5 天）

现状已优于参照项目：`weighted_choice`（`app/quality.py:156`）按历史准确率 +
置信度加权，票数也已回传（`unit_votes/unit_share/unit_weights`，`app/main.py:1097-1112`）。

**只补两点**
1. 平票且权重完全相等时：取 `confidence` 高者 → 再相同取 provider 列表顺序靠前者；
2. `data` 增 `tie_break` 字段：`"count" | "weight" | "confidence" | "order"`，前端可解释。

**不做**：MultiAI 的"权重模型平票多一票"（`src/content/main.ts:173-184`）——
我们的历史准确率加权粒度更细，硬编码偏好会引入人为偏差。

**验收**
- 构造 2:2 平票用例，`tie_break` 正确；单题接口返回结构仅**新增**字段，不删不改。

## 10. Phase 7 — trace 批次分组（P2，1 天）

**问题（实测）**：`TraceStore` 只有一个 `current()`（`app/trace.py:317`），
批量时无法区分每题。

**改动**
- `Trace` 增 `batch_id` / `item_id` 字段；`TraceStore.begin(..., batch_id=None)`
  允许批量场景追加多条 trace，而不是覆盖当前；
- `recent()`（`:421`）支持 `batch_id` 过滤；`/api/trace/recent`（`app/main.py:677`）透传；
- `/trace` 页（`app/static/trace.html`）按批次折叠，展开看每题：
  完整题干 + 选项、各模型原始返回、`reasoning`、投票明细、最终答案。

**验收**
- 一批 5 题 → `/api/trace/recent?batch_id=xxx` 返回 5 条，字段完整；
- 单题 trace 行为不变（`tests/test_trace.py` 145 行全绿）。

## 11. Phase 8 — 文档、灰度与回归（P2，0.5 天）

- 新增 `docs/api-batch.md`（请求/响应/错误码/限流）；
- `README.md` 加"批量与预取"一节；
- `docs/ocs-upgrade-plan.md` 末尾追加一条 2026-11-11 变更记录；
- 全量回归：`uv run python -m unittest discover -s tests`（498 + 新增 ≈ 530）；
- 灰度顺序：本地 → `BATCH_ENABLED=1` 观察 3 天 → 默认开；
- 指标看板：`/api/stats/http`（`app/main.py:2795`）增加 `batch_requests/batch_ai_calls`。

## 12. 风险与对策

| 风险 | 概率 | 影响 | 对策 |
|---|---|---|---|
| 模型把答案串题（id 对不上） | 中 | 高 | id 强约束 + 缺失即回退单题 + 逐题 `parse_answer_detail` 校验 |
| 长题干撑爆上下文 | 中 | 中 | `BATCH_MAX_CHARS` 字符预算动态分片 |
| 上游 429（批请求太密） | 中 | 中 | per-provider 串行 + 现有信号量（`:2370`）+ 冷却（`_in_cooldown:950`） |
| 预取把页面搞卡 | 低 | 低 | 只在用户点按钮时触发，不自动全量预取 |
| 缓存重问把好答案换坏 | 低 | 中 | 仅低置信 + 权威来源豁免 + 概率灰度 |
| trace 内存增长 | 低 | 低 | `MAX_TRACES` 现有上限，批量按批淘汰 |

## 13. 明确不做

- **不做扩展端直连多模型**（MultiAI 模式）：我们是网关，直连会泄露 key、丢缓存与题库。
- **不做服务端密文字体解码**（`decoder.ts`）：应在 userscript 页面侧做，独立小任务。
- **不做单模型自评置信度**（`confidence.py`）：多模型一致性 + 历史准确率已是更好的信号，
  自评翻倍成本且校准差。
- **不改** `/ocs/search` 的返回结构：官方脚本契约，改动即破坏兼容。

## 14. 排期（单人，工作日）

| 天 | 内容 |
|---|---|
| D1 | Phase 0 + Phase 5 + Phase 6（全后端，无客户端改动） |
| D2-D3 | Phase 1（容错解析 + 14 用例） |
| D4-D5 | Phase 2（批量提示词 + `_call_batch_one` + 降级） |
| D6-D7 | Phase 3（接口 + 记账抽取 + 8 用例） |
| D8 | Phase 4（userscript 预取）+ Phase 7/8 收尾 |

**首个可交付里程碑（D3 结束）**：容错解析 + 缓存重问 + 平票显式化上线，
不改客户端、不改官方流程，风险最低。

## 15. D1 执行记录（2026-11-11，已完成）

### 已落地

| 项 | 文件 | 关键点 |
|---|---|---|
| Phase 0 开关 | `app/config.py:56-68` | `BATCH_ENABLED/BATCH_MAX_ITEMS/BATCH_MAX_CHARS/BATCH_RATE_LIMIT`、`CACHE_RETRY_PROBABILITY/CACHE_RETRY_MIN_CONFIDENCE` |
| 设置项 | `app/settings.py:12-52` | `batch_enabled`、`cache_retry_probability`、`cache_retry_min_confidence`（0~1 夹取） |
| 缓存列 | `app/cache.py:45` | `answers` 表自动 `ALTER TABLE` 补 `source` / `confidence`，旧库无需迁移脚本 |
| 缓存重问 | `app/cache.py:78-158` | `lookup_for_answer()` 返回 `(row, retried)`；`get_for_answer()` 简化封装；仅 `ai`/`unit-vote` 且低置信才重问 |
| 重问接入 | `app/main.py:1979-1996` | 命中被拒时 trace 记 `cache`/`retry`，`stats.cache_retry += 1` |
| 缓存写入 | `app/main.py:2147-2148` | 写入时带 `source="ai"` + 本次 `confidence` |
| 平票规则 | `app/quality.py:148-215` | `VoteResult.tie_break`：`weight → count → confidence → order` |
| 返回字段 | `app/main.py:1108`、`:1915`、`:1961`、`:2173` | `unit_tie_break`、`tie_break`（仅新增，不删旧字段） |
| 统计 | `app/main.py:215` 及 `_load/_save` | `cache_retry` 计数持久化到 `stats.json` |

### 测试（实测）

- 新增 `tests/test_cache_retry.py` 8 例：低置信重问、高置信保留、权威来源豁免、
  旧行（无 source）不重问、概率 0 关闭、未命中、简化封装、重问后覆盖。
- `tests/test_quality.py` 增 4 例：`weight/count/confidence/order` 四种平票依据。
- 全量：`python -m unittest discover -s tests` → **510 tests OK**（原 498 + 12）。
- 运行时抽查：`Settings().all()` 三项新键默认值为 `0.0 / 0.75 / False`；
  `Stats.bump("cache_retry")` 正常自增。

### 行为兼容

- 默认 `CACHE_RETRY_PROBABILITY=0`，线上行为与改动前完全一致；
- 旧缓存行 `source` 为 NULL → 归为 `""` → 永不重问，不会因为升级而把老答案全部换掉；
- `/ocs/search`、`/query` 响应只**新增** `tie_break` 字段，客户端可忽略。

### 待办（D2 起）

- 灰度：`CACHE_RETRY_PROBABILITY=0.1` 观察 `stats.cache_retry` 与答案变化；
- 进入 D2-D3：`app/batch_parse.py` + `tests/test_batch_parse.py`。

## 16. D2-D3 执行记录（2026-11-11，已完成）

### 已落地

| 项 | 文件 | 说明 |
|---|---|---|
| 容错解析模块 | `app/batch_parse.py`（385 行） | `parse_answer_array(raw, expected)` → `({id: {answer, confidence}}, missing)` |
| 5 级兜底 | `app/batch_parse.py:183-211`、`:337-384` | 候选文本按序生成：直解 → 剥围栏 → 平衡括号切片 → 修复/补括号 → 正则抓取 |
| 结构兼容 | `:220-247` | `answers/data/results/items`、顶层数组、`{id: answer}` 映射、单对象 |
| 值归一化 | `:258-302` | 数组 `#` 连接、`null`→未答、布尔→`true/false`、`1.0`→`1`、"85%"→0.85 |
| 裸 key 修复 | `:130-181` | 逐字符扫描，避开字符串内容；填空题答案 `x{a:b}` 不会被改坏 |
| 截断兜底 | `:111-117` | 退到最后一个完整 `}` 再补括号，保住已输出的题 |
| 单测 | `tests/test_batch_parse.py`（193 行，26 例） | 覆盖围栏、废话、尾逗号、中文/单引号、裸 key、截断、重复 id、别名 key、纯垃圾等 |

### 关键契约

- **按 id 对齐，不按下标**；`expected` 之外的 id 直接丢弃，缺失/空答案的 id 进 `missing`。
- 空答案仍保留在 results 里（`answer=""`），同时计入 `missing`，调用方只补问缺失。
- 重复 id 以**最后一条**为准。
- 正则兜底只认显式 id/answer 键；不做「按顺序硬套 expected」，避免串题。

### 测试（实测）

- `python -m unittest tests.test_batch_parse -v` → **26 tests OK**。
- 串联验证：`parse_answer_array` + `parse_answer_detail` 四题型
  （单选 `b`→`B`、多选 `["A","C"]`→`A#C`、判断 `true`→`正确`、填空 `12`→`12`）全部正确。
- 全量：`python -m unittest discover -s tests` → **536 tests OK**（510 + 26）。

### 待办（D4-D5）

- `build_batch_messages()`：批量提示词 + 2 题 few-shot；
- `_call_batch_one()`：部分失败回退 `_call_one`，per-provider 串行；
- 分片：`BATCH_MAX_ITEMS` + `BATCH_MAX_CHARS` 双阈值。

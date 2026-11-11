# 批量答题接口 `POST /api/batch/query`

2026-11-11 加入。一次请求把整页题目交给网关：先走题库 / 答题记忆 / 答案缓存，
剩下的才按字符预算分片交给 AI，结果按 `id` 对齐返回，命中部分直接写回答案缓存。
油猴脚本点「本页预取 N 题」走的就是这里（`mode=prefetch`）。

## 1. 开关与鉴权

| 项 | 说明 |
|---|---|
| `BATCH_ENABLED` | 默认 `0`（关闭）。关闭时接口返回 **404**（`code=40401`） |
| 管理台开关 | 「答题策略 → 批量答题」与 `.env` 是「或」关系，任一开启即生效 |
| 口令 | 与其它上报接口共用 `GATEWAY_TOKEN`；请求头 `X-Gateway-Token` 或 body 里的 `token` |
| 限流 | 每个客户端 IP 每分钟 `BATCH_RATE_LIMIT` 次（默认 10，`0` 表示不限），超限 **429** |

`.env` 相关项：

```ini
BATCH_ENABLED=1          # 打开批量接口
BATCH_MAX_ITEMS=8        # 单次 AI 请求最多几题（1~30）
BATCH_MAX_CHARS=12000    # 单次 AI 请求的题干字符预算，超出自动分片
BATCH_RATE_LIMIT=10      # 每分钟请求数上限
```

## 2. 请求

```http
POST /api/batch/query
Content-Type: application/json
X-Gateway-Token: <token>

{
  "mode": "answer",
  "items": [
    {
      "id": "q1",
      "type": "single",
      "title": "下列关于进程与线程的说法，正确的是（ ）",
      "options": ["进程是资源分配的基本单位", "线程拥有独立的地址空间"],
      "subject": "操作系统",
      "context": "第三章 进程管理"
    }
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `mode` | 否 | `answer`（默认，返回答案）或 `prefetch`（只预热缓存，不返回答案） |
| `items` | 是 | 1~30 道题，`id` 必填且不能重复 |
| `items[].id` | 是 | 调用方自定义，响应按它对齐；建议用页面上稳定的题号 |
| `items[].title` | 是 | 题干，按 `MAX_TITLE_LENGTH` 截断 |
| `items[].type` | 是 | 题型，支持 `single/multi/judge/fill/image`、`单选/多选/判断/填空/图片` 等别名 |
| `items[].options` | 否 | 选项文本数组；图片选项请走单题 `/query` |
| `items[].images` | 否 | 题干图片（URL 或 data URL）。**带图片的题不批量**，逐题走完整单题链路 |
| `items[].subject` | 否 | 专业/课程，决定查哪个题库 |
| `items[].context` | 否 | 课程上下文，参与提示词与缓存 key |

## 3. 响应（`mode=answer`）

```json
{
  "code": 0,
  "data": {
    "mode": "answer",
    "results": [
      {
        "id": "q1",
        "answer": "A",
        "confidence": 0.88,
        "source": "ai",
        "provider": "glm-5.3-flash",
        "cached": false,
        "elapsed_ms": 1520,
        "error": ""
      }
    ],
    "stats": {
      "total": 3,
      "cached": 2,
      "answered": 1,
      "failed": 0,
      "ai_calls": 1,
      "batch_id": "b1762xxxxxxxxxxx",
      "elapsed_ms": 1680
    }
  }
}
```

- `results` **顺序与请求一致**，长度等于 `items` 长度。
- `source` 取值：`bank`（本地题库）、`memory`（答题记忆）、`cache`（答案缓存）、
  `ai`（本次 AI 作答）、`official`（图片题走单题链路时的官方答案）。
- 单题没答出来时返回 `answer=""`、`error="no_answer"`，整批仍是 **200**。
- `stats.cached` 是便宜路径命中数，`answered` 是本次 AI 新答出的题数，
  `failed` 是最终没答案的题数，`ai_calls` 是实际发出去的批量 AI 请求数。
- `stats.batch_id` 对应 `/trace` 页的批次分组，可直接 `GET /api/trace/recent?batch_id=...`。

## 4. 响应（`mode=prefetch`）

`results` 被裁剪成 `{id, ok, cached, source, error}`——**不返回答案**，只告诉你哪些题已经
进了缓存。之后官方脚本逐题请求 `/ocs/search` 时命中缓存即可秒回。

```json
{"code": 0, "data": {"mode": "prefetch",
  "results": [{"id": "q1", "ok": true, "cached": false, "source": "ai", "error": ""}],
  "stats": {"total": 1, "cached": 0, "answered": 1, "failed": 0, "ai_calls": 1,
            "batch_id": "b1762xxxxxxxxxxx", "elapsed_ms": 1500}}}
```

## 5. 错误码

| HTTP | `code` | 场景 |
|---|---|---|
| 404 | `40401` | `BATCH_ENABLED=0` 且管理台开关也关着 |
| 400 | `40000` | 请求体不是 JSON 对象 |
| 400 | `40002` | `mode` 不是 `answer` / `prefetch` |
| 400 | `40003` | `items` 为空、超 30 题、`id` 重复、缺 `title`、题型无法识别 |
| 401 | — | `GATEWAY_TOKEN` 不匹配（FastAPI 默认错误体） |
| 429 | `42901` | 超过 `BATCH_RATE_LIMIT` |

## 6. 与单题链路的关系

- 便宜路径优先级与单题完全一致：题库 `find_exact` → 答题记忆 `recall` → 缓存
  `lookup_for_answer`；批量只负责「剩下的题」。
- 批量**不查官方答案**：官方校验仍是单题链路的前置步骤，所以批量写进缓存的 AI 答案
  不会挡住下一次单题请求里的官方答案。
- 批量结果写回缓存时带 `source="ai"` 和本次 `confidence`，供缓存低置信重问使用。
- 图片题、超出 30 题的请求不进批量，回退单题完整链路，保证不比单题差。
- 没有按题加单飞锁：预取与单题同时解同一题可能重复调用一次 AI，只影响成本，不影响正确性。

## 7. 客户端示例

油猴脚本 `tools/ocs-assistant.user.js`（v0.2.2 起）面板里的「本页预取 N 题」：

```js
request('POST', '/api/batch/query', { mode: 'prefetch', items: items }, { timeout: 90000 })
  .then(function (result) {
    var stats = result.data.data.stats;
    // 已缓存 stats.cached / 共 stats.total，本次 AI 补答 stats.answered
  });
```

最多取当前页前 30 题（与服务端上限对齐）；按钮不接管 OCS 官方 handler，预取失败时
官方逐题流程照常工作。

# Errors

Command failures and integration errors.

---

## [ERR-20261110-OC2] bash_failure

**Logged**: 2026-11-10T09:05:39.624Z
**Priority**: medium
**Status**: pending
**Area**: config

### Summary
Auto-detected 1 error signature in a `Bash` tool result.

### Error
```
- `runtime.type-error` — except (TypeError, ValueError):
```

### Context
- Detected by the self-improvement hook on `PostToolUse` (real-time, at the moment of failure)
- Tool: `Bash`
- Working directory: `C:\Users\刘帅良\Documents\Qoder\2026-11-08\62711a50\ocs-ai-gateway`
- Session: `b33b84e9-d102-4dc0-9ea0-d39daab317f0`
- Excerpts are truncated and redacted; check the transcript for full context

### Suggested Fix
Triage this entry. If the failure was real and non-obvious, keep it and fill in the actual fix; if it was transient or already handled, mark it resolved. Before keeping it, grep for the Pattern-Key(s) above and fold recurrences into the existing entry (bump `Recurrence-Count`) instead of duplicating.

### Metadata
- Source: posttooluse-error-sweep
- Reproducible: unknown
- Pattern-Key: runtime.type-error

---

## [ERR-20261110-VSX] bash_failure

**Logged**: 2026-11-10T09:15:02.367Z
**Priority**: medium
**Status**: pending
**Area**: config

### Summary
Auto-detected 2 error signatures in a `Bash` tool result.

### Error
```
- `runtime.python-exception` — Traceback (most recent call last):
- `deps.module-not-found` — ModuleNotFoundError: No module named 'pydantic_core.core_schema'
```

### Context
- Detected by the self-improvement hook on `PostToolUse` (real-time, at the moment of failure)
- Tool: `Bash`
- Working directory: `C:\Users\刘帅良\Documents\Qoder\2026-11-08\62711a50\ocs-ai-gateway`
- Session: `b33b84e9-d102-4dc0-9ea0-d39daab317f0`
- Excerpts are truncated and redacted; check the transcript for full context

### Suggested Fix
Triage this entry. If the failure was real and non-obvious, keep it and fill in the actual fix; if it was transient or already handled, mark it resolved. Before keeping it, grep for the Pattern-Key(s) above and fold recurrences into the existing entry (bump `Recurrence-Count`) instead of duplicating.

### Metadata
- Source: posttooluse-error-sweep
- Reproducible: unknown
- Pattern-Key: runtime.python-exception
- Pattern-Key: deps.module-not-found

---

## [ERR-20261110-1R9] bash_failure

**Logged**: 2026-11-10T09:18:30.639Z
**Priority**: medium
**Status**: pending
**Area**: config

### Summary
Auto-detected 1 error signature in a `Bash` tool result.

### Error
```
- `deps.module-not-found` — ModuleNotFoundError: No module named 'dotenv'
```

### Context
- Detected by the self-improvement hook on `PostToolUse` (real-time, at the moment of failure)
- Tool: `Bash`
- Working directory: `C:\Users\刘帅良\Documents\Qoder\2026-11-08\62711a50\ocs-ai-gateway`
- Session: `b33b84e9-d102-4dc0-9ea0-d39daab317f0`
- Excerpts are truncated and redacted; check the transcript for full context

### Suggested Fix
Triage this entry. If the failure was real and non-obvious, keep it and fill in the actual fix; if it was transient or already handled, mark it resolved. Before keeping it, grep for the Pattern-Key(s) above and fold recurrences into the existing entry (bump `Recurrence-Count`) instead of duplicating.

### Metadata
- Source: posttooluse-error-sweep
- Reproducible: unknown
- Pattern-Key: deps.module-not-found

---

## [ERR-20261110-X2J] bash_failure

**Logged**: 2026-11-10T09:19:14.396Z
**Priority**: medium
**Status**: pending
**Area**: config

### Summary
Auto-detected 5 error signatures in a `Bash` tool result.

### Error
```
- `deps.module-not-found` — FAIL dotenv ModuleNotFoundError No module named 'dotenv'
- `deps.module-not-found` — FAIL numpy ModuleNotFoundError No module named 'numpy'
- `deps.module-not-found` — FAIL onnxruntime ModuleNotFoundError No module named 'onnxruntime'
- `deps.module-not-found` — FAIL pydantic_settings ModuleNotFoundError No module named 'dotenv'
- `deps.module-not-found` — FAIL multipart ModuleNotFoundError No module named 'multipart'
```

### Context
- Detected by the self-improvement hook on `PostToolUse` (real-time, at the moment of failure)
- Tool: `Bash`
- Working directory: `C:\Users\刘帅良\Documents\Qoder\2026-11-08\62711a50\ocs-ai-gateway`
- Session: `b33b84e9-d102-4dc0-9ea0-d39daab317f0`
- Excerpts are truncated and redacted; check the transcript for full context

### Suggested Fix
Triage this entry. If the failure was real and non-obvious, keep it and fill in the actual fix; if it was transient or already handled, mark it resolved. Before keeping it, grep for the Pattern-Key(s) above and fold recurrences into the existing entry (bump `Recurrence-Count`) instead of duplicating.

### Metadata
- Source: posttooluse-error-sweep
- Reproducible: unknown
- Pattern-Key: deps.module-not-found

---

## [ERR-20261110-UWI] bash_failure

**Logged**: 2026-11-10T10:20:02.380Z
**Priority**: medium
**Status**: pending
**Area**: config

### Summary
Auto-detected 1 error signature in a `Bash` tool result.

### Error
```
- `shell.nonzero-exit` — [exited with code 127]
```

### Context
- Detected by the self-improvement hook on `PostToolUse` (real-time, at the moment of failure)
- Tool: `Bash`
- Working directory: `C:\Users\刘帅良\Documents\Qoder\2026-11-08\62711a50\ocs-ai-gateway`
- Session: `b33b84e9-d102-4dc0-9ea0-d39daab317f0`
- Excerpts are truncated and redacted; check the transcript for full context

### Suggested Fix
Triage this entry. If the failure was real and non-obvious, keep it and fill in the actual fix; if it was transient or already handled, mark it resolved. Before keeping it, grep for the Pattern-Key(s) above and fold recurrences into the existing entry (bump `Recurrence-Count`) instead of duplicating.

### Metadata
- Source: posttooluse-error-sweep
- Reproducible: unknown
- Pattern-Key: shell.nonzero-exit

---

## [ERR-20261110-A28] bash_failure

**Logged**: 2026-11-10T11:21:44.527Z
**Priority**: medium
**Status**: pending
**Area**: config

### Summary
Auto-detected 2 error signatures in a `Bash` tool result.

### Error
```
- `vcs.fatal-error` — 2026-11-10T11:17:37.247505Z ERROR rmcp::transport::worker: worker quit with fatal: Transport channel closed, when AuthRequired(AuthRequiredError { www_authenticate_header: "Bearer [REDACTED]\"OAuth\",…
- `shell.nonzero-exit` — [exited with code 1]
```

### Context
- Detected by the self-improvement hook on `PostToolUse` (real-time, at the moment of failure)
- Tool: `Bash`
- Working directory: `C:\Users\刘帅良\Documents\Qoder\2026-11-08\62711a50\ocs-ai-gateway`
- Session: `b33b84e9-d102-4dc0-9ea0-d39daab317f0`
- Excerpts are truncated and redacted; check the transcript for full context

### Suggested Fix
Triage this entry. If the failure was real and non-obvious, keep it and fill in the actual fix; if it was transient or already handled, mark it resolved. Before keeping it, grep for the Pattern-Key(s) above and fold recurrences into the existing entry (bump `Recurrence-Count`) instead of duplicating.

### Metadata
- Source: posttooluse-error-sweep
- Reproducible: unknown
- Pattern-Key: vcs.fatal-error
- Pattern-Key: shell.nonzero-exit

---

## [ERR-20261110-F4I] bash_failure

**Logged**: 2026-11-10T11:22:39.439Z
**Priority**: medium
**Status**: pending
**Area**: config

### Summary
Auto-detected 1 error signature in a `Bash` tool result.

### Error
```
- `runtime.type-error` — - 触发场景：`source` 为 `"auto"`/`"page"` 等非 `"manual"` 值且 `memory_only=False` 时，函数走完校验、甚至已经执行了 `bank.upsert_calibrated` 落库，然后隐式返回 `None`。调用方若 `res["ok"]` 直接 `TypeError`；若做了 None 判空，则会把「已成功写入」当成失败上报——两种情况都是…
```

### Context
- Detected by the self-improvement hook on `PostToolUse` (real-time, at the moment of failure)
- Tool: `Bash`
- Working directory: `C:\Users\刘帅良\Documents\Qoder\2026-11-08\62711a50\ocs-ai-gateway`
- Session: `b33b84e9-d102-4dc0-9ea0-d39daab317f0`
- Excerpts are truncated and redacted; check the transcript for full context

### Suggested Fix
Triage this entry. If the failure was real and non-obvious, keep it and fill in the actual fix; if it was transient or already handled, mark it resolved. Before keeping it, grep for the Pattern-Key(s) above and fold recurrences into the existing entry (bump `Recurrence-Count`) instead of duplicating.

### Metadata
- Source: posttooluse-error-sweep
- Reproducible: unknown
- Pattern-Key: runtime.type-error

---

## [ERR-20261110-LCB] bash_failure

**Logged**: 2026-11-10T12:47:23.930Z
**Priority**: medium
**Status**: pending
**Area**: config

### Summary
Auto-detected 1 error signature in a `Bash` tool result.

### Error
```
- `runtime.type-error` — except (OSError, ValueError, TypeError):
```

### Context
- Detected by the self-improvement hook on `PostToolUse` (real-time, at the moment of failure)
- Tool: `Bash`
- Working directory: `C:\Users\刘帅良\Documents\Qoder\2026-11-08\62711a50\ocs-ai-gateway`
- Session: `e4aa9ed1-c799-4628-84d3-839586c8500c`
- Excerpts are truncated and redacted; check the transcript for full context

### Suggested Fix
Triage this entry. If the failure was real and non-obvious, keep it and fill in the actual fix; if it was transient or already handled, mark it resolved. Before keeping it, grep for the Pattern-Key(s) above and fold recurrences into the existing entry (bump `Recurrence-Count`) instead of duplicating.

### Metadata
- Source: posttooluse-error-sweep
- Reproducible: unknown
- Pattern-Key: runtime.type-error

---

## [ERR-20261110-7P9] bash_failure

**Logged**: 2026-11-10T13:03:17.048Z
**Priority**: medium
**Status**: pending
**Area**: config

### Summary
Auto-detected 1 error signature in a `Bash` tool result.

### Error
```
- `runtime.type-error` — except (ValueError, ZeroDivisionError, TypeError):
```

### Context
- Detected by the self-improvement hook on `PostToolUse` (real-time, at the moment of failure)
- Tool: `Bash`
- Working directory: `C:\Users\刘帅良\Documents\Qoder\2026-11-08\62711a50\ocs-ai-gateway`
- Session: `e4aa9ed1-c799-4628-84d3-839586c8500c`
- Excerpts are truncated and redacted; check the transcript for full context

### Suggested Fix
Triage this entry. If the failure was real and non-obvious, keep it and fill in the actual fix; if it was transient or already handled, mark it resolved. Before keeping it, grep for the Pattern-Key(s) above and fold recurrences into the existing entry (bump `Recurrence-Count`) instead of duplicating.

### Metadata
- Source: posttooluse-error-sweep
- Reproducible: unknown
- Pattern-Key: runtime.type-error

---

## [ERR-20261110-QC0] mcp__tavily__tavily_search_failure

**Logged**: 2026-11-10T13:30:26.455Z
**Priority**: medium
**Status**: pending
**Area**: config

### Summary
Auto-detected 1 error signature in a `mcp__tavily__tavily_search` tool result.

### Error
```
- `shell.nonzero-exit` — (4) non-zero integer
```

### Context
- Detected by the self-improvement hook on `PostToolUse` (real-time, at the moment of failure)
- Tool: `mcp__tavily__tavily_search`
- Working directory: `C:\Users\刘帅良\Documents\Qoder\2026-11-08\62711a50\ocs-ai-gateway`
- Session: `e4aa9ed1-c799-4628-84d3-839586c8500c`
- Excerpts are truncated and redacted; check the transcript for full context

### Suggested Fix
Triage this entry. If the failure was real and non-obvious, keep it and fill in the actual fix; if it was transient or already handled, mark it resolved. Before keeping it, grep for the Pattern-Key(s) above and fold recurrences into the existing entry (bump `Recurrence-Count`) instead of duplicating.

### Metadata
- Source: posttooluse-error-sweep
- Reproducible: unknown
- Pattern-Key: shell.nonzero-exit

---

## [ERR-20261110-2CS] bash_failure

**Logged**: 2026-11-10T13:55:09.471Z
**Priority**: medium
**Status**: pending
**Area**: config

### Summary
Auto-detected 5 error signatures in a `Bash` tool result.

### Error
```
- `deps.module-not-found` — fitz NO ModuleNotFoundError
- `deps.module-not-found` — pymupdf NO ModuleNotFoundError
- `deps.module-not-found` — pypdf NO ModuleNotFoundError
- `deps.module-not-found` — pdfplumber NO ModuleNotFoundError
- `deps.module-not-found` — pdf2image NO ModuleNotFoundError
```

### Context
- Detected by the self-improvement hook on `PostToolUse` (real-time, at the moment of failure)
- Tool: `Bash`
- Working directory: `C:\Users\刘帅良\Documents\Qoder\2026-11-08\62711a50\ocs-ai-gateway`
- Session: `e4aa9ed1-c799-4628-84d3-839586c8500c`
- Excerpts are truncated and redacted; check the transcript for full context

### Suggested Fix
Triage this entry. If the failure was real and non-obvious, keep it and fill in the actual fix; if it was transient or already handled, mark it resolved. Before keeping it, grep for the Pattern-Key(s) above and fold recurrences into the existing entry (bump `Recurrence-Count`) instead of duplicating.

### Metadata
- Source: posttooluse-error-sweep
- Reproducible: unknown
- Pattern-Key: deps.module-not-found

---

## [ERR-20261110-Y6P] mcp__tavily__tavily_search_failure

**Logged**: 2026-11-10T14:17:07.626Z
**Priority**: medium
**Status**: pending
**Area**: config

### Summary
Auto-detected 1 error signature in a `mcp__tavily__tavily_search` tool result.

### Error
```
- `deps.module-not-found` — Content: import re import signal from typing import Dict, List, Optional from importlib.metadata import version import datasets from lm_eval.utils import eval_logger try: import antlr4 import sympy fr…
```

### Context
- Detected by the self-improvement hook on `PostToolUse` (real-time, at the moment of failure)
- Tool: `mcp__tavily__tavily_search`
- Working directory: `C:\Users\刘帅良\Documents\Qoder\2026-11-08\62711a50\ocs-ai-gateway`
- Session: `e4aa9ed1-c799-4628-84d3-839586c8500c`
- Excerpts are truncated and redacted; check the transcript for full context

### Suggested Fix
Triage this entry. If the failure was real and non-obvious, keep it and fill in the actual fix; if it was transient or already handled, mark it resolved. Before keeping it, grep for the Pattern-Key(s) above and fold recurrences into the existing entry (bump `Recurrence-Count`) instead of duplicating.

### Metadata
- Source: posttooluse-error-sweep
- Reproducible: unknown
- Pattern-Key: deps.module-not-found

---

## [ERR-20261110-MKJ] bash_failure

**Logged**: 2026-11-10T14:44:25.348Z
**Priority**: medium
**Status**: resolved
**Area**: config
**Resolution**: 2026-11-10 已把 `math-verify` 加入主依赖并安装到 `.venv`（连带 sympy）；`app/mathcheck.py` 在依赖缺失时静默降级，不再抛 ModuleNotFoundError。

### Summary
Auto-detected 2 error signatures in a `Bash` tool result.

### Error
```
- `deps.module-not-found` — ModuleNotFoundError: No module named 'sympy'
- `deps.module-not-found` — ModuleNotFoundError: No module named 'math_verify'
```

### Context
- Detected by the self-improvement hook on `PostToolUse` (real-time, at the moment of failure)
- Tool: `Bash`
- Working directory: `C:\Users\刘帅良\Documents\Qoder\2026-11-08\62711a50\ocs-ai-gateway`
- Session: `e4aa9ed1-c799-4628-84d3-839586c8500c`
- Excerpts are truncated and redacted; check the transcript for full context

### Suggested Fix
Triage this entry. If the failure was real and non-obvious, keep it and fill in the actual fix; if it was transient or already handled, mark it resolved. Before keeping it, grep for the Pattern-Key(s) above and fold recurrences into the existing entry (bump `Recurrence-Count`) instead of duplicating.

### Metadata
- Source: posttooluse-error-sweep
- Reproducible: unknown
- Pattern-Key: deps.module-not-found

---

## [ERR-20261110-JU0] bash_failure

**Logged**: 2026-11-10T14:53:05.949Z
**Priority**: medium
**Status**: pending
**Area**: config

### Summary
Auto-detected 1 error signature in a `Bash` tool result.

### Error
```
- `vcs.fatal-error` — fatal: path 'app/evaluation.py' exists on disk, but not in 'HEAD'
```

### Context
- Detected by the self-improvement hook on `PostToolUse` (real-time, at the moment of failure)
- Tool: `Bash`
- Working directory: `C:\Users\刘帅良\Documents\Qoder\2026-11-08\62711a50\ocs-ai-gateway`
- Session: `e4aa9ed1-c799-4628-84d3-839586c8500c`
- Excerpts are truncated and redacted; check the transcript for full context

### Suggested Fix
Triage this entry. If the failure was real and non-obvious, keep it and fill in the actual fix; if it was transient or already handled, mark it resolved. Before keeping it, grep for the Pattern-Key(s) above and fold recurrences into the existing entry (bump `Recurrence-Count`) instead of duplicating.

### Metadata
- Source: posttooluse-error-sweep
- Reproducible: unknown
- Pattern-Key: vcs.fatal-error

---

## [ERR-20261110-2YF] bash_failure

**Logged**: 2026-11-10T15:04:29.037Z
**Priority**: medium
**Status**: pending
**Area**: config

### Summary
Auto-detected 1 error signature in a `Bash` tool result.

### Error
```
- `fs.no-such-file` — /tmp/served_assistant.js [Errno 2] No such file or directory: '/tmp/served_assistant.js'
```

### Context
- Detected by the self-improvement hook on `PostToolUse` (real-time, at the moment of failure)
- Tool: `Bash`
- Working directory: `C:\Users\刘帅良\Documents\Qoder\2026-11-08\62711a50\ocs-ai-gateway`
- Session: `e4aa9ed1-c799-4628-84d3-839586c8500c`
- Excerpts are truncated and redacted; check the transcript for full context

### Suggested Fix
Triage this entry. If the failure was real and non-obvious, keep it and fill in the actual fix; if it was transient or already handled, mark it resolved. Before keeping it, grep for the Pattern-Key(s) above and fold recurrences into the existing entry (bump `Recurrence-Count`) instead of duplicating.

### Metadata
- Source: posttooluse-error-sweep
- Reproducible: unknown
- Pattern-Key: fs.no-such-file

---

## [ERR-20261110-4KZ] bash_failure

**Logged**: 2026-11-10T16:31:03.523Z
**Priority**: medium
**Status**: pending
**Area**: config

### Summary
Auto-detected 1 error signature in a `Bash` tool result.

### Error
```
- `runtime.type-error` — TypeError: TraceStore.stage() got multiple values for argument 'status'
```

### Context
- Detected by the self-improvement hook on `PostToolUse` (real-time, at the moment of failure)
- Tool: `Bash`
- Working directory: `C:\Users\刘帅良\Documents\Qoder\2026-11-08\62711a50\ocs-ai-gateway`
- Session: `e4aa9ed1-c799-4628-84d3-839586c8500c`
- Excerpts are truncated and redacted; check the transcript for full context

### Suggested Fix
Triage this entry. If the failure was real and non-obvious, keep it and fill in the actual fix; if it was transient or already handled, mark it resolved. Before keeping it, grep for the Pattern-Key(s) above and fold recurrences into the existing entry (bump `Recurrence-Count`) instead of duplicating.

### Metadata
- Source: posttooluse-error-sweep
- Reproducible: unknown
- Pattern-Key: runtime.type-error

---

## [ERR-20261111-3PT] bash_failure

**Logged**: 2026-11-11T01:34:16.697Z
**Priority**: medium
**Status**: pending
**Area**: config

### Summary
Auto-detected 1 error signature in a `Bash` tool result.

### Error
```
- `shell.nonzero-exit` — non-zero exit means the Task failed or capturing it is the deliverable. Send
```

### Context
- Detected by the self-improvement hook on `PostToolUse` (real-time, at the moment of failure)
- Tool: `Bash`
- Working directory: `C:\Users\刘帅良\Documents\Qoder\2026-11-08\62711a50\ocs-ai-gateway`
- Session: `34f323cb-05d7-4931-b26d-3afe83e61b8e`
- Excerpts are truncated and redacted; check the transcript for full context

### Suggested Fix
Triage this entry. If the failure was real and non-obvious, keep it and fill in the actual fix; if it was transient or already handled, mark it resolved. Before keeping it, grep for the Pattern-Key(s) above and fold recurrences into the existing entry (bump `Recurrence-Count`) instead of duplicating.

### Metadata
- Source: posttooluse-error-sweep
- Reproducible: unknown
- Pattern-Key: shell.nonzero-exit

---

## [ERR-20261111-KZX] bash_failure

**Logged**: 2026-11-11T08:49:20.725Z
**Priority**: medium
**Status**: pending
**Area**: config

### Summary
Auto-detected 3 error signatures in a `Bash` tool result.

### Error
```
- `fs.no-such-file` — ls: cannot access 'data/bank.cleaned.db': No such file or directory
- `fs.no-such-file` — ls: cannot access 'data/_rebuilt_bank.db': No such file or directory
- `fs.no-such-file` — ls: cannot access 'data/bank.rebuilt.db': No such file or directory
```

### Context
- Detected by the self-improvement hook on `PostToolUse` (real-time, at the moment of failure)
- Tool: `Bash`
- Working directory: `C:\Users\刘帅良\Documents\Qoder\2026-11-08\62711a50\ocs-ai-gateway`
- Session: `34f323cb-05d7-4931-b26d-3afe83e61b8e`
- Excerpts are truncated and redacted; check the transcript for full context

### Suggested Fix
Triage this entry. If the failure was real and non-obvious, keep it and fill in the actual fix; if it was transient or already handled, mark it resolved. Before keeping it, grep for the Pattern-Key(s) above and fold recurrences into the existing entry (bump `Recurrence-Count`) instead of duplicating.

### Metadata
- Source: posttooluse-error-sweep
- Reproducible: unknown
- Pattern-Key: fs.no-such-file

---

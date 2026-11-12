"""读取环境变量与路径常量。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

# 数据目录可用环境变量指走（测试隔离用）。默认仍是项目下的 data/。
DATA_DIR = Path(os.getenv("OCS_DATA_DIR") or (BASE_DIR / "data"))


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


HOST = os.getenv("HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = _int("PORT", 8000)
GATEWAY_TOKEN = os.getenv("GATEWAY_TOKEN", "").strip()

# 跨源白名单：默认只放行课程站点与 localhost（油猴脚本的 fetch 分支要用）。
# 需要额外站点时在 .env 里配置 CORS_ORIGINS（逗号分隔）或覆盖 CORS_ORIGIN_REGEX。
# 设为空字符串可完全关闭跨源：CORS_ORIGINS= 且 CORS_ORIGIN_REGEX=
DEFAULT_CORS_ORIGIN_REGEX = (
    r"https?://([a-z0-9-]+\.)*(chaoxing\.com|chaoxing\.cn|icourse163\.org|icourse163\.com"
    r"|xuetangx\.com|icve\.com\.cn)(:\d+)?"
    r"|https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?"
)
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "").split(",")
    if origin.strip()
]
CORS_ORIGIN_REGEX = os.getenv("CORS_ORIGIN_REGEX", DEFAULT_CORS_ORIGIN_REGEX).strip()
CACHE_TTL_HOURS = _float("CACHE_TTL_HOURS", 168)
# 填空题单独用更短的缓存 TTL（0 = 沿用 CACHE_TTL_HOURS）。
COMPLETION_CACHE_TTL_HOURS = _float("COMPLETION_CACHE_TTL_HOURS", 24)
REQUEST_TIMEOUT_SECONDS = _int("REQUEST_TIMEOUT_SECONDS", 30)
SAMPLE_BUDGET_SECONDS = _int("SAMPLE_BUDGET_SECONDS", 20)

# 缓存重问：命中缓存但来源是 AI、且置信度低于阈值时，按概率忽略缓存重解，
# 避免一次低质量答案被缓存钉死。概率默认 0（关闭），灰度后建议 0.1。
CACHE_RETRY_PROBABILITY = min(1.0, max(0.0, _float("CACHE_RETRY_PROBABILITY", 0.0)))
CACHE_RETRY_MIN_CONFIDENCE = min(1.0, max(0.0, _float("CACHE_RETRY_MIN_CONFIDENCE", 0.75)))

# 批量答题（D4 起启用）。BATCH_ENABLED=0 时 /api/batch/query 返回 404。
BATCH_ENABLED = _int("BATCH_ENABLED", 0) > 0
BATCH_MAX_ITEMS = max(1, min(_int("BATCH_MAX_ITEMS", 8), 30))
BATCH_MAX_CHARS = max(1000, _int("BATCH_MAX_CHARS", 12000))
# 每个客户端每分钟允许的批量请求数，0 = 不限制。
BATCH_RATE_LIMIT = max(0, _int("BATCH_RATE_LIMIT", 10))

# 选项快照：答题页上报的选项真实内容（内存），答案按选项指纹持久化。
OPTION_SNAPSHOT_TTL_SECONDS = _int("OPTION_SNAPSHOT_TTL_SECONDS", 600)
OPTION_SNAPSHOT_ANSWER_TTL_SECONDS = _int("OPTION_SNAPSHOT_ANSWER_TTL_SECONDS", 30 * 86400)
# 每个客户端的快照上报频率上限（次/分钟）；0 表示不限制。
SNAPSHOT_RATE_LIMIT = _int("SNAPSHOT_RATE_LIMIT", 120)

PROVIDERS_PATH = str(DATA_DIR / "providers.json")
CACHE_PATH = str(DATA_DIR / "answers.db")
BANK_PATH = str(DATA_DIR / "bank.json")
# 题库主库路径，可用环境变量 BANK_DB_PATH 指向重建后的库（应急切换用）。
# 相对路径要按 DATA_DIR 解析 —— 否则启动目录不同（``uv run`` / ``python main.py`` /
# 预览管理器各自的 cwd）会指向不同的文件，2026-11-10 实测因此读到了空库。
#
# 约定：相对路径里开头的 ``data/`` 是「数据目录」这一层，要去掉再拼到 DATA_DIR，
# 否则会拼出 ``<DATA_DIR>/data/xxx.db``。用**字面量 "data"** 判断而不是
# ``DATA_DIR.name`` —— 后者在 DATA_DIR 被指到临时目录时不叫 "data"，
# 判断就失效了（2026-11-10 实测：拼出 <临时目录>/data/bank.cleaned.db，
# 一个不存在的文件，测试于是退化成空库）。
_BANK_DB_ENV = os.getenv("BANK_DB_PATH", "").strip()
if _BANK_DB_ENV:
    _BANK_DB_PATH = Path(_BANK_DB_ENV)
    if not _BANK_DB_PATH.is_absolute():
        parts = _BANK_DB_PATH.parts
        if parts and parts[0].lower() == "data":
            _BANK_DB_PATH = DATA_DIR.joinpath(*parts[1:])
        else:
            _BANK_DB_PATH = DATA_DIR / _BANK_DB_PATH
    BANK_DB_PATH = str(_BANK_DB_PATH)
else:
    BANK_DB_PATH = str(DATA_DIR / "bank.db")
STATS_PATH = str(DATA_DIR / "stats.json")
SUBJECTS_PATH = str(DATA_DIR / "subjects.json")
SETTINGS_PATH = str(DATA_DIR / "settings.json")
PROGRESS_PATH = str(DATA_DIR / "progress.json")
FAILURES_PATH = str(DATA_DIR / "failures.json")
HTTP_LOG_PATH = str(DATA_DIR / "http_log.db")
DEADLINES_PATH = str(DATA_DIR / "deadlines.json")
# 截止时间过去多少天后自动归档，不再占用 DDL 看板；归档数据仍保留。
DEADLINE_RETENTION_DAYS = max(1, _int("DEADLINE_RETENTION_DAYS", 14))
SCENE_PATH = str(DATA_DIR / "scene.json")
UNIT_SESSIONS_PATH = str(DATA_DIR / "unit_sessions.json")
SNAPSHOT_DIR = str(DATA_DIR / "snapshots")
SEMANTIC_DIR = str(DATA_DIR / "models" / "bge-small-zh-v1.5")
SEMANTIC_DB = str(DATA_DIR / "semantic.db")
ANSWER_MEMORY_PATH = str(DATA_DIR / "answer_memory.db")
# 联网检索缓存（可重建，不进备份）：同一道题的搜索结果复用，避免重复请求。
WEB_CACHE_PATH = str(DATA_DIR / "web_cache.db")
# 可选：填了 Tavily key 就优先用 Tavily，否则用无需 key 的必应。
WEB_SEARCH_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()
WEB_SEARCH_ENGINE = os.getenv("WEB_SEARCH_ENGINE", "").strip().lower() or "auto"
WEB_SEARCH_TIMEOUT = _float("WEB_SEARCH_TIMEOUT", 4.0)

MAX_CONCURRENCY = max(1, min(_int("MAX_CONCURRENCY", 3), 20))
ADMIN_HTML = BASE_DIR / "app" / "static" / "admin.html"
TOOLS_DIR = BASE_DIR / "tools"

MAX_TITLE_LENGTH = 2000
MAX_OPTIONS_COUNT = 26

# 首次运行时用于自动创建第一个 AI 条目
SEED = {
    "name": "默认 AI",
    "base_url": os.getenv("OPEN_AI_BASE_URL", "").strip(),
    "api_key": os.getenv("OPEN_AI_API_KEY", "").strip(),
    "model": os.getenv("OPEN_AI_MODEL", "").strip(),
}
if not SEED["api_key"] or SEED["api_key"] == "your-api-key":
    SEED["api_key"] = ""

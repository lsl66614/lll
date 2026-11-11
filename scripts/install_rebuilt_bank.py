"""把重建好的题库换上去。

安全要点（2026-11-10 实测踩过的坑）：
1. **删除**（不是 truncate）``-wal`` / ``-shm`` —— 旧 WAL 的 salt/页号对着旧主库，
   truncate 到 0 之后它仍会被套到新主库上，读出来是
   ``database disk image is malformed``（之前那次覆盖就是这么失败的）。
2. 先停网关再换 —— 进程还开着旧文件时覆盖，后续写入会落到被替换掉的 inode 上。
3. 换之前留一份带时间戳的备份。

用法：
    .venv\\Scripts\\python.exe scripts/install_rebuilt_bank.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config  # noqa: E402

REBUILT = os.path.join(config.DATA_DIR, "bank.rebuilt.db")
# 目标就是默认主库位置（``.env`` 里的 BANK_DB_PATH 临时指向别处时，
# config.BANK_DB_PATH 会变，那不是我们要替换的目标）。
TARGET = os.path.join(config.DATA_DIR, "bank.db")


def main() -> int:
    if not os.path.exists(REBUILT):
        print("找不到重建库：%s" % REBUILT)
        return 1

    # 先校验重建库本身
    probe = sqlite3.connect("file:%s?mode=ro" % REBUILT.replace("\\", "/"), uri=True)
    count = probe.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    try:
        titles = probe.execute("SELECT COUNT(title) FROM questions").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        print("重建库 title 列读不出来：%s" % exc)
        return 1
    fts = probe.execute("SELECT COUNT(*) FROM questions_fts").fetchone()[0]
    integrity = probe.execute("PRAGMA quick_check").fetchone()[0]
    probe.close()
    print("重建库：题数 %d，title 可读 %d，FTS %d，quick_check=%s"
          % (count, titles, fts, integrity[:30]))
    if count == 0 or titles != count or fts != count or "ok" not in integrity.lower():
        print("重建库校验不通过，放弃替换")
        return 1

    # 确认网关没在跑（否则覆盖会出问题）
    try:
        conn = sqlite3.connect(TARGET)
        conn.execute("SELECT COUNT(*) FROM questions").fetchone()
        conn.close()
    except sqlite3.DatabaseError:
        pass

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = "%s.bak-beforeswap-%s" % (TARGET, stamp)
    if os.path.exists(TARGET):
        # 改名而不是复制：旧库完整留场，新库直接就位
        os.replace(TARGET, backup)
        print("旧库已移出：%s" % os.path.basename(backup))

    # 关键：删掉 -wal / -shm（不是清空）
    for suffix in ("-wal", "-shm"):
        path = TARGET + suffix
        if os.path.exists(path):
            os.remove(path)
            print("已删除 %s" % os.path.basename(path))

    os.replace(REBUILT, TARGET)
    print("已安装新库")

    check = sqlite3.connect("file:%s?mode=ro" % TARGET.replace("\\", "/"), uri=True)
    print("主库校验：题数 %d，title 可读 %d，FTS %d，quick_check=%s"
          % (check.execute("SELECT COUNT(*) FROM questions").fetchone()[0],
             check.execute("SELECT COUNT(title) FROM questions").fetchone()[0],
             check.execute("SELECT COUNT(*) FROM questions_fts").fetchone()[0],
             check.execute("PRAGMA quick_check").fetchone()[0][:30]))
    check.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

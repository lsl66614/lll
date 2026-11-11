"""回填题库的 img_key 列（题干图片指纹）。

背景：库里 1272 条记录中 576 条题干带图片 URL，但「图片身份」一直只以
字符串形式躺在 title 里；记录一旦被压掉 URL，就再也区分不开「同文字不同图」
的两道题（实测 12 个骨架对应多道题、覆盖 46/434 条判断题用例）。

``img_key`` = 题干里图片 URL 的稳定指纹，按出现顺序用 ``|`` 连接；无图为空串。
它是**派生列**，从 title 纯计算得来，可重复、可随时重算。

用法：
    .venv\\Scripts\\python.exe scripts/backfill_img_key.py          # 只统计，不写
    .venv\\Scripts\\python.exe scripts/backfill_img_key.py --apply  # 实际写入

⚠️ 写完后需要**重启网关**，否则运行中的实例内存里还是旧数据，
它后续的写入可能把 img_key 覆盖回空值。
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402
from app.bankdb import BankDB  # noqa: E402
from app.shape_key import image_key  # noqa: E402


def main() -> int:
    apply = "--apply" in sys.argv
    path = os.path.abspath(config.BANK_DB_PATH)
    print("库：%s" % path)
    print("模式：%s\n" % ("写入" if apply else "只统计（加 --apply 才写）"))

    # 先用 BankDB 走一遍真实的建表/迁移，保证 img_key 列存在
    # （迁移逻辑只写在 app/bankdb.py 一处，脚本不复制一份）
    schema = BankDB(path)
    schema.close()

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT id, type, title, img_key FROM questions").fetchall()
    except sqlite3.OperationalError as exc:
        print("读不了库：%s" % exc)
        return 1

    total = len(rows)
    with_img = 0
    empty_new = 0
    changed = 0
    same = 0
    samples: list[tuple[str, str]] = []

    updates: list[tuple[str, str]] = []
    for qid, qtype, title, old in rows:
        new = image_key(title)
        if new:
            with_img += 1
        else:
            empty_new += 1
        if (old or "") == new:
            same += 1
            continue
        changed += 1
        updates.append((new, qid))
        if len(samples) < 8 and new:
            samples.append((str(title)[:56], new[:60]))

    print("总记录            %d" % total)
    print("  题干有图        %d" % with_img)
    print("  题干无图        %d" % empty_new)
    print("  已经是目标值    %d" % same)
    print("  需要更新        %d\n" % changed)
    for title, key in samples:
        print("  例：%-56s → %s" % (title, key))

    if not apply:
        print("\n未写入。确认无误后加 --apply。")
        return 0
    if not updates:
        print("\n没有需要更新的行。")
        return 0

    with conn:
        conn.executemany("UPDATE questions SET img_key = ? WHERE id = ?", updates)
    left = conn.execute(
        "SELECT COUNT(*) FROM questions WHERE (img_key IS NULL OR img_key = '') "
        "AND (title LIKE '%http%')"
    ).fetchone()[0]
    conn.close()
    print("\n已写入 %d 行。仍为空但题干含 http 的行：%d" % (len(updates), left))
    print("⚠️ 记得重启网关，让运行中的实例重新加载。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

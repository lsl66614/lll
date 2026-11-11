"""把 tools/ 下的油猴脚本同步到 userscript/ 目录。

背景：仓库里同一份脚本存在两个副本——
  - tools/ocs-progress.user.js      网关对外分发的源（/tools/ocs-progress.user.js）
  - userscript/ocs-progress.user.js README 里让用户直接安装的那份
历史上两份各改各的，早就漂移过。这里把 tools/ 定为唯一事实源，
userscript/ 由本脚本生成，避免「改一份忘另一份」。

用法：
  python scripts/sync_userscripts.py          # 同步（有差异时覆盖）
  python scripts/sync_userscripts.py --check  # 只检查，有差异就非零退出（CI 用）
"""

from __future__ import annotations

import argparse
import filecmp
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(BASE_DIR, "tools")
USERSCRIPT_DIR = os.path.join(BASE_DIR, "userscript")
# 只有这些脚本在两个目录里各有一份
SYNCED = ["ocs-progress.user.js", "ocs-assistant.user.js"]


def diff_files() -> list[str]:
    out: list[str] = []
    for name in SYNCED:
        src = os.path.join(TOOLS_DIR, name)
        dst = os.path.join(USERSCRIPT_DIR, name)
        if not os.path.isfile(src):
            continue
        if not os.path.isfile(dst):
            out.append(name)
        elif not filecmp.cmp(src, dst, shallow=False):
            out.append(name)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只检查不写；有差异就退出码 1")
    args = parser.parse_args()

    os.makedirs(USERSCRIPT_DIR, exist_ok=True)
    stale = diff_files()
    if not stale:
        print("userscript/ 与 tools/ 已同步")
        return 0
    if args.check:
        print("以下脚本与 tools/ 不一致，请运行 python scripts/sync_userscripts.py：")
        for name in stale:
            print("  -", name)
        return 1
    for name in stale:
        src = os.path.join(TOOLS_DIR, name)
        dst = os.path.join(USERSCRIPT_DIR, name)
        with open(src, "rb") as fp:
            data = fp.read()
        with open(dst, "wb") as fp:
            fp.write(data)
        print("已同步：", os.path.relpath(dst, BASE_DIR))
    return 0


if __name__ == "__main__":
    sys.exit(main())

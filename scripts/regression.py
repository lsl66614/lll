"""一条命令跑完回归：单元测试 + 脚本语法 + 接口冒烟 + 备份完整性 + 版本漂移。

用法：.venv\\Scripts\\python.exe scripts/regression.py
退出码：0 全通过；1 有失败。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "http://127.0.0.1:8000"
RESULTS: list[dict] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": str(detail)[:300]})
    print(("PASS" if ok else "FAIL"), name, "|", detail)


def http(path: str, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def run_unittest() -> None:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
    )
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    summary = " ".join(tail[-6:] if proc.returncode == 0 else tail[-40:])
    record("unit tests", proc.returncode == 0, summary)


def run_node_check() -> None:
    files = [
        os.path.join(ROOT, "tools", "mooc-result-probe.user.js"),
        os.path.join(ROOT, "tools", "ocs-progress.user.js"),
        os.path.join(ROOT, "tools", "ocs-assistant.user.js"),
        os.path.join(ROOT, "tools", "chaoxing-probe.user.js"),
    ]
    for path in files:
        proc = subprocess.run(["node", "--check", path], capture_output=True, text=True, encoding="utf-8", errors="replace")
        record(f"node --check {os.path.basename(path)}", proc.returncode == 0, (proc.stderr or "").strip()[:120])

    admin = os.path.join(ROOT, "app", "static", "admin.html")
    html = open(admin, encoding="utf-8").read()
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    tmp = os.path.join(ROOT, "data", "_regression_admin.js")
    open(tmp, "w", encoding="utf-8").write(blocks[-1])
    try:
        proc = subprocess.run(["node", "--check", tmp], capture_output=True, text=True, encoding="utf-8", errors="replace")
        record("node --check admin inline", proc.returncode == 0, (proc.stderr or "").strip()[:120])
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def run_api_smoke() -> None:
    try:
        status, body = http("/health")
        record("health", status == 200 and body.get("status") == "ok", str(body))
    except Exception as exc:  # noqa: BLE001
        record("health", False, f"服务未启动？{exc}")
        return

    checks = [
        ("state", "/api/state", lambda d: "providers" in d),
        ("stats/breakdown", "/api/stats/breakdown", lambda d: "source" in d.get("bank", {})),
        ("bank list", "/api/bank?limit=1", lambda d: d.get("items") is not None),
        ("bank search", "/api/bank/search?q=" + urllib.parse.quote("行列式") + "&limit=3", lambda d: "items" in d),
        ("settings", "/api/settings", lambda d: "strict_official" in d),
        ("quiz status", "/api/quiz/status", lambda d: "items" in d),
        ("progress", "/api/progress", lambda d: "items" in d),
        ("failures", "/api/failures", lambda d: "items" in d),
        ("tools versions", "/api/tools/versions", lambda d: "served" in d),
        ("probes", "/api/probes", lambda d: "items" in d),
        ("ops", "/api/ops", lambda d: "runs" in d),
        ("vote queue", "/api/bank/vote-queue", lambda d: "items" in d),
        ("notify", "/api/notify", lambda d: "config" in d),
    ]
    for name, path, predicate in checks:
        try:
            status, body = http(path)
            record(name, status == 200 and predicate(body))
        except Exception as exc:  # noqa: BLE001
            record(name, False, str(exc))

    try:
        status, body = http("/api/bank/formula-index", {"dry_run": True})
        record("formula index", status == 200 and "missing" in body, f"urls={body.get('urls')} missing={body.get('missing')}")
    except Exception as exc:  # noqa: BLE001
        record("formula index", False, str(exc))

    try:
        admin_resp = urllib.request.urlopen(BASE + "/admin", timeout=20)
        record("admin page", admin_resp.status == 200)
    except Exception as exc:  # noqa: BLE001
        record("admin page", False, str(exc))


def run_backup_check() -> None:
    try:
        req = urllib.request.Request(BASE + "/api/backup")
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
        import io
        import zipfile

        names = zipfile.ZipFile(io.BytesIO(raw)).namelist()
        required = {"bank.json", "providers.json", "answers.db", "settings.json", "subjects.json", "progress.json", "quiz_status.json"}
        record("backup contents", required.issubset(set(names)), f"{len(names)} files")
    except Exception as exc:  # noqa: BLE001
        record("backup contents", False, str(exc))


def run_version_drift() -> None:
    try:
        _, body = http("/api/tools/versions")
        served = body.get("served") or {}
        reported = body.get("reported") or {}
        drift = []
        mapping = {"mooc-result-probe": "mooc-result-probe.user.js", "ocs-progress": "ocs-progress.user.js"}
        for key, filename in mapping.items():
            old = reported.get(key)
            new = served.get(filename)
            if old and new and old != new:
                drift.append(f"{key}: 浏览器 {old} < 网关 {new}")
        record("probe version drift", True, ("; ".join(drift) + "（待自动更新）") if drift else "无漂移")
    except Exception as exc:  # noqa: BLE001
        record("probe version drift", False, str(exc))


def run_userscript_e2e() -> None:
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "e2e_userscript.py")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=240,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        record("userscript e2e", False, str(exc))
        return
    tail = (proc.stdout or proc.stderr or "").strip().splitlines()
    summary = next((line for line in reversed(tail) if line.startswith(("SUMMARY", "SKIP"))), "")
    record("userscript e2e", proc.returncode == 0, summary if summary else " ".join(tail[-3:]))


def main() -> int:
    # 控制台可能是 GBK；子进程输出里的替换字符不能让整个回归崩在打印上。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    started = time.time()
    print("== 1/6 单元测试 ==")
    run_unittest()
    print("== 2/6 脚本语法 ==")
    run_node_check()
    print("== 3/6 接口冒烟 ==")
    run_api_smoke()
    print("== 4/6 备份完整性 ==")
    run_backup_check()
    print("== 5/6 版本漂移 ==")
    run_version_drift()
    print("== 6/6 油猴 E2E ==")
    run_userscript_e2e()

    failed = [r for r in RESULTS if not r["ok"]]
    print(f"\nSUMMARY {len(RESULTS) - len(failed)}/{len(RESULTS)} passed in {time.time() - started:.1f}s")
    os.makedirs(os.path.join(ROOT, "data", "logs"), exist_ok=True)
    out = os.path.join(ROOT, "data", "logs", time.strftime("regression-%Y%m%d-%H%M%S.json"))
    with open(out, "w", encoding="utf-8") as fp:
        json.dump({"at": time.time(), "results": RESULTS}, fp, ensure_ascii=False, indent=2)
    print("report:", out)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

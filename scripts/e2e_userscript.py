"""油猴脚本端到端冒烟：本地夹具页 → 注入 ocs-assistant → 网关真实接口。

依赖 Playwright 与系统 Edge（channel=msedge，无需下载浏览器内核）。
缺少依赖、网关未启动或浏览器不可用时输出 SKIP 并返回 0，方便挂进回归。

用法：
    .venv\\Scripts\\python.exe scripts/e2e_userscript.py
    .venv\\Scripts\\python.exe scripts/e2e_userscript.py --gateway http://127.0.0.1:8000 --headed
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PNG = ("data:image/png;base64,"
       "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
TITLE = "E2E夹具：行列式哪条性质成立"
IMAGE_TITLE = "E2E夹具：选择正确的图形"

FIXTURE = f"""<!doctype html><html><head><meta charset="utf-8"><title>E2E夹具</title></head><body>
<div class="question-item" id="q1">
  <div class="title">{TITLE}</div>
  <ul>
    <li><span class="option-letter">A</span><span class="option-content">行列互换值不变</span></li>
    <li><span class="option-letter">B</span><span class="option-content">某行乘k值不变</span></li>
    <li><span class="option-letter">C</span><span class="option-content">两行互换值变号</span></li>
    <li><span class="option-letter">D</span><span class="option-content">两行成比例值为零</span></li>
  </ul>
</div>
<div class="question-item" id="q2">
  <div class="title">{IMAGE_TITLE}</div>
  <ul>
    <li><span class="option-letter">A</span><span class="option-content"><img src="{PNG}" alt=""></span></li>
    <li><span class="option-letter">B</span><span class="option-content"><img src="{PNG}" alt=""></span></li>
  </ul>
</div>
</body></html>"""

PANEL_JS = """
() => {
  const host = document.getElementById('ocs-assistant-host');
  if (!host || !host.shadowRoot) return null;
  const shadow = host.shadowRoot;
  const text = id => (shadow.getElementById(id) || {}).textContent || '';
  return {
    online: text('ocsa-online'),
    bank: text('ocsa-bank'),
    qstate: text('ocsa-qstate'),
    title: text('ocsa-title'),
    opts: text('ocsa-optlist'),
    lookup: text('ocsa-lookup'),
    toast: text('ocsa-toast')
  };
}
"""


def http_json(base: str, path: str, token: str = "") -> dict:
    headers = {"X-Gateway-Token": token} if token else {}
    req = urllib.request.Request(base + path, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="ocs-assistant 油猴脚本端到端冒烟")
    parser.add_argument("--gateway", default=os.environ.get("E2E_GATEWAY", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.environ.get("E2E_TOKEN", ""))
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    base = args.gateway.rstrip("/")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: 未安装 playwright（uv pip install playwright）")
        return 0

    try:
        health = http_json(base, "/health")
        baseline = (http_json(base, "/api/state", args.token).get("option_snapshots") or {})
    except (urllib.error.URLError, OSError) as exc:
        print(f"SKIP: 网关未启动（{exc}）")
        return 0
    base_changes = int(baseline.get("order_changes") or 0)

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("PASS" if ok else "FAIL"), name, "|", detail)
        if not ok:
            failures.append(name)

    started = time.time()
    channel = os.environ.get("E2E_BROWSER_CHANNEL", "msedge")
    try:
        driver = sync_playwright().start()
        browser = driver.chromium.launch(channel=channel, headless=not args.headed)
    except Exception as exc:  # noqa: BLE001 - 浏览器不可用按 SKIP 处理
        print(f"SKIP: 无法启动浏览器（{channel}）：{exc}")
        return 0

    try:
        page = browser.new_page()
        page.goto(base + "/health", wait_until="domcontentloaded")
        page.set_content(FIXTURE)
        if args.token:
            page.evaluate(
                """(cfg) => {
                    window.GM_getValue = (key, fallback) =>
                        key === 'ocs_assistant_config' ? cfg : fallback;
                    window.GM_setValue = () => {};
                }""",
                json.dumps({"gateway": base, "token": args.token}),
            )
        page.add_script_tag(url=base + "/tools/ocs-assistant.user.js")

        page.wait_for_function(
            "() => { const h = document.getElementById('ocs-assistant-host');"
            " return !!(h && h.shadowRoot && h.shadowRoot.getElementById('ocsa-online')); }",
            timeout=15000,
        )
        page.wait_for_function(
            "() => { const h = document.getElementById('ocs-assistant-host');"
            " const n = h && h.shadowRoot && h.shadowRoot.getElementById('ocsa-online');"
            " return !!(n && n.textContent.includes('在线')); }",
            timeout=15000,
        )
        first = page.evaluate(PANEL_JS)
        check("面板在线", "网关在线" in first["online"], first["online"])
        check("题库数一致", first["bank"] == str(health.get("bank")), first["bank"])
        check("题目解析", TITLE in first["title"], first["title"])
        check("选项解析", "A行列互换值不变" in first["opts"], first["opts"][:80])

        page.wait_for_function(
            "() => { const h = document.getElementById('ocs-assistant-host');"
            " return !!(h && h.shadowRoot"
            " && h.shadowRoot.getElementById('ocsa-lookup').textContent.includes('选项快照')); }",
            timeout=15000,
        )
        posted = page.evaluate(PANEL_JS)
        check("快照已上报", "已上报" in posted["lookup"], posted["lookup"])

        listing = http_json(base, "/api/option-snapshots?limit=200", args.token)
        titles = {str(item.get("title") or "") for item in listing.get("items", [])}
        check("服务端快照", TITLE in titles and IMAGE_TITLE in titles,
              f"snapshots={len(titles)}")

        page.evaluate(
            """() => {
                const items = document.querySelectorAll('#q1 li .option-content');
                const a = items[0].textContent;
                items[0].textContent = items[1].textContent;
                items[1].textContent = a;
            }"""
        )
        page.wait_for_function(
            "() => { const h = document.getElementById('ocs-assistant-host');"
            " return !!(h && h.shadowRoot"
            " && h.shadowRoot.getElementById('ocsa-lookup').textContent.includes('顺序已变化')); }",
            timeout=20000,
        )
        reordered = page.evaluate(PANEL_JS)
        check("重排检测", "顺序已变化" in reordered["lookup"], reordered["lookup"])

        state = http_json(base, "/api/state", args.token)
        stats = state.get("option_snapshots") or {}
        check("服务端记录顺序变化", int(stats.get("order_changes") or 0) >= base_changes + 1, str(stats))
    finally:
        browser.close()
        driver.stop()
        try:
            listing = http_json(base, "/api/option-snapshots?limit=200", args.token)
            for item in listing.get("items", []):
                if str(item.get("title") or "").startswith("E2E夹具"):
                    headers = {"X-Gateway-Token": args.token} if args.token else {}
                    req = urllib.request.Request(
                        base + "/api/option-snapshots/" + item["key"],
                        headers=headers, method="DELETE")
                    urllib.request.urlopen(req, timeout=10).read()
        except Exception:  # noqa: BLE001 - 清理失败不影响冒烟结论
            pass

    elapsed = time.time() - started
    print(f"SUMMARY {'PASS' if not failures else 'FAIL'} ({len(failures)} failed) in {elapsed:.1f}s")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())

"""联网检索层的单元测试：查询构造、解析、缓存、预算与失败兜底。"""

from __future__ import annotations

import os
import tempfile
import unittest

import httpx

from app.retrieval import (
    WebSearch,
    build_query,
    format_context,
    parse_bing,
    parse_so,
    parse_tavily,
    source_list,
)

BING_HTML = """
<ol id="b_results">
  <li class="b_algo"><h2><a href="https://example.com/q1">原题与答案</a></h2>
    <p>答案为 B，解析见下。</p></li>
  <li class="b_algo"><h2><a href="https://example.com/q2">另一条资料</a></h2>
    <p>补充说明。</p></li>
</ol>
"""

SO_HTML = """
<ul class="result">
  <li class="res-list"><h3 class="res-title"><a href="/link?m=abc">原题与答案</a></h3>
    <p class="res-desc">答案为 B，解析见下。</p></li>
</ul>
"""

TAVILY_JSON = {
    "results": [
        {"title": "题库页", "url": "https://example.com/t", "content": "答案是 A"},
    ]
}


class QueryTests(unittest.TestCase):
    def test_urls_are_stripped(self) -> None:
        query = build_query("设矩阵 http://img.example.com/a.png 的秩为", ["A. 1", "B. 2"])
        self.assertNotIn("http", query)
        self.assertIn("矩阵", query)

    def test_short_stem_gets_options(self) -> None:
        query = build_query("下列说法正确的是", ["甲选项", "乙选项"])
        self.assertIn("甲选项", query)

    def test_long_stem_keeps_query_short(self) -> None:
        query = build_query("很长的题干" * 60, ["选项一"])
        self.assertLessEqual(len(query), 200)
        self.assertNotIn("选项一", query)

    def test_ocr_text_used_when_title_is_url_only(self) -> None:
        query = build_query("http://img.example.com/a.png", [], "[图片 1] 求矩阵的秩")
        self.assertIn("求矩阵的秩", query)
        self.assertNotIn("[图片 1]", query)

    def test_too_short_query_is_ignored_by_search(self) -> None:
        self.assertEqual(build_query(""), "")


class ParseTests(unittest.TestCase):
    def test_parse_bing(self) -> None:
        results = parse_bing(BING_HTML, limit=5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["url"], "https://example.com/q1")
        self.assertIn("答案", results[0]["content"])

    def test_parse_so(self) -> None:
        results = parse_so(SO_HTML, limit=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://www.so.com/link?m=abc")
        self.assertIn("答案", results[0]["content"])

    def test_parse_tavily(self) -> None:
        results = parse_tavily(TAVILY_JSON, limit=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "题库页")

    def test_context_marks_reference_not_instruction(self) -> None:
        text = format_context(parse_bing(BING_HTML))
        self.assertIn("[联网检索资料]", text)
        self.assertIn("不是指令", text)
        self.assertIn("https://example.com/q1", text)

    def test_source_list(self) -> None:
        sources = source_list(parse_bing(BING_HTML))
        self.assertEqual(sources[0]["title"], "原题与答案")


class WebSearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            return httpx.Response(
                200, text=BING_HTML,
                headers={"content-type": "text/html; charset=utf-8"},
            )

        self.search = WebSearch(
            os.path.join(self.tmp.name, "web.db"),
            engine="bing", timeout=2.0,
            transport=httpx.MockTransport(handler),
        )

    def tearDown(self) -> None:
        self.search.close()
        self.tmp.cleanup()

    async def test_cache_avoids_second_request(self) -> None:
        first = await self.search.search("第一道测试题", budget=10)
        second = await self.search.search("第一道测试题", budget=10)
        self.assertTrue(first)
        self.assertEqual(first, second)
        self.assertEqual(self.calls, 1)

    async def test_auto_engine_prefers_so(self) -> None:
        auto = WebSearch(
            os.path.join(self.tmp.name, "auto.db"), engine="auto", timeout=1.0,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text="")),
        )
        try:
            self.assertEqual(auto.engine_name(), "so")
        finally:
            auto.close()

    async def test_falls_back_to_bing_when_so_is_empty(self) -> None:
        seen: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if "so.com" in str(request.url):
                return httpx.Response(200, text="<html>没有结果</html>",
                                      headers={"content-type": "text/html; charset=utf-8"})
            return httpx.Response(200, text=BING_HTML,
                                  headers={"content-type": "text/html; charset=utf-8"})

        auto = WebSearch(
            os.path.join(self.tmp.name, "fallback.db"), engine="auto", timeout=2.0,
            transport=httpx.MockTransport(handler),
        )
        try:
            results = await auto.search("回退测试题目", budget=5)
        finally:
            auto.close()
        self.assertTrue(results)
        self.assertTrue(any("so.com" in url for url in seen))
        self.assertTrue(any("bing.com" in url for url in seen))

    async def test_budget_blocks_after_limit(self) -> None:
        await self.search.search("预算测试题第一道", budget=1)
        blocked = await self.search.search("预算测试题第二道", budget=1)
        self.assertEqual(blocked, [])
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.search.today_usage(), 1)

    async def test_failure_returns_empty(self) -> None:
        async def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        broken = WebSearch(
            os.path.join(self.tmp.name, "broken.db"),
            engine="bing", timeout=1.0,
            transport=httpx.MockTransport(boom),
        )
        try:
            self.assertEqual(await broken.search("失败测试题", budget=5), [])
        finally:
            broken.close()

    async def test_tavily_engine_uses_bearer(self) -> None:
        seen: dict[str, str] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, json=TAVILY_JSON)

        tavily = WebSearch(
            os.path.join(self.tmp.name, "tavily.db"),
            api_key="tvly-test", engine="auto", timeout=2.0,
            transport=httpx.MockTransport(handler),
        )
        try:
            results = await tavily.search("Tavily 测试题", budget=5)
        finally:
            tavily.close()
        self.assertEqual(tavily.engine_name(), "tavily")
        self.assertEqual(seen["auth"], "Bearer tvly-test")
        self.assertEqual(results[0]["title"], "题库页")


if __name__ == "__main__":
    unittest.main()

"""tavily_client 单元测试：结果格式化 + Key 轮询/容错（mock 网络）。"""

import asyncio
import json
from unittest.mock import patch

import pytest
from tavily_client import (
    MAX_EXTRACT_URLS,
    TavilyClient,
    TavilyClientCache,
    TavilySearchError,
    _extract_error_detail,
    _SEARCH_TIMEOUT,
    _EXTRACT_TIMEOUT,
    format_extract_results,
    format_results,
)


def test_format_results_with_source():
    results = [
        {"title": "A", "url": "https://a.com", "content": "content A"},
        {"title": "B", "url": "https://b.com", "content": "content B"},
    ]
    text = format_results(results, show_source=True)
    assert "1. A" in text
    assert "2. B" in text
    assert "[来源] https://a.com" in text
    assert "[来源] https://b.com" in text


def test_format_results_without_source():
    results = [{"title": "A", "url": "https://a.com", "content": "content A"}]
    text = format_results(results, show_source=False)
    assert "[来源]" not in text
    assert "https://a.com" not in text
    assert "content A" in text


def test_format_results_skips_missing_url():
    results = [{"title": "A", "url": "", "content": ""}]
    text = format_results(results, show_source=True)
    assert "[来源]" not in text


def test_key_rotation_round_robin():
    client = TavilyClient(["k1", "k2", "k3"])

    async def run():
        return [await client._next_key() for _ in range(4)]

    assert asyncio.run(run()) == ["k1", "k2", "k3", "k1"]


def test_search_no_keys_raises():
    client = TavilyClient([])
    with pytest.raises(TavilySearchError, match="未配置"):
        asyncio.run(client.search("q"))


class _FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def text(self):
        return "err body"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, url, json=None, headers=None):
        self.calls.append(headers.get("Authorization"))
        return self._responses.pop(0)


def test_search_failover_to_next_key():
    session = _FakeSession(
        [
            _FakeResponse(401, {}),
            _FakeResponse(
                200,
                {"results": [{"title": "t", "url": "https://x", "content": "c"}]},
            ),
        ]
    )
    client = TavilyClient(["bad-key", "good-key"])
    with patch("tavily_client.aiohttp.ClientSession", return_value=session):
        results = asyncio.run(client.search("q"))
    assert results[0]["title"] == "t"
    assert session.calls == ["Bearer bad-key", "Bearer good-key"]


def test_search_fails_fast_on_500():
    session = _FakeSession([_FakeResponse(500, {})])
    client = TavilyClient(["k1", "k2"])
    with (
        patch("tavily_client.aiohttp.ClientSession", return_value=session),
        pytest.raises(TavilySearchError) as exc_info,
    ):
        asyncio.run(client.search("q"))
    # 非重试错误不换 Key
    assert session.calls == ["Bearer k1"]
    # 原始响应体（"err body"）不进入给 LLM 的错误信息，只保留稳定的 HTTP 错误码
    assert "err body" not in str(exc_info.value)
    assert "HTTP 500" in str(exc_info.value)


class _FakeTextResp:
    """只返回指定文本的假响应，用于测试错误说明抽取。"""

    def __init__(self, text: str):
        self._text = text

    async def text(self):
        return self._text


def test_error_detail_prefers_short_json_field():
    resp = _FakeTextResp(json.dumps({"detail": "Invalid API key"}))
    assert asyncio.run(_extract_error_detail(resp)) == "Invalid API key"


def test_error_detail_nested_error_message():
    resp = _FakeTextResp(json.dumps({"error": {"message": "limit exceeded"}}))
    assert asyncio.run(_extract_error_detail(resp)) == "limit exceeded"


def test_error_detail_truncates_long_field():
    long_detail = "x" * 500
    resp = _FakeTextResp(json.dumps({"detail": long_detail}))
    assert len(asyncio.run(_extract_error_detail(resp))) <= 120


def test_error_detail_non_json_falls_back():
    resp = _FakeTextResp("plain text body")
    assert asyncio.run(_extract_error_detail(resp)) == "请求失败（响应体无法解析）"


def test_search_failover_on_timeout():
    class _FlakySession:
        """第一个 Key 超时，第二个 Key 正常返回。"""

        def __init__(self):
            self.calls: list[str] = []
            self._posts = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, json=None, headers=None):
            self.calls.append(headers.get("Authorization"))
            self._posts += 1
            if self._posts == 1:
                raise asyncio.TimeoutError("simulated timeout")
            return _FakeResponse(
                200,
                {"results": [{"title": "t", "url": "https://x", "content": "c"}]},
            )

    session = _FlakySession()
    client = TavilyClient(["slow-key", "good-key"])
    with patch("tavily_client.aiohttp.ClientSession", return_value=session):
        results = asyncio.run(client.search("q"))
    assert results[0]["title"] == "t"
    assert session.calls == ["Bearer slow-key", "Bearer good-key"]


def test_search_all_keys_timeout_raises():
    class _AlwaysTimeoutSession:
        def __init__(self):
            self.calls: list[str] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, json=None, headers=None):
            self.calls.append(headers.get("Authorization"))
            raise asyncio.TimeoutError("simulated timeout")

    session = _AlwaysTimeoutSession()
    client = TavilyClient(["k1", "k2"])
    with (
        patch("tavily_client.aiohttp.ClientSession", return_value=session),
        pytest.raises(TavilySearchError, match="超时"),
    ):
        asyncio.run(client.search("q"))
    assert session.calls == ["Bearer k1", "Bearer k2"]


# ── 客户端复用：多 Key 轮询必须跨调用生效 ───────────────


def test_client_cache_reuses_instance_for_same_keys():
    cache = TavilyClientCache()
    assert cache.get(["k1", "k2"]) is cache.get(["k1", "k2"])


def test_client_cache_rebuilds_on_key_change():
    cache = TavilyClientCache()
    first = cache.get(["k1", "k2"])
    assert cache.get(["k1", "k3"]) is not first
    # Key 内容（去空白后）不变就继续复用同一实例
    third = cache.get(["k1", "k3"])
    assert cache.get([" k1 ", " k3 "]) is third


def test_client_cache_rebuilds_after_invalidate():
    cache = TavilyClientCache()
    first = cache.get(["k1", "k2"])
    cache.invalidate()
    assert cache.get(["k1", "k2"]) is not first


def test_reused_client_rotates_keys_across_searches():
    """同一个插件持有的客户端连续两次搜索，必须分别用到 k1、k2。

    旧实现每次搜索都新建 TavilyClient，轮询下标每次从 0 开始 → 永远只用 keys[0]。
    """
    session = _FakeSession(
        [
            _FakeResponse(
                200,
                {"results": [{"title": "a", "url": "https://a", "content": "ca"}]},
            ),
            _FakeResponse(
                200,
                {"results": [{"title": "b", "url": "https://b", "content": "cb"}]},
            ),
        ]
    )
    cache = TavilyClientCache()

    async def run():
        return (
            await cache.get(["k1", "k2"]).search("q1"),
            await cache.get(["k1", "k2"]).search("q2"),
        )

    with patch("tavily_client.aiohttp.ClientSession", return_value=session):
        first, second = asyncio.run(run())

    assert session.calls == ["Bearer k1", "Bearer k2"]
    assert first[0]["title"] == "a"
    assert second[0]["title"] == "b"


def test_control_new_client_per_search_restarts_rotation():
    """对照项（非回归断言）：不复用实例时每次都从头轮询。

    这条用例断言的是**旧实现的退化行为**，因此修改前后都通过；它的作用是把
    「复用实例」与「每次新建」的差异钉成可读事实，防止后人把 `TavilyClientCache`
    当成多余包装删掉时无从对照。真正的回归断言在
    `test_reused_client_rotates_keys_across_searches` 与
    `tests/test_plugin_client_reuse.py`。
    """
    session = _FakeSession(
        [
            _FakeResponse(200, {"results": []}),
            _FakeResponse(200, {"results": []}),
        ]
    )

    async def run():
        await TavilyClient(["k1", "k2"]).search("q1")
        await TavilyClient(["k1", "k2"]).search("q2")

    with patch("tavily_client.aiohttp.ClientSession", return_value=session):
        asyncio.run(run())

    assert session.calls == ["Bearer k1", "Bearer k1"]


# ── /extract：正文提取 ──────────────────────────────


class _RecordingSession(_FakeSession):
    """额外记录最后一次请求体与 URL，用于断言 extract 的载荷形状。"""

    def __init__(self, responses: list):
        super().__init__(responses)
        self.payloads: list[dict] = []
        self.urls: list[str] = []

    def post(self, url, json=None, headers=None):
        self.urls.append(url)
        self.payloads.append(json or {})
        return super().post(url, json=json, headers=headers)


def test_extract_success_and_failed_both_returned():
    """HTTP 200 时成功与失败分别在两个数组里，二者都必须回传。"""
    session = _RecordingSession(
        [
            _FakeResponse(
                200,
                {
                    "results": [
                        {"url": "https://a.com", "raw_content": "# 标题\n正文甲"}
                    ],
                    "failed_results": [{"url": "https://b.com", "error": "timeout"}],
                },
            )
        ]
    )
    client = TavilyClient(["k1"])
    with patch("tavily_client.aiohttp.ClientSession", return_value=session):
        outcome = asyncio.run(client.extract(["https://a.com", "https://b.com"]))

    assert outcome["results"][0]["raw_content"] == "# 标题\n正文甲"
    assert outcome["failed"][0]["url"] == "https://b.com"
    assert session.urls == ["https://api.tavily.com/extract"]


def test_extract_omits_chunks_per_source_without_query():
    """不给 query 时不得带 chunks_per_source（官方文档：该参数仅在有 query 时可用）。"""
    session = _RecordingSession([_FakeResponse(200, {"results": []})])
    client = TavilyClient(["k1"])
    with patch("tavily_client.aiohttp.ClientSession", return_value=session):
        asyncio.run(client.extract(["https://a.com"]))

    assert "query" not in session.payloads[0]
    assert "chunks_per_source" not in session.payloads[0]


def test_extract_passes_query_and_depth():
    session = _RecordingSession([_FakeResponse(200, {"results": []})])
    client = TavilyClient(["k1"])
    with patch("tavily_client.aiohttp.ClientSession", return_value=session):
        asyncio.run(
            client.extract(
                ["https://a.com"],
                extract_depth="advanced",
                query="模型参数规模",
            )
        )

    assert session.payloads[0]["extract_depth"] == "advanced"
    assert session.payloads[0]["query"] == "模型参数规模"


def test_extract_failover_to_next_key():
    """extract 与 search 共用同一套 Key failover 语义。"""
    session = _RecordingSession(
        [
            _FakeResponse(429, {}),
            _FakeResponse(
                200,
                {"results": [{"url": "https://a.com", "raw_content": "ok"}]},
            ),
        ]
    )
    client = TavilyClient(["busy-key", "good-key"])
    with patch("tavily_client.aiohttp.ClientSession", return_value=session):
        outcome = asyncio.run(client.extract(["https://a.com"]))

    assert session.calls == ["Bearer busy-key", "Bearer good-key"]
    assert outcome["results"][0]["raw_content"] == "ok"


def test_extract_no_keys_raises():
    client = TavilyClient([])
    with pytest.raises(TavilySearchError, match="未配置"):
        asyncio.run(client.extract(["https://a.com"]))


def test_extract_fails_fast_on_non_retryable_status():
    session = _RecordingSession([_FakeResponse(400, {})])
    client = TavilyClient(["k1", "k2"])
    with (
        patch("tavily_client.aiohttp.ClientSession", return_value=session),
        pytest.raises(TavilySearchError, match="HTTP 400"),
    ):
        asyncio.run(client.extract(["https://a.com"]))
    assert session.calls == ["Bearer k1"]


# ── format_extract_results：截断与失败提示 ───────────


def test_format_extract_truncates_long_content():
    long_text = "段落。\n" * 500
    text = format_extract_results(
        [{"url": "https://a.com", "raw_content": long_text}],
        [],
        max_chars_per_url=200,
    )
    assert "已截断" in text
    assert len(text) < len(long_text)


def test_format_extract_no_truncation_when_disabled():
    long_text = "x" * 300
    text = format_extract_results(
        [{"url": "https://a.com", "raw_content": long_text}],
        [],
        max_chars_per_url=0,
    )
    assert "已截断" not in text
    assert "x" * 300 in text


def test_format_extract_truncation_prefers_sentence_boundary():
    """句末标点落在窗口后半段时，截断点取该句末，不把末句劈开。"""
    sentence = "前文" * 15 + "。"  # 31 字符，句末标点位于窗口(50)后半段
    content = sentence + "后续内容" * 50
    text = format_extract_results(
        [{"url": "https://a.com", "raw_content": content}],
        [],
        max_chars_per_url=50,
    )
    assert sentence in text.splitlines()


def test_format_extract_truncation_ignores_early_boundary():
    """句末标点落在窗口前半段时退回硬截断，避免为对齐句末白丢过半配额。"""
    content = "短。" + "后文" * 100  # 句末标点在第 2 字符，远在窗口(50)前半段
    text = format_extract_results(
        [{"url": "https://a.com", "raw_content": content}],
        [],
        max_chars_per_url=50,
    )
    body = next(line for line in text.splitlines() if line.startswith("短。"))
    assert len(body) == 50


def test_format_extract_reports_failures_and_hint():
    text = format_extract_results([], [{"url": "https://b.com", "error": "403"}], 100)
    assert "https://b.com" in text
    assert "提取失败" in text
    assert "搜索" in text


def test_format_extract_empty_results_and_failures():
    assert format_extract_results([], [], 100) == "Tavily 没有返回任何提取结果。"


def test_format_extract_handles_missing_content():
    text = format_extract_results(
        [{"url": "https://a.com", "raw_content": ""}], [], 100
    )
    assert "未返回正文内容" in text


def test_max_extract_urls_matches_api_contract():
    """单批上限对齐 /extract 的 1-20 契约，改错会静默丢 URL。"""
    assert MAX_EXTRACT_URLS == 20


def test_extract_timeout_exceeds_server_side_extract_cap():
    """提取端点的总超时必须大于服务端 advanced 提取的 30 秒上限（官方默认值）。

    两者相等即等于客户端先放弃：请求已经在服务端跑起来并计费，却拿不回内容。
    """
    assert _EXTRACT_TIMEOUT.total is not None
    assert _EXTRACT_TIMEOUT.total > 30


def test_search_timeout_stays_small():
    """搜索端点必须保持较小的超时，不能被提取端点的长超时带上。

    /search 有服务端自身的快速返回预期；若与 /extract 共用 45 秒，单 Key 挂起时
    会阻塞回复链路 45×N 秒（N 为 Key 数）。
    """
    assert _SEARCH_TIMEOUT.total is not None
    assert _SEARCH_TIMEOUT.total <= 20
    assert _SEARCH_TIMEOUT.total < _EXTRACT_TIMEOUT.total


def test_search_timeout_is_passed_to_session():
    """搜索必须把自己的短超时传给 session，而不是沿用提取端点的长超时。"""
    session = _FakeSession([_FakeResponse(200, {"results": []})])
    client = TavilyClient(["k1"])
    with patch(
        "tavily_client.aiohttp.ClientSession", return_value=session
    ) as session_cls:
        asyncio.run(client.search("q"))

    assert session_cls.call_args.kwargs["timeout"] is _SEARCH_TIMEOUT


def test_extract_timeout_is_passed_to_session():
    """提取必须把自己的长超时传给 session，避免服务端还在跑就被客户端掐断。"""
    session = _FakeSession([_FakeResponse(200, {"results": []})])
    client = TavilyClient(["k1"])
    with patch(
        "tavily_client.aiohttp.ClientSession", return_value=session
    ) as session_cls:
        asyncio.run(client.extract(["https://a.com"]))

    assert session_cls.call_args.kwargs["timeout"] is _EXTRACT_TIMEOUT


def test_search_error_carries_no_duplicate_domain_prefix():
    """网络层只抛中性细节，搜索失败的整体文案由调用方一次拼出，不出现重复前缀。"""
    session = _FakeSession([_FakeResponse(500, {})])
    client = TavilyClient(["k1"])
    with (
        patch("tavily_client.aiohttp.ClientSession", return_value=session),
        pytest.raises(TavilySearchError) as exc_info,
    ):
        asyncio.run(client.search("q"))

    message = str(exc_info.value)
    assert message.startswith("HTTP 500")
    # 领域前缀（「Tavily 搜索失败」）由调用方补；网络层再带一次就成了重复前缀。
    assert "搜索失败" not in message
    assert "Tavily" not in message


def test_search_failover_on_432_quota_exhausted():
    """432 是 Tavily 的配额耗尽码，必须换 Key 重试而不是快速失败。"""
    session = _FakeSession(
        [
            _FakeResponse(432, {}),
            _FakeResponse(200, {"results": [{"title": "t", "url": "https://x"}]}),
        ]
    )
    client = TavilyClient(["exhausted-key", "good-key"])
    with patch("tavily_client.aiohttp.ClientSession", return_value=session):
        results = asyncio.run(client.search("q"))

    assert results[0]["title"] == "t"
    assert session.calls == ["Bearer exhausted-key", "Bearer good-key"]


def test_search_failover_on_403_disabled_key():
    """403 表示当前 Key 被禁用，同样应换 Key 重试。"""
    session = _FakeSession(
        [
            _FakeResponse(403, {}),
            _FakeResponse(200, {"results": [{"title": "t", "url": "https://x"}]}),
        ]
    )
    client = TavilyClient(["disabled-key", "good-key"])
    with patch("tavily_client.aiohttp.ClientSession", return_value=session):
        results = asyncio.run(client.search("q"))

    assert results[0]["title"] == "t"
    assert session.calls == ["Bearer disabled-key", "Bearer good-key"]


def test_extract_reports_server_error_reason():
    """failed_results 里的 error 要出现在给 LLM 的文本里，模型才能区分失败原因。"""
    text = format_extract_results(
        [], [{"url": "https://b.com", "error": "Failed to retrieve content"}], 100
    )
    assert "https://b.com" in text
    assert "Failed to retrieve content" in text


def test_extract_failure_without_reason_has_no_empty_parens():
    """error 为空时不留一对空括号。"""
    text = format_extract_results([], [{"url": "https://b.com", "error": ""}], 100)
    line = next(line for line in text.splitlines() if line.startswith("- "))
    assert line == "- https://b.com"

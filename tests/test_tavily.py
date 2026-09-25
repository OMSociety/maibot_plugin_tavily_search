"""tavily_client 单元测试：结果格式化 + Key 轮询/容错（mock 网络）。"""

import asyncio
import json
from unittest.mock import patch

import pytest
from tavily_client import (
    TavilyClient,
    TavilyClientCache,
    TavilySearchError,
    _extract_error_detail,
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

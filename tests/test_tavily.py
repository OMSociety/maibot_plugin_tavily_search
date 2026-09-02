"""tavily_client 单元测试：结果格式化 + Key 轮询/容错（mock 网络）。"""

import asyncio
from unittest.mock import patch

import pytest

from tavily_client import TavilyClient, TavilySearchError, format_results


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
        pytest.raises(TavilySearchError, match="HTTP 500"),
    ):
        asyncio.run(client.search("q"))
    # 非重试错误不换 Key
    assert session.calls == ["Bearer k1"]


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

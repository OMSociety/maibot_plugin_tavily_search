"""Tavily 搜索客户端：多 Key 轮询 + HTTP 调用 + 结果格式化。

参考 AstrBot ``astrbot/core/tools/web_search_tools.py`` 的 ``_tavily_search`` /
``_KeyRotator`` 实现。
"""

from __future__ import annotations

import asyncio

import aiohttp

_SEARCH_URL = "https://api.tavily.com/search"
# 这些 HTTP 状态码表示「当前 Key 有问题」，应换下一个 Key 重试。
# 401 未授权 / 403 禁用 / 429 限流 / 432 Tavily 配额耗尽。
_RETRYABLE_STATUSES: frozenset[int] = frozenset({401, 403, 429, 432})


class TavilySearchError(Exception):
    """Tavily 搜索失败。"""


class TavilyClient:
    """带多 Key 轮询与 failover 的 Tavily 搜索客户端。"""

    def __init__(self, keys: list[str]) -> None:
        self._keys = [str(key).strip() for key in keys if str(key).strip()]
        self._index = 0
        self._lock = asyncio.Lock()

    async def _next_key(self) -> str:
        """轮询返回下一个 Key（并发安全）。"""
        async with self._lock:
            key = self._keys[self._index % len(self._keys)]
            self._index = (self._index + 1) % len(self._keys)
            return key

    async def search(
        self,
        query: str,
        max_results: int = 7,
        search_depth: str = "basic",
    ) -> list[dict[str, str]]:
        """调用 Tavily Search API，返回归一化结果。

        Args:
            query: 搜索关键词。
            max_results: 返回条数。
            search_depth: 搜索深度，basic 或 advanced。

        Returns:
            每条含 title/url/content 的字典列表。

        Raises:
            TavilySearchError: 未配置 Key、所有 Key 均失败，或遇到非重试类错误。
        """
        if not self._keys:
            raise TavilySearchError("未配置 Tavily API Key。")

        payload = {
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
            "include_favicon": True,
        }

        last_error: TavilySearchError | None = None
        for _ in range(len(self._keys)):
            key = await self._next_key()
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            }
            async with (
                aiohttp.ClientSession() as session,
                session.post(_SEARCH_URL, json=payload, headers=headers) as response,
            ):
                if response.status == 200:
                    data = await response.json()
                    return [
                        {
                            "title": str(item.get("title") or ""),
                            "url": str(item.get("url") or ""),
                            "content": str(item.get("content") or ""),
                        }
                        for item in data.get("results", [])
                    ]
                reason = await response.text()
                if response.status in _RETRYABLE_STATUSES:
                    last_error = TavilySearchError(
                        f"Tavily 搜索失败（HTTP {response.status}）：{reason[:200]}"
                    )
                    continue
                raise TavilySearchError(
                    f"Tavily 搜索失败（HTTP {response.status}）：{reason[:200]}"
                )

        if last_error is not None:
            raise last_error
        raise TavilySearchError("Tavily 搜索失败：所有配置的 Key 均已尝试但未成功。")


def format_results(results: list[dict[str, str]], show_source: bool) -> str:
    """把搜索结果格式化成给 LLM 阅读的文本。

    Args:
        results: :meth:`TavilyClient.search` 返回的归一化结果。
        show_source: 是否在每条结果后附带来源 URL。

    Returns:
        格式化后的纯文本。
    """
    lines: list[str] = []
    for idx, item in enumerate(results, 1):
        title = item.get("title") or "(无标题)"
        content = item.get("content") or ""
        lines.append(f"{idx}. {title}")
        if content:
            lines.append(f"   {content}")
        if show_source:
            url = item.get("url") or ""
            if url:
                lines.append(f"   [来源] {url}")
        lines.append("")
    return "\n".join(lines).strip()

"""Tavily 搜索客户端：多 Key 轮询 + HTTP 调用 + 结果格式化。

参考 AstrBot ``astrbot/core/tools/web_search_tools.py`` 的 ``_tavily_search`` /
``_KeyRotator`` 实现。
"""

from __future__ import annotations

import asyncio
import json

import aiohttp

_SEARCH_URL = "https://api.tavily.com/search"
# 这些 HTTP 状态码表示「当前 Key 有问题」，应换下一个 Key 重试。
# 401 未授权 / 403 禁用 / 429 限流 / 432 Tavily 配额耗尽。
_RETRYABLE_STATUSES: frozenset[int] = frozenset({401, 403, 429, 432})
# 单次请求总超时（含连接、发送、读取响应体）。Tavily 无响应时避免请求无限挂起。
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
# 抽取到的错误说明最大长度：只给 LLM 侧足够短的稳定信息，避免把原始响应体堆进上下文。
_MAX_ERROR_DETAIL = 120
# 无法从响应体取到字段时的固定兜底文案（不包含原始响应体内容）。
_ERROR_HINT_FALLBACK = "请求失败（响应体无法解析）"


async def _extract_error_detail(response: aiohttp.ClientResponse) -> str:
    """从非 200 响应里取一个简短、稳定的错误说明。

    Tavily 的原始响应体可能很长、含 HTML 或不可控内容，直接截断塞回上下文会污染
    LLM 侧信息。策略：优先取 JSON 响应里的 ``detail`` / ``error`` / ``message``
    短字段（截断到 ``_MAX_ERROR_DETAIL``）；取不到时回退为固定文案，只保留稳定的
    HTTP 错误码。
    """
    try:
        text = await response.text()
    except (aiohttp.ClientError, UnicodeDecodeError):
        return _ERROR_HINT_FALLBACK
    if not text:
        return "空响应体"
    try:
        data = json.loads(text)
    except ValueError:
        return _ERROR_HINT_FALLBACK
    if not isinstance(data, dict):
        return "请求失败（响应格式异常）"
    for key in ("detail", "error", "message"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_MAX_ERROR_DETAIL]
    error_obj = data.get("error")
    if isinstance(error_obj, dict):
        for key in ("message", "detail", "type"):
            value = error_obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:_MAX_ERROR_DETAIL]
    return "请求失败"


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
        # 一次 search() 调用复用一个 session，多 Key 重试也复用同一连接池。
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            for _ in range(len(self._keys)):
                key = await self._next_key()
                headers = {
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                }
                try:
                    async with session.post(
                        _SEARCH_URL, json=payload, headers=headers
                    ) as response:
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
                        reason = await _extract_error_detail(response)
                        if response.status in _RETRYABLE_STATUSES:
                            last_error = TavilySearchError(
                                f"Tavily 搜索失败（HTTP {response.status}）：{reason}"
                            )
                            continue
                        raise TavilySearchError(
                            f"Tavily 搜索失败（HTTP {response.status}）：{reason}"
                        )
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    # 网络异常 / 超时按「当前 Key 不可用」处理，换下一个 Key 重试。
                    last_error = TavilySearchError(f"Tavily 请求失败（网络异常或超时）：{exc}")
                    continue

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

"""Tavily 客户端：多 Key 轮询 + 搜索 / 正文提取 + 结果格式化。

搜索端点逻辑参考 AstrBot ``astrbot/core/tools/web_search_tools.py`` 的
``_tavily_search`` / ``_KeyRotator`` 实现。
"""

from __future__ import annotations

import asyncio
import json

import aiohttp

_SEARCH_URL = "https://api.tavily.com/search"
_EXTRACT_URL = "https://api.tavily.com/extract"
# 这些 HTTP 状态码表示「当前 Key 有问题」，应换下一个 Key 重试。
# 401 未授权 / 403 禁用 / 429 限流 / 432 Tavily 配额耗尽。
_RETRYABLE_STATUSES: frozenset[int] = frozenset({401, 403, 429, 432})
# 单次请求总超时（含连接、发送、读取响应体）。Tavily 无响应时避免请求无限挂起。
# 该值必须大于服务端自身的提取上限，否则客户端会先放弃、白白丢掉已经开始计费的请求：
# /extract 不传 timeout 时，服务端按 extract_depth 取默认值——basic 10 秒、advanced
# 30 秒（官方 API 文档）。取 45 秒即覆盖 advanced 的 30 秒上限，再留出连接与传输余量。
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=45)
# 抽取到的错误说明最大长度：只给 LLM 侧足够短的稳定信息，避免把原始响应体堆进上下文。
_MAX_ERROR_DETAIL = 120
# 无法从响应体取到字段时的固定兜底文案（不包含原始响应体内容）。
_ERROR_HINT_FALLBACK = "请求失败（响应体无法解析）"
# /extract 单批 URL 上限（服务端契约：1-20）。
MAX_EXTRACT_URLS = 20
# 截断提示里展示的 URL 最长字符数，避免超长 query string 撑爆输出。
_MAX_URL_LABEL = 100


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


class TavilyClientCache:
    """按 Key 列表复用 :class:`TavilyClient` 实例。

    轮询下标是 ``TavilyClient`` 的**实例状态**（``self._index``）。如果每次调用都新建
    客户端，下标永远从 0 开始，无错误时的「多 Key 轮询」会退化成永远只用第一个 Key。
    因此调用方必须复用同一实例，仅在 Key 列表变化（或配置更新）时重建。
    """

    def __init__(self) -> None:
        self._keys: tuple[str, ...] | None = None
        self._client: TavilyClient | None = None

    def get(self, keys: list[str]) -> TavilyClient:
        """返回当前 Key 列表对应的客户端；列表变化时重建。"""
        key_tuple = tuple(str(key).strip() for key in keys if str(key).strip())
        if self._client is None or key_tuple != self._keys:
            self._client = TavilyClient(list(key_tuple))
            self._keys = key_tuple
        return self._client

    def invalidate(self) -> None:
        """丢弃缓存（配置更新后调用，下次按新 Key 重建）。"""
        self._client = None
        self._keys = None


class TavilyClient:
    """带多 Key 轮询与 failover 的 Tavily 客户端。"""

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

    async def _post_json(self, url: str, payload: dict[str, object]) -> dict:
        """向 Tavily 端点 POST，返回 200 响应体。

        ``/search`` 与 ``/extract`` 共用同一套 Key 轮询与 failover 语义：凡
        ``_RETRYABLE_STATUSES`` 里的状态码、网络异常或超时，都视为「当前 Key 不可用」
        并换下一个 Key 重试；其余 HTTP 错误快速失败（重试也不会变好，白白多烧配额）。

        Args:
            url: Tavily 端点 URL。
            payload: 请求体。

        Returns:
            dict: 解析后的 JSON 响应体。

        Raises:
            TavilySearchError: 未配置 Key、所有 Key 均失败，或遇到非重试类错误。
        """
        if not self._keys:
            raise TavilySearchError("未配置 Tavily API Key。")

        last_error: TavilySearchError | None = None
        # 一次调用复用一个 session，多 Key 重试也复用同一连接池。
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            for _ in range(len(self._keys)):
                key = await self._next_key()
                headers = {
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                }
                try:
                    async with session.post(
                        url, json=payload, headers=headers
                    ) as response:
                        if response.status == 200:
                            return await response.json()
                        reason = await _extract_error_detail(response)
                        if response.status in _RETRYABLE_STATUSES:
                            last_error = TavilySearchError(
                                f"Tavily 请求失败（HTTP {response.status}）：{reason}"
                            )
                            continue
                        raise TavilySearchError(
                            f"Tavily 请求失败（HTTP {response.status}）：{reason}"
                        )
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    # 网络异常 / 超时按「当前 Key 不可用」处理，换下一个 Key 重试。
                    last_error = TavilySearchError(
                        f"Tavily 请求失败（网络异常或超时）：{exc}"
                    )
                    continue

        if last_error is not None:
            raise last_error
        raise TavilySearchError("Tavily 请求失败：所有配置的 Key 均已尝试但未成功。")

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
        payload = {
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
            "include_favicon": True,
        }
        data = await self._post_json(_SEARCH_URL, payload)
        return [
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "content": str(item.get("content") or ""),
            }
            for item in data.get("results", [])
        ]

    async def extract(
        self,
        urls: list[str],
        extract_depth: str = "basic",
        query: str = "",
    ) -> dict[str, list[dict[str, str]]]:
        """调用 Tavily Extract API，抓取指定 URL 的正文。

        ``/extract`` 的 HTTP 200 **不代表每个 URL 都抓到了**：抓到的进 ``results``，
        没抓到的进 ``failed_results``，所以两个数组都要回传给调用方。

        请求体不传 ``format``（服务端默认 markdown），也不传 ``chunks_per_source``
        ——后者仅在带 ``query`` 时可用，无 ``query`` 时带上会被判参数错误。

        Args:
            urls: 待抓取 URL 列表（1-20 条）。
            extract_depth: 提取深度，basic 或 advanced。
            query: 可选；给出后服务端按该意图重排片段，只回相关部分而非整页。

        Returns:
            ``{"results": [...], "failed": [...]}``；results 每条含 url/raw_content，
            failed 每条含 url/error。

        Raises:
            TavilySearchError: 未配置 Key、所有 Key 均失败，或遇到非重试类错误。
        """
        payload: dict[str, object] = {
            "urls": list(urls),
            "extract_depth": extract_depth,
        }
        if str(query or "").strip():
            payload["query"] = str(query).strip()

        data = await self._post_json(_EXTRACT_URL, payload)
        results = [
            {
                "url": str(item.get("url") or ""),
                "raw_content": str(item.get("raw_content") or ""),
            }
            for item in data.get("results", [])
        ]
        failed = [
            {
                "url": str(item.get("url") or ""),
                "error": str(item.get("error") or ""),
            }
            for item in data.get("failed_results", [])
        ]
        return {"results": results, "failed": failed}


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


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """按字符数截断正文，并尽量落在句子/段落边界上。

    边界只在窗口后半段生效：若最后一个句末标点出现在窗口前半段，照它切会白丢一半
    以上配额，此时退回硬上限切。截断点劈开一句话对 LLM 无害，丢掉大半正文才有害。

    Args:
        text: 原始正文。
        max_chars: 保留的最大字符数；``<= 0`` 表示不截断。

    Returns:
        tuple[str, bool]: ``(保留的文本, 是否发生了截断)``。
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False

    window = text[:max_chars]
    boundary = max(window.rfind("\n"), window.rfind("。"), window.rfind(". "))
    if boundary >= max_chars // 2:
        window = window[: boundary + 1]
    return window, True


def format_extract_results(
    results: list[dict[str, str]],
    failed: list[dict[str, str]],
    max_chars_per_url: int,
) -> str:
    """把正文提取结果格式化成给 LLM 阅读的文本。

    Args:
        results: :meth:`TavilyClient.extract` 返回的成功项（url/raw_content）。
        failed: :meth:`TavilyClient.extract` 返回的失败项（url/error）。
        max_chars_per_url: 每个 URL 保留的最大字符数；``<= 0`` 表示不截断。

    Returns:
        格式化后的纯文本。失败项只要存在就一定会体现出来——接口在「全部地址
        提取失败」时仍返回 HTTP 200，只报成功会让模型误以为拿到了内容。
    """
    lines: list[str] = []
    if results:
        lines.append(f"成功提取 {len(results)} 个网页的正文：")
        lines.append("")
        for idx, item in enumerate(results, 1):
            url = item.get("url") or "(未知地址)"
            content = item.get("raw_content") or ""
            lines.append(f"[{idx}] {url}")
            if not content.strip():
                lines.append("   （该页未返回正文内容）")
            else:
                kept, truncated = _truncate(content, max_chars_per_url)
                lines.append(kept.strip())
                if truncated:
                    lines.append(
                        f"   ……（正文过长，已截断至 {max_chars_per_url} 字符以内）"
                    )
            lines.append("")

    if failed:
        # 截断到展示长度：URL 可能带很长的 query string，塞进上下文没有价值。
        labels = [(item.get("url") or "(未知地址)")[:_MAX_URL_LABEL] for item in failed]
        lines.append(f"以下 {len(failed)} 个地址提取失败：")
        for label in labels:
            lines.append(f"- {label}")
        # 典型失败原因是站点反爬、需登录或链接失效；提示模型改走搜索而非反复重试。
        lines.append("（失败通常源于站点反爬、需登录或链接失效，可改用搜索获取信息）")

    if not lines:
        return "Tavily 没有返回任何提取结果。"
    return "\n".join(lines).strip()

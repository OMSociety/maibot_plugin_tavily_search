"""MaiBot Plugin: TavilySearch — Tavily 网页搜索工具

提供 tavily_search LLM 工具，调用 Tavily API 搜索网页。
参考 AstrBot ``astrbot/core/tools/web_search_tools.py`` 的 Tavily 实现。
"""

import logging
import re
from typing import Any, ClassVar, Literal

from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .tavily_client import (
    MAX_EXTRACT_URLS,
    TavilyClientCache,
    format_extract_results,
    format_results,
)

logger = logging.getLogger(__name__)

# URL 里的空白与分隔符：LLM 可能传数组，也可能把多个地址塞进一个字符串。
_URL_SPLIT_RE = re.compile(r"[,\s，、]+")


def _normalize_urls(raw: Any) -> tuple[list[str], str]:
    """把工具入参归一化成去重的 URL 列表。

    Args:
        raw: ``urls`` 参数；可能是字符串，也可能是字符串列表。

    Returns:
        tuple[list[str], str]: ``(URL 列表, 错误信息)``。错误信息非空时列表为 ``[]``。
    """
    if isinstance(raw, list):
        candidates = [str(item) for item in raw]
    else:
        candidates = _URL_SPLIT_RE.split(str(raw or ""))

    urls: list[str] = []
    for candidate in candidates:
        url = candidate.strip().strip("<>\"'")
        if url and url not in urls:
            urls.append(url)

    if not urls:
        return [], "请提供至少一个要抓取的网址。"
    if len(urls) > MAX_EXTRACT_URLS:
        return [], f"单次最多抓取 {MAX_EXTRACT_URLS} 个网址，当前收到 {len(urls)} 个。"

    # Tavily 只处理 http/https；先把非 http(s) 的入参拦下来，避免把无效地址送去消耗配额。
    invalid = [
        url for url in urls if not url.lower().startswith(("http://", "https://"))
    ]
    if invalid:
        shown = "、".join(invalid[:3])
        return [], f"只支持 http/https 网址，以下地址格式无效：{shown}"
    return urls, ""


# ============ 配置模型 ============


class PluginBaseConfig(PluginConfigBase):
    """插件基础配置"""

    __ui_label__ = "插件基础设置"
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en-US": {
            "title": "Plugin basic settings",
            "description": "Base configuration for the plugin",
        },
        "ja-JP": {
            "title": "プラグイン基本設定",
            "description": "プラグインの基本構成",
        },
    }

    config_version: str = Field(
        default="1.0.0",
        description="配置版本号",
        json_schema_extra={
            "label": "配置版本",
            "disabled": True,
            "i18n": {
                "en-US": {
                    "label": "Config version",
                    "hint": "Configuration version number",
                },
                "ja-JP": {
                    "label": "設定バージョン",
                    "hint": "設定のバージョン番号",
                },
            },
        },
    )
    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={
            "label": "启用插件",
            "i18n": {
                "en-US": {
                    "label": "Enable plugin",
                    "hint": "Whether to enable the plugin",
                },
                "ja-JP": {
                    "label": "プラグインの有効化",
                    "hint": "プラグインを有効にするかどうか",
                },
            },
        },
    )


class SearchConfig(PluginConfigBase):
    """搜索设置"""

    __ui_label__ = "搜索设置"
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en-US": {
            "title": "Search settings",
            "description": "Search settings",
        },
        "ja-JP": {
            "title": "検索設定",
            "description": "検索設定",
        },
    }

    tavily_api_key: list[str] = Field(
        default_factory=list,
        description="Tavily API Key（可添加多个 Key 进行轮询）",
        json_schema_extra={
            "label": "Tavily API Key",
            "hint": "可添加多个 Key 轮询",
            "placeholder": "tvly-xxxxxxxxxxxxxxx",
            "i18n": {
                "en-US": {
                    "label": "Tavily API Key",
                    "hint": "You can add multiple Key entries for round-robin rotation",
                    "placeholder": "tvly-xxxxxxxxxxxxxxx",
                },
                "ja-JP": {
                    "label": "Tavily の API キー",
                    "hint": "複数の Key を追加してラウンドロビンできます",
                    "placeholder": "tvly-xxxxxxxxxxxxxxx",
                },
            },
        },
    )
    show_source: bool = Field(
        default=True,
        description="是否在搜索结果中显示来源引用",
        json_schema_extra={
            "label": "显示来源引用",
            "hint": "开启后，每条搜索结果会附上 [来源] URL",
            "i18n": {
                "en-US": {
                    "label": "Show source citations",
                    "hint": "When enabled, each search result is appended with a [来源] URL",
                },
                "ja-JP": {
                    "label": "ソース引用の表示",
                    "hint": "有効にすると、各検索結果に [来源] URL が付きます",
                },
            },
        },
    )
    extract_depth: Literal["basic", "advanced"] = Field(
        default="basic",
        description="网页正文提取深度，basic 或 advanced",
        json_schema_extra={
            "label": "提取深度",
            "hint": "advanced 可提取表格与内嵌内容、成功率更高，代价是延迟与额度（约为 basic 的两倍）",
            "i18n": {
                "en-US": {
                    "label": "Extract depth",
                    "hint": "advanced also retrieves tables and embedded content with a higher success rate, at the cost of latency and credits (roughly twice basic)",
                },
                "ja-JP": {
                    "label": "抽出の深さ",
                    "hint": "advanced は表や埋め込みコンテンツも抽出でき成功率も上がりますが、遅延とクレジット消費（basic の約二倍）が増えます",
                },
            },
        },
    )
    max_chars_per_url: int = Field(
        default=6000,
        description="每个网址返回正文的最大字符数，0 表示不截断",
        json_schema_extra={
            "label": "单页正文字数上限",
            "hint": "超出部分会被截断，避免一次提取挤占上下文；0 表示不截断，1-499 按 500 处理",
            "i18n": {
                "en-US": {
                    "label": "Max characters per page",
                    "hint": "Excess content is truncated to keep one extraction from consuming the whole context; 0 means no truncation, and 1-499 is treated as 500",
                },
                "ja-JP": {
                    "label": "ページごとの最大文字数",
                    "hint": "超過分は切り詰められ、抽出がコンテキストを圧迫するのを防ぎます。0 は切り詰めなし、1-499 は 500 として扱われます",
                },
            },
        },
    )


class TavilySearchConfig(PluginConfigBase):
    """插件完整配置"""

    __ui_label__ = "Tavily 网页搜索"

    plugin: PluginBaseConfig = Field(
        default_factory=PluginBaseConfig, description="插件基础配置"
    )
    search: SearchConfig = Field(default_factory=SearchConfig, description="搜索设置")


# ============ 插件主类 ============


class TavilySearchPlugin(MaiBotPlugin):
    """Tavily 网页搜索插件"""

    config_model = TavilySearchConfig

    def __init__(self) -> None:
        super().__init__()
        # 复用同一个客户端实例，多 Key 轮询下标才能跨调用保留（见 TavilyClientCache）
        self._client_cache = TavilyClientCache()

    async def on_load(self) -> None:
        logger.info("TavilySearch 插件已加载")

    async def on_unload(self) -> None:
        logger.info("TavilySearch 插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict[str, Any], version: str
    ) -> None:
        if scope != "self":
            return
        # Key 可能已变，丢弃缓存的客户端，下次调用按新 Key 重建
        self._client_cache.invalidate()

    def _keys(self) -> list[str]:
        """返回去空白的已配置 Key 列表。"""
        return [
            str(key).strip()
            for key in self.config.search.tavily_api_key
            if str(key).strip()
        ]

    def _extract_depth(self, raw: Any) -> str:
        """归一化提取深度：只接受 basic / advanced，非法值回落到配置值。

        Args:
            raw: 工具调用传来的 ``extract_depth``，可能缺失或为任意类型。

        Returns:
            str: ``"basic"`` 或 ``"advanced"``。
        """
        value = str(raw or "").strip().lower()
        if value in {"basic", "advanced"}:
            return value
        # 配置文件可被手改，故配置值也过一遍校验，非法时兜底 basic。
        configured = str(self.config.search.extract_depth or "").strip().lower()
        return configured if configured in {"basic", "advanced"} else "basic"

    def _max_chars_per_url(self) -> int:
        """读取单页正文字数上限。

        Returns:
            int: 上限字符数；``0`` 表示不截断。配置非法时回落 6000；正数下限 500
            （更小的值会让正文只剩零头），负数按 500 处理而不是当成「不截断」。
        """
        try:
            value = int(self.config.search.max_chars_per_url)
        except (TypeError, ValueError):
            value = 6000
        if value == 0:
            return 0
        return max(500, value)

    @Tool(
        "tavily_search",
        description=(
            "使用 Tavily 搜索引擎搜索网页，获取最新信息、新闻和网页内容。"
            "当需要查询实时信息、事实、新闻或网页内容时调用。"
        ),
        brief_description="用 Tavily 搜索网页",
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="搜索关键词",
                required=True,
            ),
            ToolParameterInfo(
                name="max_results",
                param_type=ToolParamType.INTEGER,
                description="返回结果条数（5-20），默认 7",
                required=False,
            ),
            ToolParameterInfo(
                name="search_depth",
                param_type=ToolParamType.STRING,
                description="搜索深度，basic 或 advanced，默认 basic",
                required=False,
            ),
        ],
    )
    async def handle_tavily_search(self, query: str, **kwargs):
        keys = self._keys()
        if not keys:
            return {
                "success": False,
                "message": "未配置 Tavily API Key，请在插件配置的「搜索设置」里填写（可填多个 Key 轮询）。",
            }

        max_results = kwargs.get("max_results") or 7
        try:
            max_results = int(max_results)
        except (TypeError, ValueError):
            max_results = 7
        max_results = max(5, min(20, max_results))

        search_depth = str(kwargs.get("search_depth") or "basic").strip().lower()
        if search_depth not in {"basic", "advanced"}:
            search_depth = "basic"

        try:
            results = await self._client_cache.get(keys).search(
                query=query,
                max_results=max_results,
                search_depth=search_depth,
            )
        except Exception as e:  # noqa: BLE001 - 外部 API 错误统一转 LLM 可读信息
            return {"success": False, "message": f"Tavily 搜索失败：{e}"}

        if not results:
            return {"success": True, "content": "Tavily 没有返回任何搜索结果。"}

        content = format_results(results, self.config.search.show_source)
        return {"success": True, "content": content}

    @Tool(
        "tavily_extract",
        description=(
            "抓取指定网址的网页正文（Markdown）。当你已经有明确的 URL、"
            "需要该页面的完整内容而非搜索摘要时调用。"
            "仅支持公开可访问的 http/https 页面。"
        ),
        brief_description="抓取指定网址的网页正文",
        parameters=[
            ToolParameterInfo(
                name="urls",
                param_type=ToolParamType.STRING,
                description=(
                    "要抓取的网址；可传单个 URL，也可传多个 URL"
                    f"（用逗号或换行分隔，单次最多 {MAX_EXTRACT_URLS} 个）"
                ),
                required=True,
            ),
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="你关心的具体问题；给出后只返回与该问题最相关的片段，而非整页",
                required=False,
            ),
            ToolParameterInfo(
                name="extract_depth",
                param_type=ToolParamType.STRING,
                description="提取深度，basic 或 advanced；默认取插件配置",
                required=False,
                enum_values=["basic", "advanced"],
            ),
        ],
    )
    async def handle_tavily_extract(self, urls: Any, **kwargs):
        keys = self._keys()
        if not keys:
            return {
                "success": False,
                "message": "未配置 Tavily API Key，请在插件配置的「搜索设置」里填写（可填多个 Key 轮询）。",
            }

        url_list, error = _normalize_urls(urls)
        if error:
            return {"success": False, "message": error}

        try:
            outcome = await self._client_cache.get(keys).extract(
                urls=url_list,
                extract_depth=self._extract_depth(kwargs.get("extract_depth")),
                query=str(kwargs.get("query") or ""),
            )
        except Exception as e:  # noqa: BLE001 - 外部 API 错误统一转 LLM 可读信息
            return {"success": False, "message": f"Tavily 网页提取失败：{e}"}

        content = format_extract_results(
            outcome["results"],
            outcome["failed"],
            self._max_chars_per_url(),
        )
        return {"success": True, "content": content}


def create_plugin():
    return TavilySearchPlugin()

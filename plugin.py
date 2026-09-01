"""MaiBot Plugin: TavilySearch — Tavily 网页搜索工具

提供 tavily_search LLM 工具，调用 Tavily API 搜索网页。
参考 AstrBot ``astrbot/core/tools/web_search_tools.py`` 的 Tavily 实现。
"""

import logging
from typing import Any

from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .tavily_client import TavilyClient, format_results

logger = logging.getLogger(__name__)


# ============ 配置模型 ============


class PluginBaseConfig(PluginConfigBase):
    """插件基础配置"""

    __ui_label__ = "插件基础设置"

    config_version: str = Field(
        default="1.0.0",
        description="配置版本号",
        json_schema_extra={"label": "配置版本", "disabled": True},
    )
    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={"label": "启用插件"},
    )


class SearchConfig(PluginConfigBase):
    """搜索设置"""

    __ui_label__ = "搜索设置"

    tavily_api_key: list[str] = Field(
        default_factory=list,
        description="Tavily API Key（可添加多个 Key 进行轮询）",
        json_schema_extra={
            "label": "Tavily API Key",
            "hint": "可添加多个 Key 轮询",
            "placeholder": "tvly-xxxxxxxxxxxxxxx",
        },
    )
    show_source: bool = Field(
        default=True,
        description="是否在搜索结果中显示来源引用",
        json_schema_extra={
            "label": "显示来源引用",
            "hint": "开启后，每条搜索结果会附上 [来源] URL",
        },
    )


class TavilySearchConfig(PluginConfigBase):
    """插件完整配置"""

    __ui_label__ = "Tavily 网页搜索"

    plugin: PluginBaseConfig = Field(
        default_factory=PluginBaseConfig, description="插件基础配置"
    )
    search: SearchConfig = Field(
        default_factory=SearchConfig, description="搜索设置"
    )


# ============ 插件主类 ============


class TavilySearchPlugin(MaiBotPlugin):
    """Tavily 网页搜索插件"""

    config_model = TavilySearchConfig

    async def on_load(self) -> None:
        logger.info("TavilySearch 插件已加载")

    async def on_unload(self) -> None:
        logger.info("TavilySearch 插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict[str, Any], version: str
    ) -> None:
        if scope != "self":
            return

    def _keys(self) -> list[str]:
        """返回去空白的已配置 Key 列表。"""
        return [
            str(key).strip()
            for key in self.config.search.tavily_api_key
            if str(key).strip()
        ]

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
            results = await TavilyClient(keys).search(
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


def create_plugin():
    return TavilySearchPlugin()

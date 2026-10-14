"""插件层回归：同一个插件对象连续两次搜索必须轮到 k1、k2。

`plugin.py` 依赖 maibot-plugin-sdk，本机/CI 未必安装。本文件在导入插件模块前
注入一个只含「配置模型 + 装饰器」最小面的假 SDK，用来验证**插件把客户端实例
复用了**（旧实现每次搜索都新建 TavilyClient，轮询下标每次从 0 开始 → 永远只用
第一个 Key）。网络仍被打桩，插件与 TavilyClient 都是真代码。

用例导入的 SDK 桩只在本次导入期间存在于 sys.modules，结束后原样还原，
不会影响其它测试模块（如 test_config_i18n）。
"""

import asyncio
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT.parent))

PACKAGE = "maibot_plugin_tavily_search"


def _fake_sdk_modules():
    """构造最小的 maibot_sdk / maibot_sdk.types 替身。"""
    types_mod = types.ModuleType("maibot_sdk.types")

    class ToolParameterInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class ToolParamType:
        STRING = "string"
        INTEGER = "integer"

    types_mod.ToolParameterInfo = ToolParameterInfo
    types_mod.ToolParamType = ToolParamType

    sdk = types.ModuleType("maibot_sdk")

    def Field(default=None, *, default_factory=None, description="", **kwargs):
        return default_factory() if default_factory is not None else default

    class PluginConfigBase:
        pass

    class MaiBotPlugin:
        def __init__(self):
            self.config = None

    def Tool(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

    sdk.Field = Field
    sdk.PluginConfigBase = PluginConfigBase
    sdk.MaiBotPlugin = MaiBotPlugin
    sdk.Tool = Tool
    sdk.types = types_mod
    return {"maibot_sdk": sdk, "maibot_sdk.types": types_mod}


def _import_plugin_with_fake_sdk():
    """临时把假 SDK 放进 sys.modules 导入 plugin.py，之后还原现场。"""
    prefixes = ("maibot_sdk", PACKAGE)
    saved = {
        name: mod
        for name, mod in list(sys.modules.items())
        if name.split(".")[0] in prefixes
    }
    for name in saved:
        del sys.modules[name]
    sys.modules.update(_fake_sdk_modules())
    try:
        return importlib.import_module(f"{PACKAGE}.plugin")
    finally:
        for name in [n for n in list(sys.modules) if n.split(".")[0] in prefixes]:
            del sys.modules[name]
        sys.modules.update(saved)


class _FakeResponse:
    def __init__(self, status, payload):
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
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, url, json=None, headers=None):
        self.calls.append(headers.get("Authorization"))
        return self._responses.pop(0)


def _make_plugin(module, keys):
    plugin = module.TavilySearchPlugin()
    plugin.config = SimpleNamespace(
        search=SimpleNamespace(
            tavily_api_key=list(keys),
            show_source=False,
            extract_depth="basic",
            max_chars_per_url=6000,
        )
    )
    return plugin


def test_plugin_reuses_client_across_searches():
    """同一插件对象连续两次搜索 → Authorization 分别是 k1、k2。"""
    module = _import_plugin_with_fake_sdk()
    plugin = _make_plugin(module, ["k1", "k2"])

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

    async def run():
        return (
            await plugin.handle_tavily_search(query="q1"),
            await plugin.handle_tavily_search(query="q2"),
        )

    with patch("aiohttp.ClientSession", return_value=session):
        first, second = asyncio.run(run())

    assert session.calls == ["Bearer k1", "Bearer k2"]
    assert first["success"] is True and "a" in first["content"]
    assert second["success"] is True and "b" in second["content"]


def test_plugin_rebuilds_client_when_keys_change():
    """Key 列表变化后按新 Key 重建（下标从头开始），旧列表不残留。"""
    module = _import_plugin_with_fake_sdk()
    plugin = _make_plugin(module, ["k1", "k2"])

    session = _FakeSession(
        [
            _FakeResponse(200, {"results": []}),
            _FakeResponse(200, {"results": []}),
            _FakeResponse(200, {"results": []}),
        ]
    )

    async def run():
        await plugin.handle_tavily_search(query="q1")  # k1
        await plugin.handle_tavily_search(query="q2")  # k2
        plugin.config.search.tavily_api_key = ["k9"]  # 配置变更
        await plugin.handle_tavily_search(query="q3")  # k9

    with patch("aiohttp.ClientSession", return_value=session):
        asyncio.run(run())

    assert session.calls == ["Bearer k1", "Bearer k2", "Bearer k9"]


def test_plugin_config_update_invalidates_client():
    """on_config_update(scope=self) 之后必须丢弃缓存客户端（换新实例、下标归零）。

    只断言「下一次搜索用 k1」不足以判别：旧实现每次搜索都新建客户端，也是 k1。
    因此这里同时断言缓存里的实例对象确实被换掉——这一步在旧 plugin.py（根本没有
    _client_cache）上会直接 AttributeError，在「有缓存但不失效」的实现上会拿到同一
    个实例，两种回归都能测出。
    """
    module = _import_plugin_with_fake_sdk()
    plugin = _make_plugin(module, ["k1", "k2"])

    session = _FakeSession(
        [
            _FakeResponse(200, {"results": []}),
            _FakeResponse(200, {"results": []}),
            _FakeResponse(200, {"results": []}),
        ]
    )

    async def run():
        stale_client = plugin._client_cache.get(["k1", "k2"])
        await plugin.handle_tavily_search(query="q1")  # k1
        await plugin.handle_tavily_search(query="q2")  # k2（下标已推进）
        await plugin.on_config_update("self", {}, "1.0.0")
        assert plugin._client_cache.get(["k1", "k2"]) is not stale_client
        await plugin.handle_tavily_search(query="q3")  # 新实例下标归零 → k1

    with patch("aiohttp.ClientSession", return_value=session):
        asyncio.run(run())

    assert session.calls == ["Bearer k1", "Bearer k2", "Bearer k1"]


def test_plugin_config_update_ignores_other_scopes():
    """scope != self 的配置更新不能清缓存（否则每改别的插件都重置轮询）。"""
    module = _import_plugin_with_fake_sdk()
    plugin = _make_plugin(module, ["k1", "k2"])

    session = _FakeSession(
        [
            _FakeResponse(200, {"results": []}),
            _FakeResponse(200, {"results": []}),
        ]
    )

    async def run():
        client = plugin._client_cache.get(["k1", "k2"])
        await plugin.on_config_update("other", {}, "1.0.0")
        assert plugin._client_cache.get(["k1", "k2"]) is client
        await plugin.handle_tavily_search(query="q1")  # k1
        await plugin.handle_tavily_search(query="q2")  # k2 → 轮询未被重置

    with patch("aiohttp.ClientSession", return_value=session):
        asyncio.run(run())

    assert session.calls == ["Bearer k1", "Bearer k2"]


# ── tavily_extract 工具层 ────────────────────────────


class _ExtractSession(_FakeSession):
    """记录请求体，用于校验 extract 工具的入参归一化结果。"""

    def __init__(self, responses):
        super().__init__(responses)
        self.payloads = []

    def post(self, url, json=None, headers=None):
        self.payloads.append(json or {})
        return super().post(url, json=json, headers=headers)


def test_extract_tool_returns_content_and_failures():
    module = _import_plugin_with_fake_sdk()
    plugin = _make_plugin(module, ["k1"])

    session = _ExtractSession(
        [
            _FakeResponse(
                200,
                {
                    "results": [{"url": "https://a.com", "raw_content": "正文甲"}],
                    "failed_results": [{"url": "https://b.com", "error": "403"}],
                },
            )
        ]
    )

    with patch("aiohttp.ClientSession", return_value=session):
        result = asyncio.run(
            plugin.handle_tavily_extract(urls="https://a.com, https://b.com")
        )

    assert result["success"] is True
    assert "正文甲" in result["content"]
    assert "https://b.com" in result["content"]


def test_extract_tool_splits_and_dedupes_string_urls():
    """LLM 常把多个地址塞进一个字符串：逗号、中文顿号、空白都要能切，重复要去掉。"""
    module = _import_plugin_with_fake_sdk()
    plugin = _make_plugin(module, ["k1"])
    session = _ExtractSession([_FakeResponse(200, {"results": []})])

    async def run():
        await plugin.handle_tavily_extract(
            urls="https://a.com、https://b.com\nhttps://a.com"
        )

    with patch("aiohttp.ClientSession", return_value=session):
        asyncio.run(run())

    assert session.payloads[0]["urls"] == ["https://a.com", "https://b.com"]


def test_extract_tool_accepts_list_urls():
    module = _import_plugin_with_fake_sdk()
    plugin = _make_plugin(module, ["k1"])
    session = _ExtractSession([_FakeResponse(200, {"results": []})])

    async def run():
        await plugin.handle_tavily_extract(urls=["https://a.com", "https://b.com"])

    with patch("aiohttp.ClientSession", return_value=session):
        asyncio.run(run())

    assert session.payloads[0]["urls"] == ["https://a.com", "https://b.com"]


def test_extract_tool_rejects_non_http_urls_without_calling_api():
    """非 http(s) 入参在本地拦下，不消耗配额也不发请求。"""
    module = _import_plugin_with_fake_sdk()
    plugin = _make_plugin(module, ["k1"])
    session = _ExtractSession([])

    with patch("aiohttp.ClientSession", return_value=session):
        result = asyncio.run(plugin.handle_tavily_extract(urls="file:///C:/secret.txt"))

    assert result["success"] is False
    assert "http" in result["message"]
    assert session.payloads == []


def test_extract_tool_rejects_empty_urls():
    module = _import_plugin_with_fake_sdk()
    plugin = _make_plugin(module, ["k1"])

    result = asyncio.run(plugin.handle_tavily_extract(urls="  "))

    assert result["success"] is False
    assert "至少一个" in result["message"]


def test_extract_tool_rejects_too_many_urls():
    module = _import_plugin_with_fake_sdk()
    plugin = _make_plugin(module, ["k1"])

    urls = [f"https://a{i}.com" for i in range(21)]
    result = asyncio.run(plugin.handle_tavily_extract(urls=urls))

    assert result["success"] is False
    assert "20" in result["message"]


def test_extract_tool_without_keys_returns_hint():
    module = _import_plugin_with_fake_sdk()
    plugin = _make_plugin(module, [])

    result = asyncio.run(plugin.handle_tavily_extract(urls="https://a.com"))

    assert result["success"] is False
    assert "API Key" in result["message"]


def test_extract_tool_falls_back_on_invalid_depth():
    module = _import_plugin_with_fake_sdk()
    plugin = _make_plugin(module, ["k1"])
    session = _ExtractSession([_FakeResponse(200, {"results": []})])

    async def run():
        await plugin.handle_tavily_extract(urls="https://a.com", extract_depth="deep")

    with patch("aiohttp.ClientSession", return_value=session):
        asyncio.run(run())

    assert session.payloads[0]["extract_depth"] == "basic"


def test_extract_tool_uses_config_depth():
    """工具没传 depth 时必须取配置值，而不是硬编码 basic。"""
    module = _import_plugin_with_fake_sdk()
    plugin = _make_plugin(module, ["k1"])
    plugin.config.search.extract_depth = "advanced"
    session = _ExtractSession([_FakeResponse(200, {"results": []})])

    async def run():
        await plugin.handle_tavily_extract(urls="https://a.com")

    with patch("aiohttp.ClientSession", return_value=session):
        asyncio.run(run())

    assert session.payloads[0]["extract_depth"] == "advanced"


def test_extract_tool_reports_api_failure():
    module = _import_plugin_with_fake_sdk()
    plugin = _make_plugin(module, ["k1"])
    session = _ExtractSession([_FakeResponse(500, {})])

    with patch("aiohttp.ClientSession", return_value=session):
        result = asyncio.run(plugin.handle_tavily_extract(urls="https://a.com"))

    assert result["success"] is False
    assert "提取失败" in result["message"]


def test_extract_tool_truncates_per_configured_limit():
    """配置的字符上限要真正落到输出上，而不是只在文档里写着。"""
    module = _import_plugin_with_fake_sdk()
    plugin = _make_plugin(module, ["k1"])
    plugin.config.search.max_chars_per_url = 600
    session = _ExtractSession(
        [
            _FakeResponse(
                200,
                {"results": [{"url": "https://a.com", "raw_content": "字" * 5000}]},
            )
        ]
    )

    with patch("aiohttp.ClientSession", return_value=session):
        result = asyncio.run(plugin.handle_tavily_extract(urls="https://a.com"))

    body = next(
        line for line in result["content"].splitlines() if line.startswith("字")
    )
    assert len(body) <= 600
    assert "已截断" in result["content"]


def test_extract_tool_invalid_max_chars_config_falls_back():
    """配置被手改成非法值时不能抛异常穿透到工具层。"""
    module = _import_plugin_with_fake_sdk()
    plugin = _make_plugin(module, ["k1"])
    plugin.config.search.max_chars_per_url = "not-a-number"
    session = _ExtractSession(
        [
            _FakeResponse(
                200,
                {"results": [{"url": "https://a.com", "raw_content": "字" * 8000}]},
            )
        ]
    )

    with patch("aiohttp.ClientSession", return_value=session):
        result = asyncio.run(plugin.handle_tavily_extract(urls="https://a.com"))

    assert result["success"] is True
    assert "已截断" in result["content"]


def test_max_chars_negative_is_not_treated_as_no_truncation():
    """负数只能是配置笔误，0 才是「不截断」；负数按最小可用长度处理。"""
    module = _import_plugin_with_fake_sdk()
    plugin = _make_plugin(module, ["k1"])
    plugin.config.search.max_chars_per_url = -100

    assert plugin._max_chars_per_url() == 500


def test_max_chars_zero_disables_truncation():
    module = _import_plugin_with_fake_sdk()
    plugin = _make_plugin(module, ["k1"])
    plugin.config.search.max_chars_per_url = 0

    assert plugin._max_chars_per_url() == 0


def test_extract_tool_accepts_exactly_max_urls():
    """正好 20 条是契约允许的上限，必须通过；只有 21 条才拒绝。"""
    module = _import_plugin_with_fake_sdk()
    plugin = _make_plugin(module, ["k1"])
    session = _ExtractSession([_FakeResponse(200, {"results": []})])

    urls = [f"https://a{i}.com" for i in range(20)]

    with patch("aiohttp.ClientSession", return_value=session):
        result = asyncio.run(plugin.handle_tavily_extract(urls=urls))

    assert result["success"] is True
    assert session.payloads[0]["urls"] == urls


def test_normalize_urls_splits_merged_entries_inside_list():
    """LLM 常把多个地址塞进数组的同一个元素里，元素内部也要切分。"""
    module = _import_plugin_with_fake_sdk()

    for raw in (
        ["https://a.com、https://b.com"],
        ["https://a.com,https://b.com"],
        ["https://a.com\nhttps://b.com"],
    ):
        urls, error = module._normalize_urls(raw)
        assert error == "", raw
        assert urls == ["https://a.com", "https://b.com"], raw


def test_normalize_urls_accepts_tuple():
    """tuple 入参应被当成序列处理，而不是 str() 之后按分隔符切坏。"""
    module = _import_plugin_with_fake_sdk()

    urls, error = module._normalize_urls(("https://a.com", "https://b.com"))

    assert error == ""
    assert urls == ["https://a.com", "https://b.com"]


def test_normalize_urls_keeps_single_url_with_commas_intact():
    """合法 URL 自带逗号（query 参数）时不能被误切。"""
    module = _import_plugin_with_fake_sdk()
    url = "https://a.com/search?q=x,y&lang=zh"

    urls, error = module._normalize_urls([url])

    assert error == ""
    assert urls == [url]


def test_normalize_urls_splits_semicolon_separated_string():
    """分号也应视为分隔符（LLM 有时用它并列多个地址）。"""
    module = _import_plugin_with_fake_sdk()

    urls, error = module._normalize_urls("https://a.com;https://b.com")

    assert error == ""
    assert urls == ["https://a.com", "https://b.com"]

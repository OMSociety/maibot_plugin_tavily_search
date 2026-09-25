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
        for name in [
            n for n in list(sys.modules) if n.split(".")[0] in prefixes
        ]:
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
        search=SimpleNamespace(tavily_api_key=list(keys), show_source=False)
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

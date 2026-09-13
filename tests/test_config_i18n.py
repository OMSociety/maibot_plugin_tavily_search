"""配置界面 i18n 回归测试：schema 覆盖 + 翻译纪律（en-US / ja-JP）。

需要 maibot-plugin-sdk（仅用于离线生成配置 schema，不起宿主）；
未安装 SDK 的环境自动跳过，不让测试挂掉。
"""

import re
import sys
from pathlib import Path

import pytest

# 让测试在任意 cwd 下都能找到插件包（D:\WorkSpace）与插件内模块
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_PLUGIN_ROOT), str(_PLUGIN_ROOT.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import maibot_plugin_tavily_search.plugin as plugin_module
    from maibot_sdk.config import generate_plugin_config_schema
except ImportError:  # pragma: no cover - 无 SDK 环境直接跳过
    pytest.skip("maibot_sdk not available", allow_module_level=True)

LOCALES = ["en-US", "ja-JP"]
FIELD_KEYS = ("label", "hint", "placeholder")
SECTION_KEYS = ("title", "description")

# 允许的「token 丢失」白名单：{(字段路径, locale, token): 理由}
TOKEN_WHITELIST = {
    # "Tavily API Key" -> "Tavily の API キー"：Key 按术语表译作 APIキー 的 キー
    ("search.tavily_api_key", "ja-JP", "Key"),
}


@pytest.fixture(scope="module")
def schema():
    return generate_plugin_config_schema(
        plugin_module.TavilySearchConfig, plugin_id="omsociety.tavily-search"
    )


def _iter_translations(schema):
    """产出 (路径, locale, 键, base 文本, 译文)，覆盖字段与配置节。"""
    for sec_name, sec in schema["sections"].items():
        sec_i18n = sec.get("i18n") or {}
        for key in SECTION_KEYS:
            if sec.get(key):
                for loc in LOCALES:
                    yield (
                        sec_name,
                        loc,
                        key,
                        sec[key],
                        (sec_i18n.get(loc) or {}).get(key, ""),
                    )
        for f_name, field in sec.get("fields", {}).items():
            path = f"{sec_name}.{f_name}"
            f_i18n = field.get("i18n") or {}
            for key in FIELD_KEYS:
                if field.get(key):
                    for loc in LOCALES:
                        yield (
                            path,
                            loc,
                            key,
                            field[key],
                            (f_i18n.get(loc) or {}).get(key, ""),
                        )
                continue
                # 无 json hint 但有 description 时，i18n.hint 承担 description 的翻译
            if key == "hint" and field.get("description"):
                for loc in LOCALES:
                    yield (
                        path,
                        loc,
                        key,
                        field["description"],
                        (f_i18n.get(loc) or {}).get(key, ""),
                    )


def _checkable_translations(schema):
    """只产出 base 与译文都存在的条目（覆盖率断言单独做）。"""
    for item in _iter_translations(schema):
        path, loc, key, base_text, trans_text = item
        if base_text and trans_text:
            yield item


# ---------- 覆盖率 ----------


def test_all_fields_and_sections_have_i18n(schema):
    """凡 base 含 label/hint/placeholder（或 title/description）者，全 locale 必须覆盖。"""
    missing = []
    for path, loc, key, base_text, trans_text in _iter_translations(schema):
        if base_text and not trans_text:
            missing.append((path, loc, key))
    assert missing == [], f"i18n 覆盖缺口: {missing}"


def test_i18n_locales_complete(schema):
    """每个带 i18n 的字段/节都必须同时含 en-US 与 ja-JP。"""
    bad = []
    for sec_name, sec in schema["sections"].items():
        for i18n in [sec.get("i18n")] + [
            f.get("i18n") for f in sec.get("fields", {}).values() if f.get("i18n")
        ]:
            if i18n is not None and not all(loc in i18n for loc in LOCALES):
                bad.append(i18n)
    assert bad == []


# ---------- 翻译纪律 ----------


def test_digits_preserved(schema):
    """译文中的数字集合必须与 base 完全一致。"""
    for path, loc, key, base_text, trans_text in _checkable_translations(schema):
        assert re.findall(r"\d+(?:\.\d+)?", base_text) == re.findall(
            r"\d+(?:\.\d+)?", trans_text
        ), f"{path}[{loc}].{key} 数字不一致"


def test_identifier_tokens_preserved(schema):
    """技术标识 token（snake_case / UPPER / 产品名）不得在译文中丢失。"""
    for path, loc, key, base_text, trans_text in _checkable_translations(schema):
        base_tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", base_text))
        trans_tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", trans_text))
        lost = {
            t
            for t in base_tokens - trans_tokens
            if (path, loc, t) not in TOKEN_WHITELIST
        }
        assert not lost, f"{path}[{loc}].{key} 丢失 token: {lost}"


def test_ja_long_kanji_runs_have_kana(schema):
    """ja 译文里连续汉字 >=5 且无假名的，视为可疑（需人工确认为合法复合词）。"""
    suspicious = []
    for path, loc, key, _base, trans_text in _checkable_translations(schema):
        if loc != "ja-JP":
            continue
        if re.search(r"[\u4e00-\u9fff]{5,}", trans_text) and not re.search(
            r"[\u3040-\u30ff]", trans_text
        ):
            suspicious.append((path, key, trans_text))
    assert suspicious == []


def test_ja_not_identical_to_zh_base(schema):
    """ja 译文与 base 中文完全相等的需人工确认；纯技术串（无 CJK）自动豁免。"""
    identical = []
    for path, loc, key, base_text, trans_text in _checkable_translations(schema):
        if (
            loc == "ja-JP"
            and trans_text == base_text
            and re.search(r"[\u4e00-\u9fff]", base_text)
        ):
            identical.append((path, key, trans_text))
    assert identical == []

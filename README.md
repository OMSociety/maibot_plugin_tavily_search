<div align="center">

<img src="https://raw.githubusercontent.com/OMSociety/maibot_plugin_tavily_search/main/logo.png" width="120" alt="Tavily Search Logo" />

# Tavily 网页搜索

**Tavily 网页搜索工具** —— 实时信息检索 · 多 Key 轮询 · 来源引用 · 搜索深度可控

[![Version](https://img.shields.io/badge/version-1.1.0-blue.svg)](https://github.com/OMSociety/maibot_plugin_tavily_search)
[![MaiBot](https://img.shields.io/badge/MaiBot-%E2%89%A51.0-green.svg)](https://github.com/Mai-with-u/MaiBot)
[![License](https://img.shields.io/badge/license-AGPL--3.0-orange.svg)](LICENSE)
[![Stars](https://img.shields.io/github/stars/OMSociety/maibot_plugin_tavily_search)](https://github.com/OMSociety/maibot_plugin_tavily_search/stargazers)
[![Issues](https://img.shields.io/github/issues/OMSociety/maibot_plugin_tavily_search)](https://github.com/OMSociety/maibot_plugin_tavily_search/issues)

[核心特性](#核心特性) • [功能概览](#功能概览) • [快速开始](#快速开始) • [配置项说明](#配置项说明) • [LLM 可调用工具](#llm-可调用工具) • [常见问题](#常见问题) • [更新日志](CHANGELOG.md)

</div>

> 本项目由 AI 编写 · Tavily 搜索逻辑参考 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 内置的 `web_search_tools.py`

---

## 核心特性

| 特性 | 说明 |
|------|------|
| **实时信息检索** | 调用 Tavily API 搜索网页，获取最新信息、新闻与网页内容 |
| **多 Key 轮询** | 支持配置多个 API Key 自动轮询，配额耗尽 / 限流时自动切换下一个 Key |
| **来源引用** | 可选在每条结果附上来源 URL，方便追溯信息出处 |
| **搜索深度可控** | 支持 `basic` / `advanced` 两种深度、5-20 条结果数 |

---

## 功能概览

### tavily_search
一个 LLM 工具：模型在需要查询实时信息、事实、新闻或网页内容时自动调用，返回标题 + 摘要（开启来源引用时附带 URL）。

---

## 快速开始

### 第一步：安装

MaiBot WebUI → 插件市场 → 搜索 `tavily`

### 第二步：配置 Tavily API Key（必需）

> **提示：**不配置 Key 时工具会返回友好提示，不会报错崩溃。

在插件配置的「搜索设置」分组中填写 Tavily API Key。Key 在 [Tavily 官网](https://app.tavily.com/home) 获取，可添加多个 Key 进行轮询。

### 第三步：重启生效

配置完成后在 WebUI 重载插件（或重启 MaiBot），即可用自然语言让 bot 搜索。

---

## 配置项说明

| 配置项 | 类型 | 默认值 | 说明 |
|:------|:-----|:-------|:-----|
| `tavily_api_key` | list | `[]` | Tavily API Key，可添加多个 Key 进行轮询 |
| `show_source` | bool | `true` | 是否在搜索结果中显示来源引用（URL） |

### 快速配置模板

在 MaiBot WebUI 插件配置面板填写，或参考以下 `config.toml` 结构（插件目录下，首次加载自动生成）：

```toml
[plugin]
config_version = "1.0.0"
enabled = true

[search]
tavily_api_key = ["你的Key1", "你的Key2"]
show_source = true
```

> **多 Key 轮询**：遇到 401 / 403 / 429 / 432（Key 无效、限流、配额耗尽）会自动换下一个 Key 重试，其余错误快速失败。

---

## LLM 可调用工具

插件注册 1 个 LLM 工具，模型会自动判断何时调用，你只需用自然语言说需求：

```
用户: 帮我搜一下最近 DeepSeek 有什么新闻
🤖 → tavily_search(query="DeepSeek 新闻")
    1. DeepSeek 发布新版本……
       ……
       [来源] https://example.com/news
```

### tavily_search
使用 Tavily 搜索引擎搜索网页，获取最新信息、新闻和网页内容。

| 参数 | 类型 | 必填 | 说明 |
|:----|:----|:----:|:-----|
| `query` | string | 是 | 搜索关键词 |
| `max_results` | integer | 否 | 返回结果条数，5-20，默认 7 |
| `search_depth` | string | 否 | 搜索深度，`basic` 或 `advanced`，默认 `basic` |

---

## 常见问题

**Q：需要配置吗？**
A：**需要**。请先在配置的「搜索设置」里填写 Tavily API Key（获取方式见[快速开始](#快速开始)），否则工具会提示未配置。

**Q：来源引用是什么？**
A：开启 `show_source` 后，每条搜索结果会额外附上一行 `[来源] <URL>`，方便追溯出处；关闭后只返回标题和摘要。

**Q：`basic` 和 `advanced` 有什么区别？**
A：`advanced` 会返回更丰富、更深入的内容，但耗时和 token 消耗更高；日常查询用默认的 `basic` 即可。

## 支持与致谢

如果这个插件对你有帮助，欢迎点亮 Star，有问题和建议请提交 [Issue](https://github.com/OMSociety/maibot_plugin_tavily_search/issues) 或 [Pull Request](https://github.com/OMSociety/maibot_plugin_tavily_search/pulls)。

- [MaiBot](https://github.com/Mai-with-u/MaiBot) 开源聊天机器人框架
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) 上游 Tavily 搜索实现参考
- [Tavily](https://tavily.com/) 搜索引擎 API

## 许可证与作者

本项目采用 **AGPL-3.0** 开源协议。

[@OMSociety](https://github.com/OMSociety)

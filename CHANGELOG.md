# Changelog

本项目的更改记录在此文件。

All notable changes to this project are documented in this file.

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-10-15

### 新增

- **`tavily_extract` 工具：抓取指定网址的网页正文**。给定具体 URL（单个或多个，逗号 / 换行分隔，单次最多 20 个）直接取回该页正文，补齐搜索摘要之外的信息；可传 `query` 让 Tavily 按问题重排片段、只回相关部分，也可选 `basic` / `advanced` 提取深度（`advanced` 可提取表格与内嵌内容、成功率更高，官方计费约为 `basic` 的两倍）。抓取失败的地址单独列出并提示模型改用搜索，因为接口在「全部抓取失败」时仍返回 HTTP 200。
- **新增配置项** `extract_depth`（默认 `basic`）与 `max_chars_per_url`（默认 `6000`，`0` 表示不截断）：单页正文超出上限时在句末边界截断并标注，避免一次提取吃光上下文。
- 新增 29 条回归测试：正文提取的成功 / 部分失败 / 参数校验 / 非 http 网址拦截 / 多 Key failover，以及截断与失败提示的格式化行为。

### 变更

- 搜索与提取共用同一套网络层（Key 轮询、可重试状态码、超时换 Key），提取端点因此自动获得与搜索一致的多 Key 容错。
- 单次请求总超时由 15 秒放宽到 45 秒：`/extract` 的深度提取在服务端最长可跑 30 秒（官方默认上限），客户端超时若与之相等就会先放弃，白白丢掉已经开始计费的请求。
- 提取深度配置项本身会做校验，非法值回落 `basic` 而不是原样传给接口。

### Added

- **`tavily_extract` tool: fetch the full text of given URLs**. Pass one or many URLs (comma/newline separated, up to 20 per call) to retrieve the page body, covering what search snippets leave out. An optional `query` reranks extracted chunks by relevance, and `extract_depth` selects `basic` or `advanced` extraction. Failed URLs are listed separately with a hint to fall back to search, since the API still returns HTTP 200 when every URL fails.
- **New configuration options** `extract_depth` (default `basic`) and `max_chars_per_url` (default `6000`, `0` disables truncation): oversized page bodies are cut at a sentence boundary and annotated, so a single extraction cannot consume the whole context.
- 29 new regression tests covering extract success, partial failure, parameter validation, non-http URL rejection, multi-key failover, plus truncation and failure-report formatting.

### Changed

- Search and extract now share one network layer (key rotation, retryable status codes, key switch on timeout), so the extract endpoint inherits the same multi-key tolerance as search.
- The per-request total timeout was raised from 15 to 45 seconds: deep `/extract` runs can take up to 30 seconds server-side (the documented default cap), so a client timeout at that same value would give up first and waste an already-billed request.
- The extraction depth setting is validated, falling back to `basic` instead of being passed through when it holds an invalid value.

## [1.1.1] - 2026-09-25

### 修复

- **多 Key 轮询恢复生效**：客户端实例在插件生命周期内复用（Key 变化或配置更新才重建）。此前每次搜索都新建实例，轮询下标恒为 0，只有第一个 Key 承接全部流量，其余仅在失败时兜底。

### Fixed

- **Multi-key polling works again**: the client instance is reused for the lifetime of the plugin (it is rebuilt only when the keys change or the configuration is updated). Previously a new instance was created on every search, so the round-robin index stayed at 0 and only the first key handled all the traffic, with the rest used only as a fallback on failure.

## [1.1.0] - 2026-09-14

### 新增

- **配置界面多语言（en-US / ja-JP）**：WebUI 配置页的字段标题、提示与占位文本、分区标题与分区描述按界面语言自动切换（宿主界面语言支持中文/英文/日文/韩文，无对应译文时回退中文），manifest 声明 `supported_locales`。
- 新增配置界面 i18n 回归测试：离线生成配置 Schema，校验各语言覆盖率、数值一致与技术标识符保全。

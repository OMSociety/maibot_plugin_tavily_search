# Changelog

本项目的更改记录在此文件。

All notable changes to this project are documented in this file.

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.1] - 2026-10-15

### 修复

- **搜索不再沿用提取端点的 45 秒超时**：两个端点分开取超时值——`/search` 用 15 秒、`/extract` 用 45 秒。此前共用的 45 秒是为深度提取放宽的，搜索路径一并被拉长，单 Key 挂起时最坏阻塞从 `15×N` 秒变成 `45×N` 秒（N 为 Key 数）。
- **错误文案不再出现重复前缀**：网络层只抛中性细节（`HTTP 500：…`），「Tavily 搜索失败」「Tavily 网页提取失败」由各自的调用方拼一次。此前搜索失败会输出 `Tavily 搜索失败：Tavily 请求失败（HTTP 500）：…` 这类叠加前缀。
- **提取失败原因回传给模型**：`failed_results` 里的 `error` 现在附在地址之后（此前只列 URL），模型可据此区分站点反爬 / 需登录与链接失效。
- **数组入参里的多个地址会被正确拆分**：LLM 把多个 URL 塞进同一个数组元素（如 `["https://a.com、https://b.com"]`）时，此前整串被当作一个地址送去消耗配额；现在数组元素内部同样按分隔符拆分，元组入参也按序列处理。URL 自带的逗号（query 参数）不会被误切。

### 变更

- 单批 URL 上限 20 的边界补上正例：正好 20 个此前未被测试覆盖，`>` 误写成 `>=` 会静默拒绝合法请求。
- 新增 13 条回归测试：搜索 / 提取超时分别落到 session、432 与 403 的 Key 轮询、错误文案无重复前缀、失败原因回传、URL 分割与 20 条边界。

### Fixed

- **Search no longer inherits the extract endpoint's 45-second timeout**: the two endpoints now carry separate timeouts — 15 seconds for `/search`, 45 seconds for `/extract`. The shared 45-second value was widened for deep extraction, which also stretched the search path; a hung key blocked the reply chain for up to `45×N` seconds (N = number of keys) instead of `15×N`.
- **Error messages no longer repeat their prefix**: the network layer raises neutral details (`HTTP 500: …`) only, and the callers prepend "Tavily search failed" / "Tavily extraction failed" once. Search failures previously rendered as stacked prefixes such as `Tavily 搜索失败：Tavily 请求失败（HTTP 500）：…`.
- **Extraction failure reasons reach the model**: the `error` field of `failed_results` is now appended after each URL (previously only the URL was listed), so the model can tell a blocked/login-walled page from a dead link.
- **Multiple URLs inside one array element are split correctly**: when the LLM packs several URLs into a single array element (e.g. `["https://a.com、https://b.com"]`), the whole string used to be sent as one address and burn quota; array elements are now split on the same separators and tuple inputs are treated as sequences. Commas that are part of a valid URL (query parameters) are not split.

### Changed

- The 20-URL batch limit now has a positive test: an exact batch of 20 was previously uncovered, so mistyping `>` as `>=` would silently reject valid requests.
- 13 new regression tests covering per-endpoint timeouts reaching the session, key rotation on 432 and 403, absence of duplicated error prefixes, failure-reason passthrough, URL splitting, and the 20-URL boundary.

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

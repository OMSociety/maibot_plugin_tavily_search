# Changelog

本项目的更改记录在此文件。

All notable changes to this project are documented in this file.

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.1] - 2026-09-25

### 修复

- **多 Key 轮询恢复生效**：客户端实例在插件生命周期内复用（Key 变化或配置更新才重建）。此前每次搜索都新建实例，轮询下标恒为 0，只有第一个 Key 承接全部流量，其余仅在失败时兜底。

### Fixed

- **Multi-key polling works again**: the client instance is reused for the lifetime of the plugin (it is rebuilt only when the keys change or the configuration is updated). Previously a new instance was created on every search, so the round-robin index stayed at 0 and only the first key handled all the traffic, with the rest used only as a fallback on failure.

## [1.1.0] - 2026-09-14

### 新增

- **配置界面多语言（en-US / ja-JP）**：WebUI 配置页的字段标题、提示与占位文本、分区标题与分区描述按界面语言自动切换（宿主界面语言支持中文/英文/日文/韩文，无对应译文时回退中文），manifest 声明 `supported_locales`。
- 新增配置界面 i18n 回归测试：离线生成配置 Schema，校验各语言覆盖率、数值一致与技术标识符保全。

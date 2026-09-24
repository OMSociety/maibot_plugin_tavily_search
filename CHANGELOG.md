# Changelog

本项目所有重要更改都会记录在此文件。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.1.0] - 2026-09-14

### 新增 (Added)

- **配置界面多语言（en-US / ja-JP）**：WebUI 配置页的字段标题、提示与占位文本、分区标题与分区描述按界面语言自动切换（宿主界面语言支持中文/英文/日文/韩文，无对应译文时回退中文），manifest 声明 `supported_locales`。
- 新增配置界面 i18n 回归测试：离线生成配置 Schema，校验各语言覆盖率、数值一致与技术标识符保全。

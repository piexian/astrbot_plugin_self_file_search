# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [1.0.3] - 2026-10-13

- 修正插件元数据字段名以符合 AstrBot 规范（`description` → `desc`，`repository` → `repo`），补充 `short_desc` 与 `astrbot_version`。

## [1.0.2] - 2026-10-13

- 搜索支持排除模式，命中文件按访问频率排序。
- 读取支持指定行区间，截断时给出精确续读参数与剩余量。
- 新增插件 Logo 与调试日志。

## [1.0.1] - 2026-10-11

- metadata 补充 `repository` 字段。

## [1.0.0] - 2026-10-11

- 首个版本：6 个 LLM 工具，文本级 `self_search` / `self_read` / `self_glob`，语义级 `self_symbols` / `self_refs` / `self_def`。
- 数据源条目化配置，可并入 AstrBot 自身白名单目录。
- 通用 LSP 后端，从环境 PATH 自动探测语言服务器。
- 只读、输出限长、目录白名单校验。

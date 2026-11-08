# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。



## [1.1.0] - 2026-11-09

- 按服务器和文件发现项目根目录，`self_symbols` 支持可选 `path` 建立文件上下文。
- 新增 `lsp_initialization`，补齐 workspace 能力声明与服务端配置/进度/工作区请求响应。
- Windows 隐藏控制台并在强制关闭时清理进程树；启动或进程故障后熔断，避免反复崩溃。
- 单元测试覆盖 Linux、Windows、macOS，包含 Windows 带空格 `.cmd` 启动回归。
## [1.0.4] - 2026-11-09

- 修复 `file_glob` 相对路径匹配，`self_search` / `self_glob` 支持逗号分隔的多个模式。
- 空结果区分无候选文件、文件内无命中、扫描不完整和分页越界。
- `csharp-ls` 缺少项目文件时明确报错，进程异常保留 stderr 摘要；修复关闭流程与管道资源释放。
## [1.0.3] - 2026-10-13

- 修正插件元数据字段名以符合 AstrBot 规范（`description` → `desc`，`repository` → `repo`），补充 `short_desc` 与 `astrbot_version`。
- 新增 CI：ruff 格式与静态检查、插件元数据校验，规则集与 AstrBot 本体保持一致。

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

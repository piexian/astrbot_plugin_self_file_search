# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [1.0.2] - 2026-10-13

### Added
- `self_search` 新增 `exclude_glob`：逗号分隔的排除模式，支持 `!` 前缀与 `目录/` 形式，比反复收窄 `file_glob` 高效。
- `self_search` 的 `files` 模式新增 frecency 排序：按「访问频率 + 24 小时半衰」打分，常查文件排前面并标注 `[常看的文件]` / `[看过的文件]`；`content` / `count` 模式保持源码顺序不受影响。
- 截断提示新增剩余量与页数：`self_read` 报「还剩多少行 / 约多少字符 / 按当前 max_chars 估计还需几次」，`self_search` 报「还剩多少条、估计还需几页」，`self_glob` 报剩余文件数与页数。
- 新增插件 Logo（文档 + 命中行 + 放大镜概念），提供 `logo.svg` 母版与 512×512 透明底 `logo.png`。
- 新增 debug / warning 级日志：LSP 进程生命周期（spawn、initialize 耗时、空闲关闭）、每次 LSP 请求方法与耗时、数据源合并结果、目录缺失等。

### Changed
- `self_read` 新增 `end_line`：支持 `start_line` / `end_line` 闭区间读取，`end_line` 为负数时从末尾倒数（`-1` = 最后一行）；批量 `files` 数组的每一项也可单独指定区间。
- 截断处统一给出精确续读参数，不再出现 `end_line=...` 形式的占位省略号，例如直接给出 `start_line=26&end_line=8480`（读到末尾）与 `start_line=26&end_line=50`（只读下一页）。
- `self_read` 的续读提示从头部移到正文末尾，模型读到结尾即可看到下一步参数。
- `lsp_servers` 配置改用普通列表编辑（移除代码编辑器模式）；修正配置描述中残留的旧工具名。

### Fixed
- 修复工具参数 `context` 与 `call(self, context, ...)` 形参撞名导致运行时 TypeError 的问题，参数改名 `context_lines`。
- 修复 `__init__.py` 缺失导致 `from .lsp import` 在部分加载路径下解析失败的问题。
- 修复 frecency 的 KV 存储无上限增长：新增自动淘汰（每源 500 条 / 最多 20 个数据源 / 全局 5000 条硬上限 / 约 5 天未访问自动删除），加载时补跑一次 GC；插件卸载或停用时随 `terminate` 清空 KV，不给 AstrBot 本体留垃圾。

## [1.0.1] - 2026-10-11

### Changed
- metadata 补充 `repository` 字段。

## [1.0.0] - 2026-10-11

### Added
- 首个版本：6 个 LLM 工具——文本级 `self_search` / `self_read` / `self_glob`，语义级 `self_symbols` / `self_refs` / `self_def`。
- 数据源白名单采用 AstrBot 条目化配置（启用 / 名称 / 绝对路径 / 描述），描述进入工具的可用源提示帮助模型选对源。
- `include_astrbot` 开关：把 AstrBot 自身白名单目录（工作区、全局技能、各插件 skills、临时目录）自动并入为数据源；全部停用时不注册任何工具。
- 通用 LSP 后端：内置常见语言探测表从当前环境 PATH 自动探测服务器，`lsp_servers` 配置可覆盖以适配任意环境；进程惰性启动、空闲自动关闭。
- 目录约定：数据源根目录的 `version.json`（`versionText` / `version`）作为版本戳出现在工具输出中，与外部自动更新脚本解耦。
- 安全设计：白名单 realpath 包含校验、只读、资源封顶、LSP 返回位置二次校验、注册门控、schema 可移植子集。


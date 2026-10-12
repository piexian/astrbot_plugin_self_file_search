<p align="center"><img src="https://cdn.jsdelivr.net/gh/piexian/astrbot_plugin_self_file_search@360518e5526f6104f2caadd8173574e97b8429a2/logo.svg" width="128" alt="白名单搜索"></p>

# 白名单搜索

为 AstrBot 提供受白名单约束的只读文件与代码工具：文本级搜索/读取 + LSP 语义级符号/引用/定义。


## 环境要求

| 依赖 | 版本要求 |
|------|----------|
| Python | >= 3.10 |
| AstrBot | >= 4.10.4（template_list 条目配置） |
| LSP 服务器（可选） | 从系统 PATH 自动探测，如 `csharp-ls` / `gopls` / `rust-analyzer` |

## 功能

- 6 个 LLM Tool：文本级 `self_search` / `self_read` / `self_glob` + 语义级 `self_symbols` / `self_refs` / `self_def`
- 数据源条目化配置（启用 / 名称 / 路径 / 描述），支持多源与版本戳
- 一键并入 AstrBot 自身白名单目录（工作区 / 全局技能 / 插件 skills / 临时目录）
- LSP 服务器通用加载：调用时在当前环境 PATH 逐个探测，配置可覆盖，任何环境有啥用啥
- 服务器进程惰性启动、空闲自动关闭
- 输出限长 + 分页，防止撑爆上下文

## 安装

**方式一**：在 AstrBot 插件市场搜索「白名单搜索」，点击安装。

**方式二**：插件界面右下角点击加号 → 从链接安装，输入：
```
https://github.com/piexian/astrbot_plugin_self_file_search
```

## 工具列表

### 文本级（永远可用）

| 工具 | 功能 |
|------|------|
| `self_search` | 目录内 grep：`mode=content`（命中行，默认）/ `files`（命中文件）/ `count`（每文件计数）；支持正则、大小写、`context_lines` 前后文、`offset` 翻页 |
| `self_read` | 读文件（带行号，可 `start_line` 续读）；`files` 数组一次最多批量读 8 个，共享输出预算，逐文件报错 |
| `self_glob` | 按文件名通配符列文件（如 `*.cs`、`Card*.cs`） |

### LSP 语义级（需要环境里有对应语言服务器）

| 工具 | 功能 |
|------|------|
| `self_symbols` | 工作区符号搜索（类/方法/字段），首次调用可能在建立索引 |
| `self_refs` | 符号的全部引用（跨文件语义解析），`path/line` 定位，`symbol` 缺省取该行第一个标识符 |
| `self_def` | 跳到符号定义处（可跨文件） |

## 使用

### LLM 对话中使用

当用户在群里问代码问题时，AI 会自动调用对应工具。例如：

- 「搜下 ThrowMethod 在哪」→ `self_symbols`，返回 `ThrowMethod.cs:1: [enum] ThrowMethod`
- 「这个枚举哪里用到了」→ `self_refs`，跨文件列出 `AI_PlayMusic.cs:572` 等引用
- 「把 ThrowMethod.cs 给我看下」→ `self_read`


### 安全设计

- **白名单包含校验**：所有路径 `realpath` 后必须落在数据源根目录以内，拒绝绝对路径与 `..`
- **只读**：全部工具无写入能力
- **资源封顶**：单文件 8 MB、扫描 2 万文件 / 20 秒、输出 16K 字符，超限截断并提示
- **LSP 二次校验**：语言服务器返回的位置再过一次根目录包含检查
- **注册门控**：全部数据源停用（含 AstrBot 白名单开关）时不注册任何工具
- **schema 可移植子集**：工具参数 schema 只用各 LLM provider 都接受的交集（标量 type、无 `$ref`/`anyOf`/`format`），避免一条被拒导致整个请求 400

## 配置

在 AstrBot 管理面板的插件配置中可设置：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `sources` | template_list | `[]` | 数据源白名单条目：启用 / 名称 / 绝对路径 / 描述 |
| `include_astrbot` | bool | `true` | 并入 AstrBot 白名单目录（`workspace` / `skills` / 各插件 skills / `tmp`），自建同名条目优先 |
| `max_matches_cap` | int | `100` | `self_search` 单次命中条目上限 |
| `max_read_chars` | int | `32000` | `self_read` 单次返回字符数上限 |
| `lsp_enabled` | bool | `true` | 启用 LSP 语义工具 |
| `lsp_idle_minutes` | int | `10` | LSP 服务器空闲多久自动关闭 |
| `lsp_servers` | list | `[]` | 覆盖探测表，每项 `扩展名\|命令行`（如 `cs\|csharp-ls`） |

## 项目结构

```
astrbot_plugin_self_file_search/
├── main.py                          # 插件入口：6 个 FunctionTool 与注册门控
├── lsp.py                           # 通用 LSP 后端：探测表 / 进程管理 / JSON-RPC
├── metadata.yaml                    # 插件元数据
├── _conf_schema.json                # 插件配置 Schema
└── README.md
```

## 相关链接

- [AstrBot 文档](https://docs.astrbot.app/)
- [插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [fastctx](https://github.com/yc-duan/fastctx) —— 接口设计借鉴来源
- [Issues](https://github.com/piexian/astrbot_plugin_self_file_search/issues)

<div align="center">

**如果这个插件对你有帮助，请给个 ⭐ Star 支持一下！**

</div>

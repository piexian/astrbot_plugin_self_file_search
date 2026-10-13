"""白名单代码搜索插件：给 LLM 提供受目录白名单约束的搜索与读取工具。

"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from fnmatch import fnmatch
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import AstrBotConfig, logger, star
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from .lsp import LSPManager, SYMBOL_KINDS, guess_ext, parse_overrides, to_utf16_char, uri_to_path

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vs"}
# frecency：访问一次 +1，按小时半衰（约 24h 衰减到 ~5%）
FRECENCY_HALF_LIFE_HOURS = 24.0
FRECENCY_HOT = 4.0   # >= 该分数标 [常看]
FRECENCY_WARM = 1.5  # >= 该分数标 [看过]
MAX_SCAN_FILES = 20000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_LINE_CHARS = 300
SEARCH_TIMEOUT_SECONDS = 20
SEARCH_OUTPUT_CAP = 16000


def _astrbot_whitelist_sources() -> dict[str, dict]:
    """把 AstrBot 自身白名单目录映射为数据源（与内置 fs 工具的非管理员可读范围对齐）：
    工作区、全局技能、插件/内置插件的 skills 子目录、临时目录。"""
    out: dict[str, dict] = {}
    try:
        from astrbot.core.utils.astrbot_path import (
            get_astrbot_builtin_plugin_path,
            get_astrbot_plugin_path,
            get_astrbot_skills_path,
            get_astrbot_system_tmp_path,
            get_astrbot_workspaces_path,
        )
    except Exception:
        return out

    def add(name: str, path: str, desc: str) -> None:
        if path and os.path.isdir(path) and name not in out:
            out[name] = {"path": path, "desc": desc}

    add("workspace", get_astrbot_workspaces_path(), "AstrBot 会话工作区根目录")
    add("skills", get_astrbot_skills_path(), "AstrBot 全局技能（只读）")
    try:
        for base, prefix, label in (
            (get_astrbot_plugin_path(), "plugin", "插件"),
            (get_astrbot_builtin_plugin_path(), "builtin", "内置插件"),
        ):
            for entry in sorted(os.listdir(base)):
                sk = os.path.join(base, entry, "skills")
                add(f"{prefix}_{entry}", sk, f"AstrBot {label}技能（只读）")
    except OSError:
        pass
    add("tmp", get_astrbot_system_tmp_path(), "AstrBot 系统临时目录")
    return out


def _sources(config) -> dict[str, dict]:
    """合并数据源：template_list 条目 + AstrBot 白名单目录（include_astrbot 开关）。"""
    out: dict[str, dict] = {}
    for item in (config.get("sources") or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        path = str(item.get("path", "")).strip()
        if not name or not path or not item.get("enabled", True):
            continue
        out[name] = {"path": path, "desc": str(item.get("desc", "")).strip()}
    if config.get("include_astrbot", True):
        for name, entry in _astrbot_whitelist_sources().items():
            out.setdefault(name, entry)  # 管理员自建的同名条目优先
    return out


def _contain(root: str, rel: str) -> str | None:
    """把相对路径安全解析到白名单根目录以内；越界或非法返回 None。"""
    rel = rel.replace("\\", "/").lstrip("/")
    if os.path.isabs(rel) or ".." in rel.split("/"):
        return None
    cand = os.path.realpath(os.path.join(root, rel))
    root_real = os.path.realpath(root)
    if cand != root_real and not cand.startswith(root_real + os.sep):
        return None
    return cand


def _stamp(root: str) -> str:
    try:
        with open(os.path.join(root, "version.json"), encoding="utf-8") as f:
            data = json.load(f)
        text = data.get("versionText") or data.get("version") or ""
        return f"[{text}] " if text else ""
    except (OSError, ValueError):
        return ""


def _frecency_key(src: str) -> str:
    return f"frecency:{src}"


def _frecency_load(plugin, src: str) -> dict:
    """{rel: [score, last_epoch_hours]}；KV 里是 {rel: {"s": float, "t": float}}。"""
    raw = plugin.kv.get(_frecency_key(src), {}) if plugin and plugin.kv_ready else {}
    return {k: [v.get("s", 0.0), v.get("t", 0.0)] for k, v in (raw or {}).items()}


def _frecency_bump(plugin, src: str, rels: list) -> None:
    """把本次返回的文件记一次访问（衰减后的旧分 + 1）。"""
    if not (plugin and plugin.kv_ready) or not rels:
        return
    now = _now_hours()
    data = _frecency_load(plugin, src)
    for rel in rels:
        s_old, t_old = data.get(rel, [0.0, now])
        decayed = s_old * (0.5 ** ((now - t_old) / FRECENCY_HALF_LIFE_HOURS))
        data[rel] = [decayed + 1.0, now]
    plugin.kv[_frecency_key(src)] = {
        k: {"s": round(v[0], 3), "t": round(v[1], 3)} for k, v in data.items()
    }


def _frecency_annotate(score: float) -> str:
    if score >= FRECENCY_HOT:
        return "  [常看的文件]"
    if score >= FRECENCY_WARM:
        return "  [看过的文件]"
    return ""


def _now_hours() -> float:
    return time.time() / 3600.0


def _err(msg: str) -> str:
    return f"error: {msg}"


def _line_text(abs_path: str, line1: int) -> str:
    """取文件第 line1 行（1 起）的文本；失败返回空串。"""
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            for i, text in enumerate(f, 1):
                if i == line1:
                    return text.rstrip("\n")
    except OSError:
        pass
    return ""


def _locate(line_text: str, symbol: str | None) -> tuple[str, int] | None:
    """返回 (符号, 行内 Python 字符偏移)；symbol 缺省取行内第一个标识符。"""
    if not symbol:
        match = re.search(r"\w{3,}", line_text)
        if not match:
            return None
        symbol = match.group(0)
    col = line_text.find(symbol)
    if col < 0:
        return None
    return symbol, col


async def _lsp_client(plugin, kwargs, root: str, ext_hint: str | None = None):
    """拿到 LSP 客户端；失败返回错误字符串。ext 未指定时按数据源里最多的已知扩展名。"""
    if not plugin.config.get("lsp_enabled", True):
        return _err("lsp_enabled 已关闭。")
    ext = str(kwargs.get("ext", "")).strip().lstrip(".").lower() or ext_hint
    if not ext:
        return _err("无法判断语言（数据源里没有已知扩展名的文件），可用 ext 参数显式指定。")
    overrides = parse_overrides(plugin.config.get("lsp_servers"))
    try:
        return await plugin.lsp.get(root, ext, overrides)
    except RuntimeError as exc:
        logger.debug(f"LSP unavailable: {exc}")
        return _err(str(exc))
    except (TimeoutError, OSError) as exc:
        logger.warning(f"LSP startup failed: {exc}")
        return _err(f"语言服务器启动失败：{exc}")


def _format_locations(root: str, result, limit: int, root_real: str) -> str:
    """把 Location/LocationLink 列表格式化成 rel:line: text。"""
    if result is None:
        return ""
    if isinstance(result, dict):
        result = [result]
    lines: list[str] = []
    seen: set[str] = set()
    for item in result or []:
        if "targetUri" in item:  # LocationLink
            uri = item["targetUri"]
            rng = item.get("targetSelectionRange") or item.get("targetRange") or {}
        else:
            uri = item.get("uri", "")
            rng = item.get("range", {})
        abs_path = uri_to_path(uri)
        if not abs_path.startswith(root_real + os.sep) and abs_path != root_real:
            continue  # 服务器跑在工作区里，理论上越不了界；双保险
        rel = os.path.relpath(abs_path, root).replace("\\", "/")
        line1 = rng.get("start", {}).get("line", 0) + 1
        key = f"{rel}:{line1}"
        if key in seen:
            continue
        seen.add(key)
        text = _line_text(abs_path, line1)[:MAX_LINE_CHARS]
        lines.append(f"{rel}:{line1}: {text}")
        if len(lines) >= limit:
            break
    return "\n".join(lines)


def _pick_source(kwargs: dict, cfg: dict) -> tuple[str, str] | str:
    """返回 (名称, 根目录) 或错误信息字符串。"""
    sources = _sources(cfg)
    if not sources:
        return _err("未配置数据源，请在插件配置「数据源白名单」里添加条目（名称 + 绝对路径）。")
    src = str(kwargs.get("source", "")).strip()
    if src not in sources:
        listing = "、".join(
            f"{name}（{entry['desc']}）" if entry["desc"] else name
            for name, entry in sources.items()
        )
        return _err(f"未知数据源 {src!r}，可用：{listing}")
    root = sources[src]["path"]
    if not os.path.isdir(root):
        logger.warning(f"self file search: data source {src!r} path does not exist: {root}")
        return _err(f"数据源目录不存在：{root}")
    return src, root


def _parse_excludes(raw) -> list[str]:
    """exclude_glob: 逗号/空格分隔，允许前缀 !（与 fff 的排除语法一致）。"""
    if not raw:
        return []
    text = str(raw)
    out = []
    for part in re.split(r"[,\s]+", text):
        p = part.strip().lstrip("!")
        if p:
            out.append(p)
    return out


def _is_excluded(rel: str, exclude: list[str]) -> bool:
    """相对路径或其任一路径段命中排除模式则跳过。"""
    base = rel.rsplit("/", 1)[-1]
    for pat in exclude:
        if fnmatch(rel, pat) or fnmatch(base, pat):
            return True
        # 目录前缀形式（如 generated/）
        if pat.endswith("/") and rel.startswith(pat):
            return True
    return False


def _compile(pattern: str, case_sensitive: bool, is_regex: bool):
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(pattern if is_regex else re.escape(pattern), flags)


@dataclass
class SelfSearchTool(FunctionTool[AstrAgentContext]):
    name: str = "self_search"
    description: str = (
        "Search text/code inside a whitelisted data source directory (interface "
        "borrowed from fastctx). Three output modes: mode=content returns `file:line: "
        "content` hit lines (default, supports context_lines); mode=files returns "
        "matching file paths only (locate files first); mode=count returns per-file "
        "hit counts. Case-insensitive substring by default; supports regex, file_glob "
        "and offset paging. Ideal for locating code by class name, function name, "
        "identifier or error message."
    )
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Data source name",
            },
            "pattern": {
                "type": "string",
                "description": "Text or regex to search for",
            },
            "mode": {
                "type": "string",
                "enum": ["content", "files", "count"],
                "description": "content = hit lines (default); files = matching paths only; count = per-file hit counts",
            },
            "file_glob": {
                "type": "string",
                "description": "Optional filename glob filter, e.g. *.cs; default all files",
            },
            "exclude_glob": {
                "type": "string",
                "description": "Optional globs to skip, comma-separated, e.g. '*.Designer.cs, generated/*'; leading ! tolerated",
            },
            "is_regex": {
                "type": "boolean",
                "description": "Whether pattern is parsed as regex; default false (plain text)",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Case-sensitive matching; default false",
            },
            "context_lines": {
                "type": "integer",
                "description": "Optional N lines before/after each hit (content mode only); default 0",
            },
            "max_matches": {
                "type": "integer",
                "description": "Max entries returned (content=hit lines, files/count=files); default 40",
            },
            "offset": {
                "type": "integer",
                "description": "Optional; skip the first N entries (paging); default 0",
            },
        },
        "required": ["source", "pattern"],
    })
    plugin_ref: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        cfg = self.plugin_ref.config if self.plugin_ref else {}
        picked = _pick_source(kwargs, cfg)
        if isinstance(picked, str):
            return picked
        src, root = picked

        pattern = str(kwargs.get("pattern", "")).strip()
        if not pattern:
            return _err("pattern 为空。")
        mode = str(kwargs.get("mode", "content") or "content")
        if mode not in ("content", "files", "count"):
            return _err(f"mode 只支持 content/files/count，收到 {mode!r}")
        try:
            rx = _compile(
                pattern,
                bool(kwargs.get("case_sensitive", False)),
                bool(kwargs.get("is_regex", False)),
            )
        except re.error as exc:
            return _err(f"正则无效：{exc}")
        limit = max(1, min(int(kwargs.get("max_matches", 40) or 40), 200))
        offset = max(0, int(kwargs.get("offset", 0) or 0))
        ctx_lines = max(0, min(int(kwargs.get("context_lines", 0) or 0), 10)) if mode == "content" else 0
        glob = str(kwargs.get("file_glob", "")).strip() or "*"
        exclude = _parse_excludes(kwargs.get("exclude_glob"))

        # 收集每个文件的命中行号（含行文本），扫描由文件数/时限双封顶
        matches: dict[str, dict[int, str]] = {}
        order: list[str] = []
        total = 0
        scanned = 0
        deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS
        incomplete = False
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if scanned >= MAX_SCAN_FILES or time.monotonic() > deadline:
                    incomplete = True
                    break
                if not fnmatch(fn, glob):
                    continue
                if exclude and _is_excluded(os.path.relpath(os.path.join(dirpath, fn), root).replace("\\", "/"), exclude):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(path) > MAX_FILE_BYTES:
                        continue
                    with open(path, encoding="utf-8", errors="replace") as f:
                        for i, line in enumerate(f, 1):
                            if rx.search(line):
                                rel = os.path.relpath(path, root).replace("\\", "/")
                                if rel not in matches:
                                    matches[rel] = {}
                                    order.append(rel)
                                matches[rel][i] = line.rstrip()[:MAX_LINE_CHARS]
                                total += 1
                                if total >= 5000:
                                    incomplete = True
                                    break
                except OSError:
                    continue
                scanned += 1
            if incomplete:
                break

        logger.debug(
            f"self_search: {src} mode={mode} pattern={pattern!r} "
            f"total={total} scanned={scanned} incomplete={incomplete}"
        )
        head = f"{_stamp(root)}[{src}] {mode} 模式"
        if mode == "files":
            # files 模式按 frecency 排序：常查的文件（Card.cs 这类）排前面。
            # content/count 不动——content 逐行命中，重排会打乱行号语义。
            order = self._rank_by_frecency(src, order)
            entries = [
                f"{rel}（{len(matches[rel])} 处）{self._frecency_tag(src, rel)}"
                for rel in order
            ][offset:offset + limit]
            head += f"，命中文件 {len(order)} 个"
            if self.plugin_ref and self.plugin_ref.kv_ready:
                head += "（按访问频率排序）"
            # 记一次访问（异步落盘，不阻塞返回）
            self._bump(src, order[offset:offset + limit])
        elif mode == "count":
            entries = [f"{rel}: {len(matches[rel])}" for rel in order][offset:offset + limit]
            head += f"，命中文件 {len(order)} 个，共 {total} 处"
        else:
            rendered = self._render_content(root, order, matches, ctx_lines, limit, offset)
            entries = rendered
            head += f"，命中 {total} 行"
        if incomplete:
            head += "（达到上限或扫描超时，结果可能不全，可缩小 pattern 或加 file_glob 再搜）"
        # 剩余条数 + 预估页数：让模型知道还有多少、要读几页
        if mode == "content":
            remain = max(0, total - offset - len(entries))
        else:
            remain = max(0, len(order) - offset - len(entries))
        if remain:
            pages = max(1, -(-remain // max(limit, 1)))
            head += f"；还剩 {remain} 条未显示，按 max_matches={limit} 估计还需 {pages} 页（offset={offset + len(entries)} 起）"
        if not entries:
            return f"{head}，无可命中内容。"
        body = "\n".join(entries)
        if len(head) + len(body) > SEARCH_OUTPUT_CAP:
            shown = len(entries)
            next_hint = ("本次已输出 " + str(shown) + " 条：翻页用 offset=" + str(offset + shown) + " 继续，或用 file_glob 收窄、max_matches 调小后重搜。")
            body = body[: SEARCH_OUTPUT_CAP - len(head)] + "\n…（输出超限截断）" + next_hint
        return head + "\n" + body

    def _score(self, src: str, rel: str) -> float:
        plugin = self.plugin_ref
        if not (plugin and plugin.kv_ready):
            return 0.0
        data = _frecency_load(plugin, src)
        s_old, t_old = data.get(rel, [0.0, _now_hours()])
        return s_old * (0.5 ** ((_now_hours() - t_old) / FRECENCY_HALF_LIFE_HOURS))

    def _rank_by_frecency(self, src: str, order: list[str]) -> list[str]:
        """稳定排序：frecency 降序，同分保持原顺序。"""
        if not (self.plugin_ref and self.plugin_ref.kv_ready):
            return list(order)
        return sorted(order, key=lambda r: -self._score(src, r))

    def _frecency_tag(self, src: str, rel: str) -> str:
        return _frecency_annotate(self._score(src, rel))

    def _bump(self, src: str, rels: list[str]) -> None:
        plugin = self.plugin_ref
        if not (plugin and plugin.kv_ready) or not rels:
            return
        now = _now_hours()
        data = _frecency_load(plugin, src)
        for rel in rels:
            s_old, t_old = data.get(rel, [0.0, now])
            data[rel] = [s_old * (0.5 ** ((now - t_old) / FRECENCY_HALF_LIFE_HOURS)) + 1.0, now]
        payload = {k: {"s": round(v[0], 3), "t": round(v[1], 3)} for k, v in data.items()}
        plugin.kv[_frecency_key(src)] = payload
        asyncio.create_task(plugin._save_kv(src, payload))

    @staticmethod
    def _render_content(root, order, matches, ctx_lines, limit, offset) -> list[str]:
        """content 模式渲染：无前后文逐命中行；带前后文按区间合并输出（rg 风格 - 行）。"""
        out: list[str] = []
        emitted = 0
        for rel in order:
            if emitted >= limit:
                break
            line_map = matches[rel]
            abs_path = os.path.join(root, rel.replace("/", os.sep))
            if ctx_lines <= 0:
                for line1 in sorted(line_map):
                    if emitted >= limit:
                        break
                    if offset:
                        offset -= 1
                        continue
                    out.append(f"{rel}:{line1}: {line_map[line1]}")
                    emitted += 1
                continue
            # 合并命中±context 的行区间
            intervals: list[list[int]] = []
            for line1 in sorted(line_map):
                if intervals and line1 - intervals[-1][1] <= ctx_lines + 1:
                    intervals[-1][1] = max(intervals[-1][1], line1 + ctx_lines)
                else:
                    intervals.append([max(1, line1 - ctx_lines), line1 + ctx_lines])
            file_lines: dict[int, str] = {}
            try:
                with open(abs_path, encoding="utf-8", errors="replace") as f:
                    for i, text in enumerate(f, 1):
                        if any(a <= i <= b for a, b in intervals):
                            file_lines[i] = text.rstrip()[:MAX_LINE_CHARS]
            except OSError:
                continue
            for a, b in intervals:
                if emitted >= limit:
                    break
                if offset:
                    offset -= 1
                    continue
                for i in range(a, b + 1):
                    if i in line_map:
                        out.append(f"{rel}:{i}: {file_lines.get(i, '')}")
                    else:
                        out.append(f"{rel}-{i}- {file_lines.get(i, '')}")
                out.append("")
                emitted += 1
        # 去掉末尾多余空行
        while out and out[-1] == "":
            out.pop()
        return out


@dataclass
class SelfReadTool(FunctionTool[AstrAgentContext]):
    name: str = "self_read"
    description: str = (
        "Read text/code files inside a whitelisted data source (relative path, "
        "line-numbered, length-capped). Use start_line/end_line to read a specific "
        "line range (end_line negative counts from the end, e.g. -1 = last line) "
        "instead of always reading from the top. Paths come from self_search / "
        "self_symbols hits; follow the start_line hint to continue long files. Pass "
        "`files` (up to 8) to batch-read several files in one call with a shared "
        "output budget; per-file problems are reported inline. Omit files to read "
        "a single path."
    )
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Data source name",
            },
            "path": {
                "type": "string",
                "description": "Relative path inside the data source (single-file form)",
            },
            "files": {
                "type": "array",
                "description": "Batch-read up to 8 files; mutually exclusive with path",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path inside the data source"},
                        "start_line": {"type": "integer", "description": "1-based start line"},
                        "end_line": {"type": "integer", "description": "Optional 1-based inclusive end line; negative counts from the end (-1 = last line)"},
                        "max_chars": {"type": "integer", "description": "Max characters returned for this file"},
                    },
                    "required": ["path"],
                },
            },
            "start_line": {
                "type": "integer",
                "description": "Optional 1-based start line; default 1",
            },
            "end_line": {
                "type": "integer",
                "description": "Optional 1-based inclusive end line; negative counts from the end (-1 = last line)",
            },
            "max_chars": {
                "type": "integer",
                "description": "Optional max characters returned; default 8000",
            },
        },
        "required": ["source"],
    })
    plugin_ref: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        cfg = self.plugin_ref.config if self.plugin_ref else {}
        picked = _pick_source(kwargs, cfg)
        if isinstance(picked, str):
            return picked
        src, root = picked

        files = kwargs.get("files")
        if files:
            if not isinstance(files, list) or not all(isinstance(x, dict) for x in files):
                return _err("files 必须是对象数组，每项至少含 path。")
            if len(files) > 8:
                return _err("files 一次最多 8 个文件，分多次调用。")
            default_chars = min(int(kwargs.get("max_chars", 8000) or 8000),
                                int(cfg.get("max_read_chars", 32000) or 32000))
            blocks: list[str] = []
            truncated = False
            for idx, entry in enumerate(files, 1):
                head, body, more = self._read_entry(root, entry, default_chars)
                tag = "" if not more else "，未完可续读"
                blocks.append(f"[{idx}/{len(files)}] {head}{tag}\n{body}" if body else f"[{idx}/{len(files)}] {head}")
                if more:
                    truncated = True
            text = f"[{src}] 批量读取 {len(files)} 个文件\n" + "\n".join(blocks)
            if truncated:
                text += "\n（Partial：有文件未显示完整，按各文件头提示的 start_line 续读）"
            return _limit(
                text, SEARCH_OUTPUT_CAP,
                "输出超限：改小各文件的 max_chars，或拆成两次调用分批读"
                "（各文件头已注明还剩多少行未读）。",
            )

        rel = str(kwargs.get("path", "")).strip()
        if not rel:
            return _err("请提供 path 或 files。")
        head, body, more = self._read_entry(
            root, {"path": rel,
                   "start_line": kwargs.get("start_line"),
                   "end_line": kwargs.get("end_line"),
                   "max_chars": kwargs.get("max_chars")},
            min(int(kwargs.get("max_chars", 8000) or 8000),
                int(cfg.get("max_read_chars", 32000) or 32000)),
        )
        text = f"{head}\n{body}" if body else head
        if more:
            # 续读提示放正文末尾：模型读到结尾才需要知道下一步
            mm = re.search(r"显示 (\d+)-(\d+) 行", head)
            tip = ("\n（Partial：未显示完整，用 start_line=" + str(int(mm.group(2)) + 1)
                   + " 继续读）") if mm else "\n（Partial：未显示完整，用 start_line 指定起点继续读）"
            text += tip
        m = re.search(r"显示 (\d+)-(\d+) 行", head)
        mtotal = re.search(r"共 (\d+) 行", head)
        total_hint = int(mtotal.group(1)) if mtotal else 0
        page_size = (int(m.group(2)) - int(m.group(1)) + 1) if m else 0
        if m:
            nxt = int(m.group(2)) + 1
            last = int(m.group(2))
            nxt = last + 1
            next_hint = (
                "输出超限：改用 start_line=" + str(nxt) + " 继续读"
                "（读到底 start_line=" + str(nxt) + "&end_line=" + str(total_hint) +
                "；只读一页 start_line=" + str(nxt) + "&end_line=" + str(min(total_hint, last + page_size)) + "）。"
            )
        else:
            next_hint = "输出超限：用 start_line 指定起点继续读。"
        return _limit(f"{_stamp(root)}[{src}] " + text, SEARCH_OUTPUT_CAP, next_hint)

    def _read_entry(self, root: str, entry: dict, default_chars: int) -> tuple[str, str, bool]:
        """读单个条目，返回 (头信息, 正文, 是否还有剩余)。"""
        rel = str(entry.get("path", "")).strip()
        abs_path = _contain(root, rel)
        if abs_path is None:
            return f"{rel}: 路径越界或非法", "", False
        if not os.path.isfile(abs_path):
            return f"{rel}: 文件不存在", "", False
        max_chars = min(int(entry.get("max_chars", default_chars) or default_chars), default_chars)
        start = max(1, int(entry.get("start_line", 1) or 1))
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as exc:
            return f"{rel}: 读取失败（{exc}）", "", False
        total = len(lines)
        s = min(start, max(total, 1))
        # end_line: 闭区间；负数从末尾倒数（-1 = 最后一行）
        raw_end = entry.get("end_line")
        if raw_end not in (None, ""):
            try:
                n = int(raw_end)
            except (TypeError, ValueError):
                n = total
            stop = total + 1 + n if n < 0 else n
            stop = min(max(stop, s), total)
        else:
            stop = total
        out: list[str] = []
        used = 0
        for i in range(s, stop + 1):
            piece = f"{i}: {lines[i - 1].rstrip()[:MAX_LINE_CHARS]}\n"
            if used + len(piece) > max_chars and out:
                break
            out.append(piece)
            used += len(piece)
        end = s + len(out) - 1
        head = f"{rel}（共 {total} 行，显示 {s}-{end} 行）"
        if end < total:
            left_lines = total - end
            left_chars = sum(len(x) for x in lines[end:])
            pages = max(1, -(-left_chars // max(max_chars, 1)))
            nxt = end + 1
            # 精确给出下一页的区间终点，别让模型自己猜
            page_end = min(total, nxt + max(1, len(out)) - 1)
            head += (
                f"，还剩 {left_lines} 行未读（约 {left_chars} 字符，按当前 max_chars"
                f" 估计还需 {pages} 次）；续读：start_line={nxt}（读到文件末尾则用"
                f" start_line={nxt}&end_line={total}，只读下一页用"
                f" start_line={nxt}&end_line={page_end}）"
            )
            return head, "".join(out), True
        return head, "".join(out), False


@dataclass
class SelfGlobTool(FunctionTool[AstrAgentContext]):
    name: str = "self_glob"
    description: str = (
        "List files inside a whitelisted data source by filename glob (e.g. *.cs, "
        "Card*.cs, *Trait*). Use it to confirm a path when you know the filename or "
        "to browse structure; use self_search to find code by content."
    )
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Data source name"},
            "pattern": {
                "type": "string",
                "description": "Filename glob, e.g. *.cs; default * (all files)",
            },
            "max": {"type": "integer", "description": "Max entries returned; default 100"},
        },
        "required": ["source"],
    })
    plugin_ref: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        cfg = self.plugin_ref.config if self.plugin_ref else {}
        picked = _pick_source(kwargs, cfg)
        if isinstance(picked, str):
            return picked
        src, root = picked
        pattern = str(kwargs.get("pattern", "")).strip() or "*"
        limit = max(1, min(int(kwargs.get("max", 100) or 100), 500))
        rels: list[str] = []
        scanned = 0
        deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS
        incomplete = False
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if scanned >= MAX_SCAN_FILES or time.monotonic() > deadline:
                    incomplete = True
                    break
                if not fnmatch(fn, pattern):
                    continue
                path = os.path.join(dirpath, fn)
                rels.append(os.path.relpath(path, root).replace("\\", "/"))
                scanned += 1
            if incomplete:
                break
        rels.sort()
        total = len(rels)
        shown = rels[:limit]
        head = f"{_stamp(root)}[{src}] 匹配 {total} 个文件"
        if incomplete:
            head += "（扫描超时/达到上限，结果可能不全）"
        if not shown:
            return f"{head}，无匹配文件。"
        body = "\n".join(shown)
        if total > len(shown):
            rest = total - len(shown)
            pages = max(1, -(-rest // max(limit, 1)))
            body += (
                f"\n…（其余 {rest} 个未显示，按 max={limit} 估计还需 {pages} 页；"
                f"建议收窄 pattern，如加前缀）"
            )
        return _limit(
            head + "\n" + body, SEARCH_OUTPUT_CAP,
            "输出超限：收窄 pattern（如加前缀 Card*）后重列。",
        )


@dataclass
class SelfSymbolsTool(FunctionTool[AstrAgentContext]):
    name: str = "self_symbols"
    description: str = (
        "Search workspace symbols (classes/methods/fields/enums) via the language "
        "server — semantically more precise than plain-text search. Requires an LSP "
        "server for the language on PATH; on first call the server may still be "
        "indexing, so retry if the result is empty."
    )
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Data source name"},
            "query": {"type": "string", "description": "Symbol name or a part of it, e.g. ThrowMethod"},
            "ext": {"type": "string", "description": "Optional language extension (e.g. cs); auto-detected from the source by default"},
            "max": {"type": "integer", "description": "Max entries returned; default 30"},
        },
        "required": ["source", "query"],
    })
    plugin_ref: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        cfg = self.plugin_ref.config if self.plugin_ref else {}
        picked = _pick_source(kwargs, cfg)
        if isinstance(picked, str):
            return picked
        src, root = picked
        if not self.plugin_ref._lsp_ext_cache.get(root):
            self.plugin_ref._lsp_ext_cache[root] = guess_ext(root)
        client = await _lsp_client(self.plugin_ref, kwargs, root, self.plugin_ref._lsp_ext_cache.get(root))
        if isinstance(client, str):
            return client
        query = str(kwargs.get("query", "")).strip()
        if not query:
            return _err("query 为空。")
        limit = max(1, min(int(kwargs.get("max", 30) or 30), 100))
        try:
            result = await client.symbols(query)
        except Exception as exc:
            return _err(f"符号搜索失败：{exc}")
        items = result if isinstance(result, list) else []
        lines: list[str] = []
        for item in items[:limit]:
            loc = item.get("location", {})
            abs_path = uri_to_path(loc.get("uri", ""))
            if not abs_path.startswith(os.path.realpath(root)):
                continue
            rel = os.path.relpath(abs_path, root).replace("\\", "/")
            line1 = loc.get("range", {}).get("start", {}).get("line", 0) + 1
            kind = SYMBOL_KINDS.get(item.get("kind"), "symbol")
            lines.append(f"{rel}:{line1}: [{kind}] {item.get('name', '')}")
        head = f"符号 {query!r} 命中 {len(lines)} 条"
        if not lines:
            head += "（服务器可能仍在建立索引，稍后重试通常会有结果；也可先用 code_search 文本兜底）"
        return head + "\n" + "\n".join(lines)


@dataclass
class SelfRefsTool(FunctionTool[AstrAgentContext]):
    name: str = "self_refs"
    description: str = (
        "Find all references to a symbol across the data source (semantic, resolved "
        "by the language server, cross-file). path/line come from self_search or "
        "self_symbols hits; symbol defaults to the first identifier on that line."
    )
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Data source name"},
            "path": {"type": "string", "description": "Relative path inside the data source"},
            "line": {"type": "integer", "description": "1-based line number where the symbol appears"},
            "symbol": {"type": "string", "description": "Optional symbol to query; defaults to the first identifier on that line"},
            "ext": {"type": "string", "description": "Optional language extension (e.g. cs)"},
            "max": {"type": "integer", "description": "Max entries returned; default 50"},
        },
        "required": ["source", "path", "line"],
    })
    plugin_ref: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        return await self._run("refs", kwargs)

    async def _run(self, mode: str, kwargs: dict) -> str:
        cfg = self.plugin_ref.config if self.plugin_ref else {}
        picked = _pick_source(kwargs, cfg)
        if isinstance(picked, str):
            return picked
        src, root = picked
        rel = str(kwargs.get("path", "")).strip()
        abs_path = _contain(root, rel)
        if abs_path is None:
            return _err("路径越界或非法（拒绝绝对路径与 ..）。")
        if not os.path.isfile(abs_path):
            return _err(f"文件不存在：{rel}")
        line1 = int(kwargs.get("line", 0) or 0)
        line_text = _line_text(abs_path, line1)
        if not line_text:
            return _err(f"{rel} 第 {line1} 行不存在或为空。")
        located = _locate(line_text, str(kwargs.get("symbol", "")).strip() or None)
        if located is None:
            return _err(f"该行找不到可定位的标识符，请用 symbol 参数指明。")
        symbol, col = located
        if not self.plugin_ref._lsp_ext_cache.get(root):
            self.plugin_ref._lsp_ext_cache[root] = guess_ext(root)
        client = await _lsp_client(
            self.plugin_ref, kwargs, root, self.plugin_ref._lsp_ext_cache.get(root)
        )
        if isinstance(client, str):
            return client
        char = to_utf16_char(line_text, col)
        limit = max(1, min(int(kwargs.get("max", 50) or 50), 200))
        root_real = os.path.realpath(root)
        try:
            if mode == "refs":
                result = await client.references(abs_path, line1 - 1, char, include_decl=True)
            else:
                result = await client.definition(abs_path, line1 - 1, char)
        except Exception as exc:
            return _err(f"语言服务器查询失败：{exc}")
        body = _format_locations(root, result, limit, root_real)
        head = f"符号 {symbol!r} 的{('定义' if mode == 'def' else '引用')}："
        if not body:
            return head + " 无结果（若服务器刚启动，可稍后重试）。"
        return _limit(
            head + "\n" + body, SEARCH_OUTPUT_CAP,
            "输出超限：调小 max 只看前几条，或换个更具体的 symbol 重查。",
        )


@dataclass
class SelfDefTool(FunctionTool[AstrAgentContext]):
    name: str = "self_def"
    description: str = (
        "Jump to a symbol's definition (semantic, resolved by the language server, "
        "cross-file). path/line point at a usage; symbol defaults to the first "
        "identifier on that line."
    )
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Data source name"},
            "path": {"type": "string", "description": "Relative path inside the data source"},
            "line": {"type": "integer", "description": "1-based line number where the symbol appears"},
            "symbol": {"type": "string", "description": "Optional symbol to jump to; defaults to the first identifier on that line"},
            "ext": {"type": "string", "description": "Optional language extension (e.g. cs)"},
        },
        "required": ["source", "path", "line"],
    })
    plugin_ref: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        return await SelfRefsTool(plugin_ref=self.plugin_ref)._run("def", kwargs)


def _limit(text: str, max_chars: int, next_hint: str = "") -> str:
    if len(text) <= max_chars:  # 截断时给出「下一步怎么读」
        return text
    tail = "\n…（输出超限被截断，共 " + str(len(text)) + " 字符）"
    if next_hint:
        tail += " " + next_hint
    return text[:max_chars] + tail


class SelfFileSearchPlugin(star.Star):
    """白名单代码搜索插件。"""

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.lsp = LSPManager(int(config.get("lsp_idle_minutes", 10) or 10))
        self._lsp_ext_cache: dict[str, str | None] = {}
        # frecency 走 AstrBot 自带的插件 KV 存储（Star 已混入 PluginKVStoreMixin），
        # 不自己落盘：跨重载保留、无并发写竞争。
        self.kv: dict = {}
        self.kv_ready = False
        self._kv_task = None
        if config.get("frecency_enabled", True):
            self._kv_task = asyncio.create_task(self._load_kv())
        sources = _sources(config)
        if not sources:
            # 全部数据源停用（含 AstrBot 白名单开关）时不注册工具，
            # 免得机器人面对五个永远报错的工具白白耗上下文。
            logger.info("astrbot_plugin_self_file_search: no enabled data source, tools not registered")
            return
        logger.debug(f"self file search: enabled sources: {sorted(sources)}")
        context.add_llm_tools(
            SelfSearchTool(plugin_ref=self),
            SelfReadTool(plugin_ref=self),
            SelfGlobTool(plugin_ref=self),
            SelfSymbolsTool(plugin_ref=self),
            SelfRefsTool(plugin_ref=self),
            SelfDefTool(plugin_ref=self),
        )
        logger.info("astrbot_plugin_self_file_search: self_search/self_read/self_glob/self_symbols/self_refs/self_def registered")

    async def _load_kv(self) -> None:
        try:
            self.kv = await self.get_kv_data("frecency", {}) or {}
            self.kv_ready = True
            logger.debug(f"frecency loaded: {len(self.kv)} source buckets")
        except Exception as exc:
            logger.warning(f"frecency load failed, ranking disabled: {exc}")
            self.kv_ready = False

    async def _save_kv(self, src: str, data: dict) -> None:
        if not self.kv_ready:
            return
        try:
            self.kv[_frecency_key(src)] = data
            await self.put_kv_data("frecency", self.kv)
        except Exception as exc:
            logger.warning(f"frecency save failed: {exc}")

    async def terminate(self) -> None:
        if self._kv_task and not self._kv_task.done():
            self._kv_task.cancel()
        await self.lsp.shutdown()

"""通用 LSP 后端：自动探测环境 PATH 里的语言服务器，按数据源根目录管理进程生命周期。"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.parse
import urllib.request

from astrbot.api import logger

# 扩展名 -> 候选命令行（按优先级），PATH 里探测到哪个用哪个
LSP_CANDIDATES: dict[str, list[list[str]]] = {
    "cs": [["csharp-ls"], ["omnisharp", "-lsp"]],
    "go": [["gopls"]],
    "rs": [["rust-analyzer"]],
    "ts": [["typescript-language-server", "--stdio"]],
    "tsx": [["typescript-language-server", "--stdio"]],
    "js": [["typescript-language-server", "--stdio"]],
    "py": [["pyright-langserver", "--stdio"], ["pylsp"]],
    "c": [["clangd"]],
    "cpp": [["clangd"]],
    "cc": [["clangd"]],
    "h": [["clangd"]],
    "lua": [["lua-language-server"]],
    "php": [["intelephense", "--stdio"]],
    "rb": [["ruby-lsp"]],
    "zig": [["zls"]],
    "dart": [["dart", "language-server"]],
}

LANGUAGE_IDS = {
    "cs": "csharp",
    "go": "go",
    "rs": "rust",
    "ts": "typescript",
    "tsx": "typescriptreact",
    "js": "javascript",
    "py": "python",
    "c": "c",
    "cpp": "cpp",
    "cc": "cpp",
    "h": "c",
    "lua": "lua",
    "php": "php",
    "rb": "ruby",
    "zig": "zig",
    "dart": "dart",
}

SYMBOL_KINDS = {
    1: "file",
    2: "module",
    3: "namespace",
    4: "package",
    5: "class",
    6: "method",
    7: "property",
    8: "field",
    9: "constructor",
    10: "enum",
    11: "interface",
    12: "function",
    13: "variable",
    14: "constant",
    15: "string",
    22: "struct",
    23: "event",
    25: "type-parameter",
}


def parse_overrides(items) -> dict[str, list[list[str]]]:
    """配置项 `扩展名|命令行` -> {ext: [argv, ...]}。"""
    out: dict[str, list[list[str]]] = {}
    for item in items or []:
        text = str(item).strip()
        if not text or text.startswith("#") or "|" not in text:
            continue
        ext, _, cmd = text.partition("|")
        ext, cmd = ext.strip().lstrip("."), cmd.strip()
        try:
            argv = [a.strip('"') for a in shlex.split(cmd, posix=False)]
        except ValueError:
            continue
        if ext and argv:
            out.setdefault(ext, []).append(argv)
    return out


def parse_initialization(items) -> dict[str, dict]:
    """配置项 `服务器或扩展名|JSON 对象` -> 初始化参数。"""
    out: dict[str, dict] = {}
    for item in items or []:
        text = str(item).strip()
        if not text or text.startswith("#"):
            continue
        name, sep, raw = text.partition("|")
        if not sep or not name.strip():
            raise ValueError("每项应为 服务器或扩展名|JSON 对象")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name.strip()} 的 JSON 无效：{exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{name.strip()} 的初始化参数必须是 JSON 对象")
        out[name.strip().lower().lstrip(".")] = value
    return out


def probe(
    ext: str, overrides: dict[str, list[list[str]]]
) -> tuple[list[str] | None, list[str]]:
    """返回 (argv|None, 已尝试列表)。argv 是 PATH 探测到的可执行文件绝对路径 + 参数。"""
    tried: list[str] = []
    for argv in overrides.get(ext, []) + LSP_CANDIDATES.get(ext, []):
        exe = shutil.which(argv[0])
        if exe:
            found = list(argv)
            found[0] = exe
            return found, tried
        tried.append(" ".join(argv))
    return None, tried


def guess_ext(root: str) -> str | None:
    """数据源目录里数量最多的已知扩展名。"""
    counts: dict[str, int] = {}
    for _dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
            if ext in LSP_CANDIDATES:
                counts[ext] = counts.get(ext, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _path_to_uri(path: str) -> str:
    return urllib.parse.quote(
        "file:///" + os.path.abspath(path).replace("\\", "/"), safe="/:"
    )


def uri_to_path(uri: str) -> str:
    path = urllib.request.url2pathname(urllib.parse.urlparse(uri).path)
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return path


def to_utf16_char(line_text: str, py_char: int) -> int:
    """Python 字符偏移 -> LSP 默认的 UTF-16 code unit 偏移。"""
    return len(line_text[:py_char].encode("utf-16-le")) // 2


def no_server_msg(ext: str, tried: list[str]) -> str:
    tried_text = "；".join(tried) if tried else "内置探测表里没有该扩展名"
    return (
        f"环境里没找到 .{ext} 的语言服务器（试过：{tried_text}）。"
        f"可在插件配置 lsp_servers 里加一行「{ext}|命令行」指定，或先安装对应服务器。"
    )


ROOT_MARKER_GROUPS: dict[str, tuple[tuple[str, ...], ...]] = {
    "csharp-ls": (("*.sln", "*.slnx", "*.csproj", "global.json"),),
    "omnisharp": (("*.sln", "*.slnx", "*.csproj", "omnisharp.json", "global.json"),),
    "gopls": (("go.work",), ("go.mod", "go.sum")),
    "rust-analyzer": (("Cargo.toml", "Cargo.lock", "rust-project.json"),),
    "typescript-language-server": (
        (
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "bun.lock",
            "bun.lockb",
            "package.json",
            "tsconfig.json",
            "jsconfig.json",
        ),
    ),
    "pyright": (
        (
            "pyrightconfig.json",
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "requirements.txt",
            "Pipfile",
        ),
    ),
    "pylsp": (
        ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile"),
    ),
    "clangd": (("compile_commands.json", "compile_flags.txt", ".clangd"),),
    "lua-language-server": (
        (".luarc.json", ".luarc.jsonc", ".emmyrc.json", ".luacheckrc", ".stylua.toml"),
    ),
    "intelephense": (("composer.json", "composer.lock", ".php-version"),),
    "ruby-lsp": (("Gemfile",),),
    "zls": (("zls.json", "build.zig"),),
    "dart": (("pubspec.yaml", "analysis_options.yaml"),),
}


def _executable_name(argv: list[str]) -> str:
    name = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower() if argv else ""
    for suffix in (".exe", ".cmd", ".bat", ".sh"):
        name = name.removesuffix(suffix)
    return name


def server_id(argv: list[str], ext: str) -> str:
    return _executable_name(argv) or ext


def _has_marker(directory: str, marker: str) -> bool:
    if any(char in marker for char in "*?["):
        try:
            with os.scandir(directory) as entries:
                return any(fnmatch.fnmatch(entry.name, marker) for entry in entries)
        except OSError:
            return False
    return os.path.exists(os.path.join(directory, marker))


def _nearest_marker_root(
    source_root: str, path: str, markers: tuple[str, ...]
) -> str | None:
    source = os.path.realpath(source_root)
    current = os.path.dirname(os.path.realpath(path))
    while current == source or current.startswith(source + os.sep):
        if any(_has_marker(current, marker) for marker in markers):
            return current
        if current == source:
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def resolve_root(source_root: str, path: str | None, server: str, ext: str) -> str:
    if not path:
        return source_root
    groups = ROOT_MARKER_GROUPS.get(server) or ROOT_MARKER_GROUPS.get(ext) or ()
    for markers in groups:
        found = _nearest_marker_root(source_root, path, markers)
        if found:
            return found
    return os.path.realpath(source_root)


def _configuration_value(settings: dict, section: str | None):
    if not section:
        return settings or None
    value = settings
    for key in section.split("."):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _is_windows() -> bool:
    return os.name == "nt"


async def _spawn_lsp_process(argv: list[str], cwd: str):
    options = (
        {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        if _is_windows()
        else {}
    )
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        **options,
    )


async def _kill_process_tree(proc) -> None:
    if _is_windows() and proc.pid:
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/pid",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            _, stderr = await asyncio.wait_for(killer.communicate(), timeout=3)
            if killer.returncode == 0:
                return
            logger.debug(
                f"LSP taskkill failed (code={killer.returncode}): "
                f"{stderr.decode('utf-8', errors='replace').strip()}"
            )
        except (TimeoutError, OSError) as exc:
            logger.debug(f"LSP taskkill unavailable: {exc}")
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


LSP_STDERR_CAP = 16000
LSP_PROJECT_SUFFIXES = (".sln", ".csproj", ".fsproj")


def _is_csharp_ls(argv: list[str]) -> bool:
    return _executable_name(argv) == "csharp-ls"


def _has_csharp_project(root: str) -> bool:
    for _dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if any(fn.lower().endswith(LSP_PROJECT_SUFFIXES) for fn in filenames):
            return True
    return False


class LSPClient:
    """单个语言服务器进程。请求串行化（锁），响应按 id 匹配，通知丢弃。"""

    def __init__(
        self,
        root: str,
        ext: str,
        argv: list[str],
        server: str | None = None,
        initialization: dict | None = None,
    ):
        self.root, self.ext, self.argv = root, ext, argv
        self.server = server or server_id(argv, ext)
        self.initialization = initialization or {}
        self.proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._next_id = 0
        self._opened: set[str] = set()
        self._stderr = bytearray()
        self._stderr_task: asyncio.Task | None = None
        self.last_used = time.monotonic()

    def _initialize_params(self) -> dict:
        workspace: dict = {"configuration": True}
        params = {
            "processId": os.getpid(),
            "rootUri": _path_to_uri(self.root),
            "initializationOptions": self.initialization,
            "capabilities": {
                "window": {"workDoneProgress": True},
                "workspace": workspace,
                "textDocument": {"synchronization": {"didOpen": True}},
            },
        }
        if self.server != "omnisharp":
            folders = [
                {"uri": _path_to_uri(self.root), "name": os.path.basename(self.root)}
            ]
            params["workspaceFolders"] = folders
            workspace["workspaceFolders"] = True
        return params

    async def start(self) -> None:
        if _is_csharp_ls(self.argv) and not _has_csharp_project(self.root):
            raise RuntimeError(
                f"csharp-ls 需要数据源内至少一个 .sln/.csproj/.fsproj 文件：{self.root}"
            )
        started = time.monotonic()
        logger.debug(f"LSP spawning: {self.argv} (root={self.root})")
        self.proc = await _spawn_lsp_process(self.argv, self.root)
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            result = await self._request(
                "initialize",
                self._initialize_params(),
                timeout=60,
            )
            if result is None:
                raise RuntimeError("initialize 失败")
            await self._notify("initialized", {})
            if self.initialization:
                await self._notify(
                    "workspace/didChangeConfiguration",
                    {"settings": self.initialization},
                )
        except Exception:
            await self.stop()
            raise
        logger.debug(
            f"LSP initialized: {self.ext} @ {self.root} in {time.monotonic() - started:.1f}s"
        )

    async def _drain_stderr(self) -> None:
        proc = self.proc
        if not proc or not proc.stderr:
            return
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            self._stderr.extend(chunk)
            if len(self._stderr) > LSP_STDERR_CAP:
                del self._stderr[:-LSP_STDERR_CAP]

    def _process_error(self, method: str) -> RuntimeError:
        code = self.proc.returncode if self.proc else None
        message = f"LSP {method} 失败：语言服务器连接已关闭"
        if code is not None:
            message += f"（退出码 {code}）"
        detail = self._stderr.decode("utf-8", errors="replace").strip()
        if detail:
            message += f"\nstderr 末尾：\n{detail[-4000:]}"
        return RuntimeError(message)

    async def stop(self) -> None:
        proc = self.proc
        if not proc:
            return
        try:
            if proc.returncode is None:
                logger.debug(f"LSP stopping: {self.ext} @ {self.root}")
                try:
                    await self._request("shutdown", {}, timeout=3)
                    await self._notify("exit", None)
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except Exception as exc:  # noqa: BLE001 - 关闭失败时仍需终止并释放进程
                    logger.debug(f"LSP graceful shutdown failed, killing: {exc}")
                    await _kill_process_tree(proc)
                    await asyncio.wait_for(proc.wait(), timeout=3)
        finally:
            self.proc = None
            if proc.stdin:
                proc.stdin.close()
            transport = getattr(proc, "_transport", None)
            if transport:
                transport.close()
            task, self._stderr_task = self._stderr_task, None
            if task:
                with contextlib.suppress(asyncio.TimeoutError, OSError):
                    await asyncio.wait_for(task, timeout=1)

    # ---- 协议底层 ----

    @staticmethod
    def _frame(payload: dict) -> bytes:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body

    def _send_raw(
        self, method: str, params, is_notify: bool, msg_id: int | None = None
    ) -> None:
        if not (self.proc and self.proc.stdin):
            raise RuntimeError("服务器进程未运行")
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not is_notify:
            payload["id"] = msg_id
        self.proc.stdin.write(self._frame(payload))

    async def _send_response(
        self, msg_id, result=None, error: dict | None = None
    ) -> None:
        if not (self.proc and self.proc.stdin):
            raise RuntimeError("服务器进程未运行")
        payload = {"jsonrpc": "2.0", "id": msg_id}
        if error is None:
            payload["result"] = result
        else:
            payload["error"] = error
        self.proc.stdin.write(self._frame(payload))
        await self.proc.stdin.drain()

    async def _handle_server_request(self, message: dict) -> None:
        method = message.get("method", "")
        params = message.get("params") or {}
        if method == "workspace/configuration":
            result = [
                _configuration_value(self.initialization, item.get("section"))
                for item in params.get("items", [])
            ]
        elif method == "window/workDoneProgress/create":
            result = None
        elif method == "workspace/workspaceFolders":
            result = self._initialize_params().get("workspaceFolders")
        else:
            await self._send_response(
                message.get("id"),
                error={"code": -32601, "message": f"Method not found: {method}"},
            )
            return
        await self._send_response(message.get("id"), result=result)

    async def _notify(self, method: str, params) -> None:
        self._send_raw(method, params, is_notify=True)
        await self.proc.stdin.drain()

    async def _request(self, method: str, params, timeout: float = 60):
        async with self._lock:
            if not self.proc or not self.proc.stdout:
                raise RuntimeError("服务器进程未运行")
            if self.proc.returncode is not None:
                raise self._process_error(method)
            self._next_id += 1
            msg_id = self._next_id
            self.last_used = time.monotonic()
            self._send_raw(method, params, is_notify=False, msg_id=msg_id)
            await self.proc.stdin.drain()
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        f"LSP {method} timed out after {timeout}s (root={self.root})"
                    )
                    raise TimeoutError(f"{method} 响应超时（{timeout}s）")
                try:
                    header = await asyncio.wait_for(
                        self.proc.stdout.readuntil(b"\r\n\r\n"), timeout=remaining
                    )
                    match = re.search(rb"Content-Length: (\d+)", header)
                    if not match:
                        raise RuntimeError(f"LSP {method} 响应缺少 Content-Length")
                    body = await asyncio.wait_for(
                        self.proc.stdout.readexactly(int(match.group(1))),
                        timeout=remaining,
                    )
                except asyncio.IncompleteReadError as exc:
                    raise self._process_error(method) from exc
                message = json.loads(body.decode("utf-8", errors="replace"))
                if "method" in message:
                    if "id" in message:
                        await self._handle_server_request(message)
                    continue
                if message.get("id") != msg_id:
                    continue
                if "error" in message:
                    logger.warning(
                        f"LSP {method} server error: {message['error'].get('message', '')}"
                    )
                    raise RuntimeError(
                        f"{method}: {message['error'].get('message', '')}"
                    )
                logger.debug(
                    f"LSP {method} ok in {time.monotonic() - (deadline - timeout):.2f}s"
                )
                return message.get("result")

    # ---- 检索用三个能力 ----

    async def _ensure_open(self, path: str) -> None:
        uri = _path_to_uri(path)
        if uri in self._opened:
            return

        # 异步上下文里不用阻塞 open：丢到线程读，避免大文件卡住事件循环
        def _read() -> str:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read()

        text = await asyncio.to_thread(_read)
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        await self._notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": LANGUAGE_IDS.get(ext, "plaintext"),
                    "version": 1,
                    "text": text,
                }
            },
        )
        self._opened.add(uri)

    async def symbols(self, query: str, timeout: float = 90, path: str | None = None):
        if path:
            await self._ensure_open(path)
        return await self._request(
            "workspace/symbol", {"query": query}, timeout=timeout
        )

    async def definition(self, path: str, line0: int, char: int):
        await self._ensure_open(path)
        return await self._request(
            "textDocument/definition",
            {
                "textDocument": {"uri": _path_to_uri(path)},
                "position": {"line": line0, "character": char},
            },
        )

    async def references(
        self, path: str, line0: int, char: int, include_decl: bool = False
    ):
        await self._ensure_open(path)
        return await self._request(
            "textDocument/references",
            {
                "textDocument": {"uri": _path_to_uri(path)},
                "position": {"line": line0, "character": char},
                "context": {"includeDeclaration": include_decl},
            },
        )


class LSPManager:
    """按 (root, ext) 管理客户端；空闲自动关闭。"""

    def __init__(self, idle_minutes: int = 10):
        self.idle_minutes = idle_minutes
        self._clients: dict[tuple[str, str], LSPClient] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._broken: dict[tuple[str, str], str] = {}
        self._reaper: asyncio.Task | None = None

    def _lock_for(self, key: tuple[str, str]) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def get(
        self,
        source_root: str,
        ext: str,
        overrides: dict[str, list[list[str]]],
        path: str | None = None,
        initialization: dict[str, dict] | None = None,
    ) -> LSPClient:
        argv, tried = probe(ext, overrides)
        if argv is None:
            raise RuntimeError(no_server_msg(ext, tried))
        server = server_id(argv, ext)
        root = resolve_root(source_root, path, server, ext)
        configs = initialization or {}
        options = configs[server] if server in configs else configs.get(ext, {})
        key = (root, server)
        async with self._lock_for(key):
            if key in self._broken:
                raise RuntimeError(
                    f"LSP 已标记不可用，请修正后重载插件：{self._broken[key]}"
                )
            client = self._clients.get(key)
            if client and client.proc and client.proc.returncode is None:
                return client
            if client is not None:
                code = client.proc.returncode if client.proc else "none"
                await client.stop()
                self._clients.pop(key, None)
                error = f"LSP 进程已退出（code={code}）：{server} @ {root}"
                self._broken[key] = error
                raise RuntimeError(error)
            client = LSPClient(root, ext, argv, server=server, initialization=options)
            try:
                await client.start()
            except Exception as exc:
                self._broken[key] = str(exc)
                raise
            self._clients[key] = client
            self._ensure_reaper()
            return client

    def _ensure_reaper(self) -> None:
        if self._reaper and not self._reaper.done():
            return
        self._reaper = asyncio.create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            for key, client in list(self._clients.items()):
                if now - client.last_used > self.idle_minutes * 60:
                    logger.debug(f"LSP idle close: {key[1]} @ {key[0]}")
                    self._clients.pop(key, None)
                    try:
                        await client.stop()
                    except Exception as exc:  # noqa: BLE001 - 回收阶段兜住，不影响其他客户端
                        logger.debug(f"LSP idle close failed: {exc}")

    async def shutdown(self) -> None:
        if self._reaper:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        for client in list(self._clients.values()):
            try:
                await client.stop()
            except Exception as exc:  # noqa: BLE001 - 卸载阶段兜住，保证全部清理
                logger.debug(f"LSP shutdown failed: {exc}")
        self._clients.clear()
        self._broken.clear()

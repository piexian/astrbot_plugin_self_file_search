"""通用 LSP 后端：自动探测环境 PATH 里的语言服务器，按数据源根目录管理进程生命周期。

"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
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
    "cs": "csharp", "go": "go", "rs": "rust", "ts": "typescript", "tsx": "typescriptreact",
    "js": "javascript", "py": "python", "c": "c", "cpp": "cpp", "cc": "cpp", "h": "c",
    "lua": "lua", "php": "php", "rb": "ruby", "zig": "zig", "dart": "dart",
}

SYMBOL_KINDS = {
    1: "file", 2: "module", 3: "namespace", 4: "package", 5: "class", 6: "method",
    7: "property", 8: "field", 9: "constructor", 10: "enum", 11: "interface",
    12: "function", 13: "variable", 14: "constant", 15: "string", 22: "struct",
    23: "event", 25: "type-parameter",
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


def probe(ext: str, overrides: dict[str, list[list[str]]]) -> tuple[list[str] | None, list[str]]:
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
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
            if ext in LSP_CANDIDATES:
                counts[ext] = counts.get(ext, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _path_to_uri(path: str) -> str:
    return urllib.parse.quote("file:///" + os.path.abspath(path).replace("\\", "/"), safe="/:")


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


class LSPClient:
    """单个语言服务器进程。请求串行化（锁），响应按 id 匹配，通知丢弃。"""

    def __init__(self, root: str, ext: str, argv: list[str]):
        self.root, self.ext, self.argv = root, ext, argv
        self.proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._next_id = 0
        self._opened: set[str] = set()
        self.last_used = time.monotonic()

    async def start(self) -> None:
        started = time.monotonic()
        logger.debug(f"LSP spawning: {self.argv} (root={self.root})")
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self.root,
        )
        result = await self._request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": _path_to_uri(self.root),
                "workspaceFolders": [
                    {"uri": _path_to_uri(self.root), "name": os.path.basename(self.root)}
                ],
                "capabilities": {},
            },
            timeout=60,
        )
        if result is None:
            await self.stop()
            raise RuntimeError("initialize 失败")
        await self._notify("initialized", {})
        logger.debug(
            f"LSP initialized: {self.ext} @ {self.root} in {time.monotonic() - started:.1f}s"
        )

    async def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc and proc.returncode is None:
            logger.debug(f"LSP stopping: {self.ext} @ {self.root}")
            try:
                self._send_raw("shutdown", {}, is_notify=False, msg_id=0)
                self._send_raw("exit", None, is_notify=True)
                await asyncio.wait_for(proc.wait(), timeout=3)
                return
            except Exception:
                pass
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    # ---- 协议底层 ----

    @staticmethod
    def _frame(payload: dict) -> bytes:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body

    def _send_raw(self, method: str, params, is_notify: bool, msg_id: int | None = None) -> None:
        if not (self.proc and self.proc.stdin):
            raise RuntimeError("服务器进程未运行")
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not is_notify:
            payload["id"] = msg_id
        self.proc.stdin.write(self._frame(payload))

    async def _notify(self, method: str, params) -> None:
        self._send_raw(method, params, is_notify=True)
        await self.proc.stdin.drain()

    async def _request(self, method: str, params, timeout: float = 60):
        async with self._lock:
            if not (self.proc and self.proc.returncode is None and self.proc.stdout):
                raise RuntimeError("服务器进程未运行")
            self._next_id += 1
            msg_id = self._next_id
            self.last_used = time.monotonic()
            self._send_raw(method, params, is_notify=False, msg_id=msg_id)
            await self.proc.stdin.drain()
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(f"LSP {method} timed out after {timeout}s (root={self.root})")
                    raise TimeoutError(f"{method} 响应超时（{timeout}s）")
                header = await asyncio.wait_for(
                    self.proc.stdout.readuntil(b"\r\n\r\n"), timeout=remaining
                )
                match = re.search(rb"Content-Length: (\d+)", header)
                body = await asyncio.wait_for(
                    self.proc.stdout.readexactly(int(match.group(1))), timeout=remaining
                )
                message = json.loads(body.decode("utf-8", errors="replace"))
                if message.get("id") != msg_id:
                    continue  # 服务器通知/服务端请求，检索场景不需要，直接丢
                if "error" in message:
                    logger.warning(f"LSP {method} server error: {message['error'].get('message', '')}")
                    raise RuntimeError(f"{method}: {message['error'].get('message', '')}")
                logger.debug(f"LSP {method} ok in {time.monotonic() - (deadline - timeout):.2f}s")
                return message.get("result")

    # ---- 检索用三个能力 ----

    async def _ensure_open(self, path: str) -> None:
        uri = _path_to_uri(path)
        if uri in self._opened:
            return
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
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

    async def symbols(self, query: str, timeout: float = 90):
        return await self._request("workspace/symbol", {"query": query}, timeout=timeout)

    async def definition(self, path: str, line0: int, char: int):
        await self._ensure_open(path)
        return await self._request(
            "textDocument/definition",
            {
                "textDocument": {"uri": _path_to_uri(path)},
                "position": {"line": line0, "character": char},
            },
        )

    async def references(self, path: str, line0: int, char: int, include_decl: bool = False):
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
        self._reaper: asyncio.Task | None = None

    def _lock_for(self, key: tuple[str, str]) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def get(self, root: str, ext: str, overrides: dict[str, list[list[str]]]) -> LSPClient:
        key = (root, ext)
        async with self._lock_for(key):
            client = self._clients.get(key)
            if client and client.proc and client.proc.returncode is None:
                return client
            if client is not None:
                logger.warning(
                    f"LSP process died (code={client.proc.returncode if client.proc else 'none'}), respawning: {ext} @ {root}"
                )
            argv, tried = probe(ext, overrides)
            if argv is None:
                raise RuntimeError(no_server_msg(ext, tried))
            client = LSPClient(root, ext, argv)
            await client.start()
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
                    except Exception:
                        pass

    async def shutdown(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        for client in list(self._clients.values()):
            try:
                await client.stop()
            except Exception:
                pass
        self._clients.clear()

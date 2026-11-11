import asyncio
import json
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO.parent))
astrbot_api = types.ModuleType("astrbot.api")
astrbot_api.logger = logging.getLogger("test-lsp")
sys.modules["astrbot.api"] = astrbot_api
from astrbot_plugin_self_file_search import lsp  # noqa: E402


class FakeStdin:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, stdout=None, stderr=None, returncode=None):
        self.stdout = stdout
        self.stderr = stderr
        self.stdin = FakeStdin()
        self.returncode = returncode
        self._transport = FakeTransport()
        self.killed = False

    def kill(self):
        self.killed = True


def decode_frames(data):
    messages = []
    while data:
        header, separator, rest = data.partition(b"\r\n\r\n")
        if not separator:
            break
        length = int(header.split(b"Content-Length: ", 1)[1].split(b"\r\n", 1)[0])
        messages.append(json.loads(rest[:length].decode("utf-8")))
        data = rest[length:]
    return messages


class ProjectDetectionTests(unittest.TestCase):
    def test_csharp_project_detection_supports_nested_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(lsp._has_csharp_project(str(root)))
            project = root / "current" / "Elin.csproj"
            project.parent.mkdir()
            project.write_text("<Project />", encoding="utf-8")
            self.assertTrue(lsp._has_csharp_project(str(root)))

    def test_csharp_ls_detection_accepts_windows_executable_name(self):
        self.assertTrue(lsp._is_csharp_ls([r"C:\Tools\csharp-ls.exe"]))
        self.assertFalse(lsp._is_csharp_ls(["omnisharp", "-lsp"]))
        self.assertFalse(lsp._is_csharp_ls([]))

    def test_roslyn_is_the_default_csharp_server(self):
        self.assertEqual(
            lsp.LSP_CANDIDATES["cs"][0],
            ["roslyn-language-server", "--stdio", "--autoLoadProjects"],
        )
        self.assertEqual(lsp.LSP_CANDIDATES["cs"][1], ["csharp-ls"])

    @unittest.skipUnless(os.name == "nt", "Windows drive URI regression")
    def test_roslyn_lowercase_encoded_drive_uri_is_normalized(self):
        uri = "file:///c%3A/Users/Test/Project/Quest.cs"
        expected = os.path.realpath(r"C:\Users\Test\Project\Quest.cs")
        self.assertEqual(lsp.uri_to_path(uri), expected)


class LspClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, root="/tmp"):
        return lsp.LSPClient(root, "cs", ["csharp-ls"])

    async def test_missing_project_fails_before_spawning(self):
        for argv in (["csharp-ls"], ["roslyn-language-server"]):
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as tmp:
                client = lsp.LSPClient(tmp, "cs", argv)
                with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as spawn:
                    with self.assertRaisesRegex(RuntimeError, "csproj"):
                        await client.start()
                spawn.assert_not_awaited()

    async def test_valid_response_frame_is_decoded(self):
        reader = asyncio.StreamReader()
        reader.feed_data(
            lsp.LSPClient._frame({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
        )
        reader.feed_eof()
        proc = FakeProcess(stdout=reader)
        client = self.make_client()
        client.proc = proc

        result = await client._request("test", {})

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(decode_frames(bytes(proc.stdin.data))), 1)

    async def test_error_response_raises_server_message(self):
        reader = asyncio.StreamReader()
        reader.feed_data(
            lsp.LSPClient._frame(
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "broken"}}
            )
        )
        reader.feed_eof()
        client = self.make_client()
        client.proc = FakeProcess(stdout=reader)

        with self.assertRaisesRegex(RuntimeError, "test: broken"):
            await client._request("test", {})

    async def test_eof_error_includes_exit_code_and_stderr(self):
        class EofStream:
            async def readuntil(self, _separator):
                raise asyncio.IncompleteReadError(b"", None)

        proc = FakeProcess(stdout=EofStream(), returncode=3762504530)
        client = self.make_client()
        client.proc = proc
        client._stderr.extend(b"System.Exception: no project files found")

        with self.assertRaises(RuntimeError) as caught:
            await client._request("workspace/symbol", {"query": "Quest"})

        message = str(caught.exception)
        self.assertIn("3762504530", message)
        self.assertIn("no project files found", message)
        self.assertNotIn("0 bytes read", message)

    async def test_eof_during_read_includes_process_diagnostics(self):
        proc = FakeProcess()

        class EofStream:
            async def readuntil(self, _separator):
                proc.returncode = 3762504530
                raise asyncio.IncompleteReadError(b"", None)

        proc.stdout = EofStream()
        client = self.make_client()
        client.proc = proc
        client._stderr.extend(b"System.Exception: read failed")

        with self.assertRaisesRegex(RuntimeError, "read failed"):
            await client._request("workspace/symbol", {"query": "Quest"})

    async def test_stderr_buffer_keeps_only_the_tail(self):
        chunks = iter((b"a" * 12000, b"b" * 12000, b""))

        class Stderr:
            async def read(self, _size):
                return next(chunks)

        client = self.make_client()
        client.proc = FakeProcess(stderr=Stderr())

        await client._drain_stderr()

        self.assertEqual(len(client._stderr), lsp.LSP_STDERR_CAP)
        self.assertTrue(client._stderr.endswith(b"b" * 4000))

    async def test_graceful_stop_uses_protocol_and_closes_transport(self):
        proc = FakeProcess()
        proc.wait = AsyncMock(return_value=0)
        client = self.make_client()
        client.proc = proc
        client._request = AsyncMock(return_value=None)
        client._notify = AsyncMock()

        await client.stop()

        client._request.assert_awaited_once_with("shutdown", {}, timeout=3)
        client._notify.assert_awaited_once_with("exit", None)
        self.assertTrue(proc.stdin.closed)
        self.assertTrue(proc._transport.closed)
        self.assertFalse(proc.killed)
        self.assertIsNone(client.proc)

    async def test_dead_process_is_closed_without_protocol_writes(self):
        proc = FakeProcess(returncode=1)
        client = self.make_client()
        client.proc = proc

        await client.stop()

        self.assertEqual(proc.stdin.data, b"")
        self.assertTrue(proc._transport.closed)
        self.assertIsNone(client.proc)


class ServerAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_initialization_config_is_parsed_and_validated(self):
        items = [r'rust-analyzer|{"linkedProjects":["rust-project.json"]}']
        self.assertEqual(
            lsp.parse_initialization(items),
            {"rust-analyzer": {"linkedProjects": ["rust-project.json"]}},
        )
        with self.assertRaisesRegex(ValueError, "JSON 对象"):
            lsp.parse_initialization(["gopls|[]"])
        with self.assertRaisesRegex(ValueError, "JSON 无效"):
            lsp.parse_initialization(["gopls|{"])

    def test_file_selects_nested_csharp_project_and_loose_files_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            project = source / "versions" / "current"
            source_file = project / "src" / "Quest.cs"
            source_file.parent.mkdir(parents=True)
            source_file.write_text("public class Quest {}", encoding="utf-8")
            (project / "Elin.csproj").write_text("<Project />", encoding="utf-8")

            selected = lsp.resolve_root(
                str(source), str(source_file), "csharp-ls", "cs"
            )
            fallback = lsp.resolve_root(str(source), str(source_file), "unknown", "txt")

            self.assertEqual(selected, os.path.realpath(project))
            self.assertEqual(fallback, os.path.realpath(source))

    def test_gopls_prefers_go_work_over_nested_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            module = source / "module"
            source_file = module / "src" / "main.go"
            source_file.parent.mkdir(parents=True)
            source_file.write_text("package main", encoding="utf-8")
            (source / "go.work").write_text("go 1.24", encoding="utf-8")
            (module / "go.mod").write_text("module example", encoding="utf-8")

            selected = lsp.resolve_root(str(source), str(source_file), "gopls", "go")

            self.assertEqual(selected, os.path.realpath(source))

    async def test_pyright_executable_uses_canonical_root_and_config_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            project = source / "nested"
            source_file = project / "src" / "app.py"
            source_file.parent.mkdir(parents=True)
            source_file.write_text("value = 1\n", encoding="utf-8")
            (project / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            manager = lsp.LSPManager()
            initialization = {"pyright": {"python": {"pythonPath": "python"}}}
            with (
                patch.object(
                    lsp, "probe", return_value=(["pyright-langserver", "--stdio"], [])
                ),
                patch.object(lsp.LSPClient, "start", new=AsyncMock()),
            ):
                client = await manager.get(
                    str(source),
                    "py",
                    {},
                    path=str(source_file),
                    initialization=initialization,
                )

            self.assertEqual(client.server, "pyright")
            self.assertEqual(client.root, os.path.realpath(project))
            self.assertEqual(client.initialization, initialization["pyright"])
            await manager.shutdown()

    def test_initialize_declares_only_supported_workspace_behaviour(self):
        client = lsp.LSPClient(
            "/tmp", "cs", ["csharp-ls"], initialization={"csharp": {"x": 1}}
        )
        params = client._initialize_params()
        self.assertTrue(params["capabilities"]["workspace"]["workspaceFolders"])
        self.assertTrue(params["capabilities"]["workspace"]["configuration"])
        self.assertEqual(params["initializationOptions"], {"csharp": {"x": 1}})

        omnisharp = lsp.LSPClient("/tmp", "cs", ["omnisharp"])
        params = omnisharp._initialize_params()
        self.assertNotIn("workspaceFolders", params)
        self.assertNotIn("workspaceFolders", params["capabilities"]["workspace"])

    async def test_server_request_is_answered_before_matching_response(self):
        reader = asyncio.StreamReader()
        reader.feed_data(
            lsp.LSPClient._frame(
                {
                    "jsonrpc": "2.0",
                    "id": 99,
                    "method": "workspace/configuration",
                    "params": {
                        "items": [{"section": "csharp"}, {"section": "missing"}]
                    },
                }
            )
        )
        reader.feed_data(
            lsp.LSPClient._frame({"jsonrpc": "2.0", "id": 1, "result": ["ok"]})
        )
        reader.feed_eof()
        proc = FakeProcess(stdout=reader)
        client = lsp.LSPClient(
            "/tmp",
            "cs",
            ["csharp-ls"],
            initialization={"csharp": {"solution": "Elin.csproj"}},
        )
        client.proc = proc

        result = await client._request("workspace/symbol", {"query": "Quest"})

        self.assertEqual(result, ["ok"])
        messages = decode_frames(bytes(proc.stdin.data))
        response = next(message for message in messages if message.get("id") == 99)
        self.assertEqual(response["id"], 99)
        self.assertEqual(response["result"], [{"solution": "Elin.csproj"}, None])

    async def test_unsupported_server_request_gets_method_not_found(self):
        proc = FakeProcess()
        client = lsp.LSPClient("/tmp", "cs", ["csharp-ls"])
        client.proc = proc

        await client._handle_server_request(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "client/registerCapability",
                "params": {},
            }
        )

        response = decode_frames(bytes(proc.stdin.data))[0]
        self.assertEqual(response["error"]["code"], -32601)

    async def test_symbols_open_reference_file_before_workspace_query(self):
        client = lsp.LSPClient("/tmp", "cs", ["csharp-ls"])
        client._ensure_open = AsyncMock()
        client._request = AsyncMock(return_value=[])

        await client.symbols("Quest", path="/tmp/Quest.cs")

        client._ensure_open.assert_awaited_once_with("/tmp/Quest.cs")
        client._request.assert_awaited_once()
        call = client._request.await_args
        self.assertEqual(call.args, ("workspace/symbol", {"query": "Quest"}))
        self.assertGreater(call.kwargs["timeout"], 0)
        self.assertLessEqual(call.kwargs["timeout"], 90)

    async def test_start_failure_is_not_retried_until_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = lsp.LSPManager()
            with (
                patch.object(lsp, "probe", return_value=(["csharp-ls"], [])),
                patch.object(
                    lsp.LSPClient,
                    "start",
                    new=AsyncMock(side_effect=RuntimeError("boom")),
                ) as start,
            ):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    await manager.get(tmp, "cs", {})
                with self.assertRaisesRegex(RuntimeError, "已标记不可用"):
                    await manager.get(tmp, "cs", {})
                start.assert_awaited_once()
            await manager.shutdown()

    async def test_windows_force_stop_uses_process_tree(self):
        class Killer:
            returncode = 0
            communicate = AsyncMock(return_value=(b"", b""))

        proc = types.SimpleNamespace(pid=123, kill=Mock())
        with (
            patch.object(lsp, "_is_windows", return_value=True),
            patch(
                "asyncio.create_subprocess_exec", new=AsyncMock(return_value=Killer())
            ) as spawn,
        ):
            await lsp._kill_process_tree(proc)

        self.assertEqual(
            spawn.await_args.args[:5], ("taskkill", "/pid", "123", "/T", "/F")
        )
        proc.kill.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows batch launch regression")
    async def test_windows_cmd_script_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "with space"
            directory.mkdir()
            script = directory / "fake server.cmd"
            script.write_text("@echo off\r\nexit /b 0\r\n", encoding="ascii")

            proc = await lsp._spawn_lsp_process([str(script)], str(directory))
            await asyncio.wait_for(proc.communicate(), timeout=5)

            self.assertEqual(proc.returncode, 0)


class RoslynReadinessTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, *messages, server="roslyn-language-server", eof=False):
        reader = asyncio.StreamReader()
        for message in messages:
            reader.feed_data(lsp.LSPClient._frame(message))
        if eof:
            reader.feed_eof()
        client = lsp.LSPClient("/tmp", "cs", [server])
        client.proc = FakeProcess(stdout=reader)
        return client, reader

    def reply(self, number, result):
        return {"jsonrpc": "2.0", "id": number, "result": result}

    def ready(self):
        return {"jsonrpc": "2.0", "method": "workspace/projectInitializationComplete"}

    def requests(self, client):
        return [
            message
            for message in decode_frames(bytes(client.proc.stdin.data))
            if message.get("method") == "workspace/symbol"
        ]

    async def test_cold_empty_waits_for_ready_and_retries_once(self):
        hit = [{"name": "Quest"}]
        client, _ = self.make_client(
            self.reply(1, []),
            {
                "jsonrpc": "2.0",
                "id": "config",
                "method": "workspace/configuration",
                "params": {"items": [{"section": "project"}]},
            },
            self.ready(),
            self.reply(2, hit),
        )
        client._ensure_open = AsyncMock()

        result = await client.symbols("Quest", path="/tmp/Quest.cs")

        self.assertEqual(result, hit)
        self.assertTrue(client.workspace_ready)
        self.assertEqual(len(self.requests(client)), 2)
        self.assertIn(
            {"jsonrpc": "2.0", "id": "config", "result": [None]},
            decode_frames(bytes(client.proc.stdin.data)),
        )
        client._ensure_open.assert_awaited_once_with("/tmp/Quest.cs")

    async def test_ready_empty_is_returned_without_retry(self):
        client, _ = self.make_client(self.ready(), self.reply(1, []))

        self.assertEqual(await client.symbols("missing"), [])
        self.assertTrue(client.workspace_ready)
        self.assertEqual(len(self.requests(client)), 1)

    async def test_non_roslyn_empty_is_not_retried(self):
        client, _ = self.make_client(self.reply(1, []), server="csharp-ls")

        self.assertEqual(await client.symbols("missing"), [])
        self.assertEqual(len(self.requests(client)), 1)

    async def test_cold_hit_is_returned_without_waiting(self):
        hit = [{"name": "Quest"}]
        client, _ = self.make_client(self.reply(1, hit))

        self.assertEqual(await client.symbols("Quest"), hit)
        self.assertFalse(client.workspace_ready)
        self.assertEqual(len(self.requests(client)), 1)

    async def test_retry_can_return_a_genuine_empty_result(self):
        client, _ = self.make_client(self.reply(1, []), self.ready(), self.reply(2, []))

        self.assertEqual(await client.symbols("missing"), [])
        self.assertEqual(len(self.requests(client)), 2)

    async def test_loading_timeout_is_bounded_and_client_can_be_used_later(self):
        client, reader = self.make_client(self.reply(1, []))

        with self.assertRaisesRegex(TimeoutError, "项目加载尚未完成"):
            await client.symbols("Quest", timeout=0.02)

        self.assertEqual(len(self.requests(client)), 1)
        self.assertFalse(client._lock.locked())
        hit = [{"name": "Quest"}]
        reader.feed_data(lsp.LSPClient._frame(self.ready()))
        reader.feed_data(lsp.LSPClient._frame(self.reply(2, hit)))
        self.assertEqual(await client.symbols("Quest"), hit)
        self.assertEqual(len(self.requests(client)), 2)

    async def test_eof_during_project_loading_is_reported_without_retry(self):
        client, _ = self.make_client(self.reply(1, []), eof=True)
        client._stderr.extend(b"project host closed")

        with self.assertRaisesRegex(RuntimeError, "project host closed"):
            await client.symbols("Quest")

        self.assertEqual(len(self.requests(client)), 1)

    async def test_server_error_is_not_retried(self):
        client, _ = self.make_client(
            {"jsonrpc": "2.0", "id": 1, "error": {"message": "broken"}}
        )

        with self.assertRaisesRegex(RuntimeError, "broken"):
            await client.symbols("Quest")

        self.assertEqual(len(self.requests(client)), 1)

    async def test_retry_uses_the_remaining_original_budget(self):
        client, _ = self.make_client()
        client._request = AsyncMock(side_effect=[[], [{"name": "Quest"}]])
        client._wait_workspace_ready = AsyncMock()
        clock = types.SimpleNamespace(monotonic=Mock(side_effect=[100, 100, 130]))

        with patch.object(lsp, "time", clock):
            await client.symbols("Quest", timeout=90)

        self.assertEqual(client._request.await_args_list[0].kwargs["timeout"], 90)
        self.assertEqual(client._request.await_args_list[1].kwargs["timeout"], 60)
        client._wait_workspace_ready.assert_awaited_once_with(190)

    async def test_start_cancellation_closes_the_new_process(self):
        client, _ = self.make_client()
        proc = client.proc
        proc.wait = AsyncMock(return_value=0)
        client._request = AsyncMock(side_effect=[asyncio.CancelledError(), None])
        with (
            patch.object(lsp, "_has_csharp_project", return_value=True),
            patch.object(lsp, "_spawn_lsp_process", new=AsyncMock(return_value=proc)),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await client.start()

        self.assertIsNone(client.proc)
        self.assertTrue(proc.stdin.closed)
        self.assertTrue(proc._transport.closed)


if __name__ == "__main__":
    unittest.main()

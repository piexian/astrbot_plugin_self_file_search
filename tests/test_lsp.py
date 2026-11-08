import asyncio
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


class LspClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, root="/tmp"):
        return lsp.LSPClient(root, "cs", ["csharp-ls"])

    async def test_missing_project_fails_before_spawning(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self.make_client(tmp)
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


if __name__ == "__main__":
    unittest.main()

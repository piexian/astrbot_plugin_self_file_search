import asyncio
import dataclasses
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


def _install_import_stubs():
    pydantic = types.ModuleType("pydantic")
    pydantic.Field = lambda *, default_factory: dataclasses.field(
        default_factory=default_factory
    )
    pydantic_dataclasses = types.ModuleType("pydantic.dataclasses")
    pydantic_dataclasses.dataclass = dataclasses.dataclass

    class FunctionTool:
        def __class_getitem__(cls, _item):
            return cls

    class ContextWrapper:
        def __class_getitem__(cls, _item):
            return cls

    class Star:
        pass

    modules = {
        "pydantic": pydantic,
        "pydantic.dataclasses": pydantic_dataclasses,
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.agent": types.ModuleType("astrbot.core.agent"),
        "astrbot.core.agent.run_context": types.ModuleType(
            "astrbot.core.agent.run_context"
        ),
        "astrbot.core.agent.tool": types.ModuleType("astrbot.core.agent.tool"),
        "astrbot.core.astr_agent_context": types.ModuleType(
            "astrbot.core.astr_agent_context"
        ),
    }
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = logging.getLogger("test")
    modules["astrbot.api"].star = types.SimpleNamespace(
        Star=Star, Context=type("Context", (), {})
    )
    modules["astrbot.core.agent.run_context"].ContextWrapper = ContextWrapper
    modules["astrbot.core.agent.tool"].FunctionTool = FunctionTool
    modules["astrbot.core.agent.tool"].ToolExecResult = object
    modules["astrbot.core.astr_agent_context"].AstrAgentContext = type(
        "AstrAgentContext", (), {}
    )
    sys.modules.update(modules)


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO.parent))
_install_import_stubs()
from astrbot_plugin_self_file_search import main  # noqa: E402


class Plugin:
    def __init__(self, root):
        self.config = {
            "sources": [{"name": "test", "path": str(root), "enabled": True}],
            "include_astrbot": False,
        }
        self.kv_ready = False


class FileSearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.plugin = Plugin(self.root)
        self.search = main.SelfSearchTool(plugin_ref=self.plugin)
        self.glob = main.SelfGlobTool(plugin_ref=self.plugin)

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    async def call_search(self, pattern, **kwargs):
        return await self.search.call(None, source="test", pattern=pattern, **kwargs)

    async def test_relative_path_and_basename_globs(self):
        self.write("versions/lang_23350/Thing.json", '"name": "橡皮鸭"\n')
        self.write("other/Thing.json", '"name": "other"\n')

        relative = await self.call_search(
            "橡皮鸭", file_glob="versions/lang_23350/Thing.json"
        )
        basename = await self.call_search("橡皮鸭", file_glob="Thing.json")
        windows_path = await self.call_search(
            "橡皮鸭", file_glob=r"versions\lang_23350\Thing.json"
        )

        self.assertIn("命中 1 行", relative)
        self.assertIn("versions/lang_23350/Thing.json:1", relative)
        self.assertIn("命中 1 行", basename)
        self.assertIn("versions/lang_23350/Thing.json:1", windows_path)

    async def test_comma_separated_globs(self):
        self.write("Thing.json", "needle json\n")
        self.write("Code.cs", "needle cs\n")
        self.write("Notes.txt", "needle txt\n")

        result = await self.call_search("needle", file_glob="*.xlsx, *.json,*.cs")

        self.assertIn("Thing.json:1", result)
        self.assertIn("Code.cs:1", result)
        self.assertNotIn("Notes.txt", result)

    async def test_zero_candidates_and_content_miss_are_distinct(self):
        self.write("Thing.json", "actual\n")

        no_files = await self.call_search("needle", file_glob="Missing.json")
        no_content = await self.call_search("needle", file_glob="Thing.json")

        self.assertIn("没有候选文件", no_files)
        self.assertIn("文件内无命中", no_content)
        self.assertIn("1/1 个候选文件", no_content)

    async def test_incomplete_empty_result_is_not_definitive(self):
        self.write("Thing.json", "needle\n")
        original = main.SEARCH_TIMEOUT_SECONDS
        main.SEARCH_TIMEOUT_SECONDS = -1
        try:
            result = await self.call_search("needle", file_glob="Thing.json")
        finally:
            main.SEARCH_TIMEOUT_SECONDS = original

        self.assertIn("扫描不完整", result)
        self.assertIn("尚不能确定", result)

    async def test_offset_beyond_results_reports_page_state(self):
        self.write("Thing.json", "needle\n")

        result = await self.call_search("needle", offset=1)

        self.assertIn("本页无内容", result)
        self.assertIn("offset", result)

    async def test_regex_operators_need_the_explicit_flag(self):
        self.write("Code.cs", "foo\nbar\nfoo|bar\n")

        literal = await self.call_search("foo|bar", file_glob="Code.cs")
        regex = await self.call_search("foo|bar", file_glob="Code.cs", is_regex=True)

        self.assertIn("命中 1 行", literal)
        self.assertIn("Code.cs:3", literal)
        self.assertNotIn("Code.cs:1", literal)
        self.assertIn("命中 3 行", regex)
        self.assertIn("Code.cs:1", regex)
        self.assertIn("Code.cs:2", regex)

    async def test_glob_supports_relative_paths_and_multiple_patterns(self):
        self.write("versions/lang_23350/Thing.json", "{}\n")
        self.write("versions/lang_23350/Chara.json", "{}\n")
        self.write("other/Thing.json", "{}\n")

        result = await self.glob.call(
            None,
            source="test",
            pattern="versions/lang_23350/Thing.json,versions/lang_23350/Chara.json",
        )

        self.assertIn("匹配 2 个文件", result)
        self.assertIn("versions/lang_23350/Thing.json", result)
        self.assertIn("versions/lang_23350/Chara.json", result)
        self.assertNotIn("other/Thing.json", result)


class ReadRangeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "Code.cs").write_text(
            "".join(f"line {number}\n" for number in range(1, 1001)), encoding="utf-8"
        )
        self.tool = main.SelfReadTool(plugin_ref=Plugin(self.root))

    async def read(self, **kwargs):
        return await self.tool.call(None, source="test", path="Code.cs", **kwargs)

    async def test_complete_explicit_range_has_only_path_and_numbered_content(self):
        result = await self.read(start_line=315, end_line=365)
        expected = "[test] Code.cs\n" + "".join(
            f"{number}: line {number}\n" for number in range(315, 366)
        )

        self.assertEqual(result, expected)

    async def test_truncated_explicit_range_keeps_its_requested_end(self):
        result = await self.read(start_line=315, end_line=365, max_chars=30)

        self.assertIn("315: line 315\n316: line 316\n", result)
        self.assertIn("start_line=317&end_line=365", result)
        self.assertNotIn("共 1000 行", result)
        self.assertNotIn("还剩", result)

    async def test_negative_end_line_is_also_a_bounded_range(self):
        result = await self.read(start_line=998, end_line=-2)

        self.assertEqual(result, "[test] Code.cs\n998: line 998\n999: line 999\n")

    async def test_completed_batch_ranges_have_no_partial_or_range_headers(self):
        result = await self.tool.call(
            None,
            source="test",
            files=[
                {"path": "Code.cs", "start_line": 315, "end_line": 315},
                {"path": "Code.cs", "start_line": 365, "end_line": 365},
            ],
        )

        self.assertIn("315: line 315", result)
        self.assertIn("365: line 365", result)
        for redundant in ("共 1000 行", "显示", "Partial", "续读", "还剩"):
            self.assertNotIn(redundant, result)

    async def test_unbounded_read_keeps_file_paging_hints(self):
        result = await self.read(start_line=315, max_chars=30)

        self.assertIn("共 1000 行", result)
        self.assertIn("显示 315-316 行", result)
        self.assertIn("start_line=317&end_line=1000", result)

    async def test_global_output_cap_does_not_invent_an_end_of_file_target(self):
        with patch.object(main, "SEARCH_OUTPUT_CAP", 200):
            result = await self.read(start_line=315, end_line=365)

        self.assertIn("输出超限", result)
        self.assertIn("保持原终点", result)
        self.assertNotIn("end_line=1000", result)


class SymbolToolBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "Quest.cs").write_text("class Quest {}\n", encoding="utf-8")
        plugin = Plugin(self.root)
        plugin._lsp_ext_cache = {}
        self.tool = main.SelfSymbolsTool(plugin_ref=plugin)

    async def test_initialization_and_query_share_one_budget(self):
        client = types.SimpleNamespace(
            workspace_ready=True, symbols=AsyncMock(return_value=[])
        )
        clock = [100]

        async def initialize(*_args, **_kwargs):
            clock[0] = 130
            return client

        with (
            patch.object(
                main, "time", types.SimpleNamespace(monotonic=lambda: clock[0])
            ),
            patch.object(main, "_lsp_client", new=initialize),
        ):
            result = await self.tool.call(None, source="test", query="missing")

        self.assertEqual(client.symbols.await_args.kwargs["timeout"], 60)
        self.assertIn("命中 0 条", result)
        self.assertNotIn("仍在建立索引", result)
        self.assertNotIn("稍后重试", result)

    async def test_timeout_reports_the_actual_phase_instead_of_zero_hits(self):
        async def pending(*_args, **_kwargs):
            await asyncio.get_running_loop().create_future()

        for ready, expected in (
            (None, "初始化尚未完成"),
            (False, "项目加载尚未完成"),
            (True, "符号查询超时"),
        ):
            with self.subTest(ready=ready):
                client = types.SimpleNamespace(
                    workspace_ready=ready, symbols=AsyncMock(side_effect=pending)
                )
                initialize = (
                    AsyncMock(side_effect=pending)
                    if ready is None
                    else AsyncMock(return_value=client)
                )
                with (
                    patch.object(main, "SYMBOL_QUERY_TIMEOUT_SECONDS", 0.02),
                    patch.object(main, "_lsp_client", new=initialize),
                ):
                    result = await self.tool.call(None, source="test", query="Quest")

                self.assertTrue(result.startswith("error:"))
                self.assertIn(expected, result)
                self.assertNotIn("命中 0 条", result)
                initialize.assert_awaited_once()
                if ready is None:
                    client.symbols.assert_not_awaited()
                else:
                    client.symbols.assert_awaited_once()

    async def test_both_timeout_types_preserve_project_loading_phase(self):
        for error_type in (TimeoutError, asyncio.TimeoutError):
            with self.subTest(error_type=error_type):
                client = types.SimpleNamespace(
                    workspace_ready=False,
                    symbols=AsyncMock(side_effect=error_type("loading")),
                )
                with patch.object(
                    main, "_lsp_client", new=AsyncMock(return_value=client)
                ):
                    result = await self.tool.call(None, source="test", query="Quest")

                self.assertIn("项目加载尚未完成", result)
                self.assertNotIn("符号搜索失败", result)
                client.symbols.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

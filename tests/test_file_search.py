import dataclasses
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()

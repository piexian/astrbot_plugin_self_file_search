import sqlite3  # noqa: I001 - 先导入 test_file_search 以安装 AstrBot 导入桩
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_file_search import Plugin

from astrbot_plugin_self_file_search import main, search_index


class IndexPlugin(Plugin):
    """把索引放到临时目录，避免在仓库根目录生成 data/。"""

    def __init__(self, root: Path, index_dir: Path) -> None:
        super().__init__(root)
        self.search_index_path = str(index_dir)


class FtsSearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.root = base / "root"
        self.index_dir = base / "index"
        self.root.mkdir()
        self.index_dir.mkdir()
        self.plugin = IndexPlugin(self.root, self.index_dir)
        self.search = main.SelfSearchTool(plugin_ref=self.plugin)

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    async def call(self, pattern: str, **kwargs):
        return await self.search.call(None, source="test", pattern=pattern, **kwargs)

    async def test_exact_is_the_default_and_still_works(self):
        self.write("Thing.json", "needle\n")

        default = await self.call("needle")
        explicit = await self.call("needle", search_mode="exact")

        self.assertIn("[test] content 模式，命中 1 行", default)
        self.assertIn("Thing.json:1", default)
        self.assertNotIn("fts5", default)
        self.assertEqual(default, explicit)

    async def test_unknown_search_mode_is_an_explicit_error(self):
        self.write("a.py", "needle\n")

        result = await self.call("needle", search_mode="fuzzy")

        self.assertTrue(result.startswith("error:"))
        self.assertIn("exact/fts5", result)

    async def test_fts5_files_ranks_by_relevance_and_creates_index(self):
        self.write("small.py", "target target\n")
        self.write("large.py", "padding " * 400 + "target\n")
        self.write("notes.txt", "target\n")

        result = await self.call(
            "target", mode="files", search_mode="fts5", file_glob="*.py"
        )

        self.assertIn("[test] fts5 files 模式", result)
        self.assertIn("按 bm25 相关性排序", result)
        self.assertIn("small.py", result)
        self.assertIn("large.py", result)
        self.assertNotIn("notes.txt", result)
        self.assertLess(result.index("small.py"), result.index("large.py"))
        self.assertTrue((self.index_dir / "search.sqlite3").is_file())

    async def test_fts5_content_reflects_modified_files(self):
        self.write("Code.cs", "alpha\nneedle one\n")
        first = await self.call("needle", mode="content", search_mode="fts5")
        self.assertIn("Code.cs:2", first)

        self.write("Code.cs", "needle two\nneedle three\nsomething else\n")
        second = await self.call("needle", mode="content", search_mode="fts5")

        self.assertIn("命中 2 行", second)
        self.assertIn("Code.cs:1", second)
        self.assertIn("Code.cs:2", second)
        self.assertNotIn("Code.cs:3", second)

    async def test_fts5_drops_deleted_files_from_the_index(self):
        self.write("a.py", "needle\n")
        self.write("b.py", "needle\n")
        (self.root / "a.py").unlink()

        result = await self.call("needle", mode="count", search_mode="fts5")

        self.assertIn("命中文件 1 个，共 1 处", result)
        self.assertIn("b.py: 1", result)
        self.assertNotIn("a.py", result)

    async def test_fts5_applies_glob_and_exclude_filters(self):
        self.write("keep.py", "needle\n")
        self.write("vendor/skip.py", "needle\n")
        self.write("notes.txt", "needle\n")

        result = await self.call(
            "needle",
            mode="files",
            search_mode="fts5",
            file_glob="*.py",
            exclude_glob="vendor/*",
        )

        self.assertIn("keep.py", result)
        self.assertNotIn("vendor/skip.py", result)
        self.assertNotIn("notes.txt", result)

    async def test_fts5_index_survives_glob_changes(self):
        self.write("a.py", "needle\n")
        self.write("b.txt", "needle\n")

        python_only = await self.call(
            "needle", mode="files", search_mode="fts5", file_glob="*.py"
        )
        text_only = await self.call(
            "needle", mode="files", search_mode="fts5", file_glob="*.txt"
        )

        self.assertIn("a.py", python_only)
        self.assertNotIn("b.txt", python_only)
        self.assertIn("b.txt", text_only)
        self.assertNotIn("a.py", text_only)

    async def test_fts5_reports_incomplete_candidate_reads(self):
        self.write("a.py", "x\n" * 5 + "needle\n")
        await self.call("needle", mode="files", search_mode="fts5")

        original = main.SEARCH_TIMEOUT_SECONDS
        main.SEARCH_TIMEOUT_SECONDS = -1
        try:
            result = await self.call("needle", search_mode="fts5")
        finally:
            main.SEARCH_TIMEOUT_SECONDS = original

        self.assertIn("已重读 0/1 个候选文件", result)
        self.assertIn("不能据此认定内容不存在", result)
        self.assertNotIn("索引可能过期", result)

    async def test_fts5_reports_candidates_excluded_by_filters(self):
        self.write("keep.py", "needle\n")

        result = await self.call(
            "needle", mode="files", search_mode="fts5", file_glob="*.nope"
        )

        self.assertIn("都被 file_glob/exclude_glob 排除", result)

    async def test_fts5_count_and_page_offsets(self):
        self.write("a.py", "needle\nneedle\nneedle\n")
        self.write("b.py", "needle\n")

        content = await self.call("needle", search_mode="fts5", max_matches=1, offset=1)
        counts = await self.call(
            "needle", mode="count", search_mode="fts5", max_matches=1, offset=1
        )

        self.assertIn("a.py:2", content)
        self.assertNotIn("a.py:1:", content)
        self.assertNotIn("a.py:3", content)
        self.assertIn("还剩 2 条", content)
        self.assertIn("offset=2 起", content)
        self.assertIn("命中文件 2 个，共 4 处", counts)
        self.assertIn("b.py: 1", counts)
        self.assertNotIn("还剩", counts)

    async def test_fts5_keeps_context_lines(self):
        self.write("a.py", "before\nneedle\nafter\n")

        result = await self.call("needle", search_mode="fts5", context_lines=1)

        self.assertIn("a.py-1- before", result)
        self.assertIn("a.py:2: needle", result)
        self.assertIn("a.py-3- after", result)

    async def test_fts5_treats_match_operators_as_literals(self):
        literals = (
            "foo AND bar",
            "foo OR bar",
            "NEAR(foo bar)",
            'he said "hi"',
            "path*",
            "col:value",
            "(parens)",
            "-dash",
        )
        self.write("operators.txt", "\n".join(literals) + "\n")

        for number, pattern in enumerate(literals, 1):
            with self.subTest(pattern=pattern):
                result = await self.call(pattern, mode="content", search_mode="fts5")
                self.assertTrue(result.startswith("[test] fts5 content 模式"))
                found = {
                    line.split(": ", 1)[0]
                    for line in result.splitlines()
                    if line.startswith("operators.txt:")
                }
                self.assertEqual(found, {f"operators.txt:{number}"})

    async def test_short_pattern_degrades_with_a_visible_note(self):
        self.write("Thing.json", "橡皮鸭\n")

        result = await self.call("橡皮", search_mode="fts5")

        self.assertTrue(result.startswith("[fts5 降级]"))
        self.assertIn("少于 3 字符", result)
        self.assertIn("已改用 exact 扫描", result)
        self.assertIn("Thing.json:1", result)

    async def test_case_sensitive_request_degrades_instead_of_changing_meaning(self):
        self.write("a.py", "Needle\nneedle\n")

        result = await self.call("needle", search_mode="fts5", case_sensitive=True)

        self.assertTrue(result.startswith("[fts5 降级]"))
        self.assertIn("case_sensitive=true", result)
        self.assertIn("命中 1 行", result)
        self.assertIn("a.py:2", result)
        self.assertNotIn("a.py:1", result)

    async def test_regex_is_rejected_with_direction_to_exact_mode(self):
        self.write("a.py", "foo\nbar\n")

        result = await self.call("foo|bar", search_mode="fts5", is_regex=True)

        self.assertTrue(result.startswith("error:"))
        self.assertIn("is_regex=true", result)
        self.assertIn("search_mode=exact", result)
        self.assertNotIn("[fts5 降级]", result)

    async def test_fts5_pipe_pattern_uses_exact_correction_feedback(self):
        self.write("a.py", "alpha\nbeta\n")

        result = await self.call("alpha|beta", search_mode="fts5")

        self.assertTrue(result.startswith("[fts5 降级]"))
        self.assertIn("fts5 只支持字面量", result)
        self.assertIn("正则预检发现 2 行", result)
        self.assertIn("search_mode=exact", result)
        self.assertIn("候选预览", result)

    async def test_fts5_ambiguous_pipe_does_not_recommend_blind_regex(self):
        self.write("a.py", "alpha\n")

        result = await self.call("a|", search_mode="fts5")

        self.assertTrue(result.startswith("[fts5 降级]"))
        self.assertIn("空的正则候选", result)
        self.assertIn("不要直接设置 is_regex=true", result)
        self.assertNotIn("候选预览", result)

    async def test_index_uses_the_plugin_data_directory(self):
        self.write("a.py", "needle\n")
        del self.plugin.search_index_path

        with patch.object(main.star, "StarTools", create=True) as star_tools:
            star_tools.get_data_dir.return_value = self.index_dir
            result = await self.call("needle", mode="files", search_mode="fts5")

        star_tools.get_data_dir.assert_called_once_with(main.PLUGIN_NAME)
        self.assertIn("a.py", result)
        self.assertTrue((self.index_dir / "search.sqlite3").is_file())

    async def test_unwritable_index_falls_back_to_exact(self):
        self.write("a.py", "needle\n")
        self.plugin.search_index_path = str(self.root / "missing-dir")

        result = await self.call("needle", search_mode="fts5")

        self.assertTrue(result.startswith("[fts5 降级]"))
        self.assertIn("已改用 exact 扫描", result)
        self.assertIn("a.py:1", result)

    async def test_missing_trigram_tokenizer_falls_back_to_exact(self):
        self.write("a.py", "needle\n")
        bogus = "CREATE VIRTUAL TABLE content USING fts5(body, tokenize='missing')"

        with patch.object(search_index, "_FTS_DDL", bogus):
            result = await self.call("needle", search_mode="fts5")

        self.assertTrue(result.startswith("[fts5 降级]"))
        self.assertIn("trigram", result)
        self.assertIn("a.py:1", result)

    async def test_locked_index_falls_back_with_the_actual_reason(self):
        self.write("a.py", "needle\n")

        with patch.object(
            search_index.SearchIndex,
            "sync",
            side_effect=search_index.FtsIndexError("database is locked"),
        ):
            result = await self.call("needle", search_mode="fts5")

        self.assertTrue(result.startswith("[fts5 降级]"))
        self.assertIn("database is locked", result)
        self.assertIn("a.py:1", result)

    async def test_sqlite_work_runs_off_the_event_loop(self):
        self.write("a.py", "needle\n")
        threads = []
        original = search_index.SearchIndex.sync

        def spy(self, *args, **kwargs):
            threads.append(threading.get_ident())
            return original(self, *args, **kwargs)

        with patch.object(search_index.SearchIndex, "sync", new=spy):
            await self.call("needle", search_mode="fts5")

        self.assertTrue(threads)
        self.assertNotEqual(threads[0], threading.get_ident())

    async def test_fts5_does_not_reindex_unchanged_files(self):
        self.write("a.py", "needle\n")
        await self.call("needle", search_mode="fts5")
        index = main._search_index(self.plugin)
        entries, _skipped, incomplete = main._collect_index_entries(
            str(self.root), main.time.monotonic() + 10
        )

        stats = index.sync("test", str(self.root), entries, complete=not incomplete)

        self.assertEqual((stats.added, stats.updated, stats.removed), (0, 0, 0))
        self.assertEqual(stats.total, 1)

    async def test_fts5_does_not_reuse_old_root_after_incomplete_walk(self):
        root_a = self.root / "root-a"
        root_b = self.root / "root-b"
        root_a.mkdir()
        root_b.mkdir()
        (root_a / "secret.txt").write_text("secret-token\n", encoding="utf-8")
        (root_b / "public.txt").write_text("public-token\n", encoding="utf-8")
        self.plugin.config["sources"][0]["path"] = str(root_a)
        await self.call("secret-token", mode="files", search_mode="fts5")

        self.plugin.config["sources"][0]["path"] = str(root_b)
        original_cap = main.MAX_SCAN_FILES
        main.MAX_SCAN_FILES = 0
        try:
            result = await self.call("secret-token", mode="files", search_mode="fts5")
        finally:
            main.MAX_SCAN_FILES = original_cap

        self.assertNotIn("secret.txt", result)

    async def test_fts5_skips_symlinks_outside_source_root(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("outside-secret\n", encoding="utf-8")
        link = self.root / "link.txt"
        try:
            link.symlink_to(outside / "secret.txt")
        except (NotImplementedError, OSError):
            self.skipTest("当前平台或权限不支持创建符号链接")

        result = await self.call("outside-secret", mode="files", search_mode="fts5")

        self.assertNotIn("link.txt", result)

        downgraded = await self.call(
            "outside-secret|missing", mode="files", search_mode="fts5"
        )
        self.assertNotIn("link.txt", downgraded)


class SearchIndexTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.root_a = base / "a"
        self.root_b = base / "b"
        self.root_a.mkdir()
        self.root_b.mkdir()
        self.index = search_index.SearchIndex(str(base / "search.sqlite3"))

    def entry(self, root: Path, rel: str) -> search_index.FileEntry:
        path = root / rel
        info = path.stat()
        return search_index.FileEntry(rel, str(path), info.st_size, info.st_mtime_ns)

    def write(self, root: Path, rel: str, text: str) -> Path:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_sync_adds_updates_and_removes(self):
        self.write(self.root_a, "keep.py", "needle\n")
        self.write(self.root_a, "gone.py", "needle\n")
        first = self.index.sync(
            "a",
            str(self.root_a),
            [self.entry(self.root_a, "keep.py"), self.entry(self.root_a, "gone.py")],
        )
        self.assertEqual((first.added, first.total), (2, 2))

        self.write(self.root_a, "keep.py", "needle needle\n")
        (self.root_a / "gone.py").unlink()
        second = self.index.sync(
            "a", str(self.root_a), [self.entry(self.root_a, "keep.py")]
        )

        self.assertEqual((second.added, second.updated, second.removed), (0, 1, 1))
        self.assertEqual(
            [rel for rel, _rank in self.index.search("a", "needle", 10)], ["keep.py"]
        )

    def test_two_instances_reuse_the_same_schema(self):
        self.write(self.root_a, "a.py", "alpha\n")
        entry = self.entry(self.root_a, "a.py")
        first = search_index.SearchIndex(self.index.db_path)
        second = search_index.SearchIndex(self.index.db_path)

        first.sync("a", str(self.root_a), [entry])
        second.sync("a", str(self.root_a), [entry])

        self.assertEqual([rel for rel, _ in second.search("a", "alpha", 5)], ["a.py"])

    def test_sources_are_isolated(self):
        self.write(self.root_a, "a.py", "alpha\n")
        self.write(self.root_b, "b.py", "beta\n")
        self.index.sync("a", str(self.root_a), [self.entry(self.root_a, "a.py")])
        self.index.sync("b", str(self.root_b), [self.entry(self.root_b, "b.py")])

        self.assertEqual(
            [rel for rel, _ in self.index.search("a", "alpha", 5)], ["a.py"]
        )
        self.assertEqual(
            [rel for rel, _ in self.index.search("b", "beta", 5)], ["b.py"]
        )

        self.index.sync("a", str(self.root_a), [], complete=True)

        self.assertEqual(self.index.search("a", "alpha", 5), [])
        self.assertEqual(
            [rel for rel, _ in self.index.search("b", "beta", 5)], ["b.py"]
        )

    def test_incomplete_sync_keeps_files_that_were_not_listed(self):
        self.write(self.root_a, "a.py", "alpha\n")
        self.index.sync("a", str(self.root_a), [self.entry(self.root_a, "a.py")])

        stats = self.index.sync("a", str(self.root_a), [], complete=False)

        self.assertEqual(stats.removed, 0)
        self.assertEqual(
            [rel for rel, _ in self.index.search("a", "alpha", 5)], ["a.py"]
        )

    def test_file_changing_after_the_walk_is_not_indexed(self):
        path = self.write(self.root_a, "a.py", "needle\n")
        stale = search_index.FileEntry("a.py", str(path), 1, 1)

        stats = self.index.sync("a", str(self.root_a), [stale])

        self.assertEqual(stats.unstable, ("a.py",))
        self.assertEqual(self.index.search("a", "needle", 5), [])

    def test_locked_database_is_reported_instead_of_hanging(self):
        self.write(self.root_a, "a.py", "needle\n")
        entry = self.entry(self.root_a, "a.py")
        locked = search_index.SearchIndex(str(self.index.db_path), timeout=0.05)
        locked.sync("a", str(self.root_a), [entry])

        holder = sqlite3.connect(self.index.db_path, isolation_level=None)
        holder.execute("BEGIN EXCLUSIVE")
        try:
            with self.assertRaises(search_index.FtsIndexError):
                locked.sync("a", str(self.root_a), [entry])
        finally:
            holder.execute("ROLLBACK")
            holder.close()

    def test_missing_trigram_tokenizer_raises_unavailable(self):
        bogus = "CREATE VIRTUAL TABLE content USING fts5(body, tokenize='missing')"
        with patch.object(search_index, "_FTS_DDL", bogus):
            with self.assertRaises(search_index.FtsUnavailable):
                self.index.sync("a", str(self.root_a), [])

    def test_prune_sources_removes_unconfigured_source_content(self):
        self.write(self.root_a, "a.py", "alpha\n")
        self.write(self.root_b, "b.py", "beta\n")
        self.index.sync("a", str(self.root_a), [self.entry(self.root_a, "a.py")])
        self.index.sync("b", str(self.root_b), [self.entry(self.root_b, "b.py")])

        self.index.prune_sources({"a"})

        self.assertEqual(
            [rel for rel, _rank in self.index.search("a", "alpha", 5)], ["a.py"]
        )
        self.assertEqual(self.index.search("b", "beta", 5), [])


if __name__ == "__main__":
    unittest.main()

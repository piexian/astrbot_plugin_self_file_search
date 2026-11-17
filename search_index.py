"""SQLite FTS5 trigram 索引：按源维护文件元数据，用内置 bm25() 返回候选文件。"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import NamedTuple

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000
_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS content USING fts5(body, tokenize='trigram')"
)


class FtsUnavailable(RuntimeError):
    """运行时不支持 FTS5 或 trigram 分词器。"""


class FtsIndexError(RuntimeError):
    """索引打开、写入或查询失败（锁、I/O、损坏）。"""


class FileEntry(NamedTuple):
    """扫描阶段得到的稳定文件元数据。"""

    rel: str
    abs_path: str
    size: int
    mtime_ns: int


class SyncStats(NamedTuple):
    """一次增量同步的结果。"""

    added: int = 0
    updated: int = 0
    removed: int = 0
    unstable: tuple[str, ...] = ()
    total: int = 0
    truncated: bool = False


def fts_phrase(pattern: str) -> str:
    """把字面量包成 FTS5 短语并转义双引号，用户输入不会变成 MATCH 运算符。"""
    if "\x00" in pattern:
        raise FtsIndexError("pattern 含 NUL 字符，无法作为 FTS5 查询")
    return '"' + pattern.replace('"', '""') + '"'


def _read_stable(entry: FileEntry) -> str | None:
    """读取文件正文；读取期间 size/mtime 变化则返回 None，避免索引半文件。"""
    try:
        before = os.stat(entry.abs_path)
        if before.st_size != entry.size or before.st_mtime_ns != entry.mtime_ns:
            return None
        with open(entry.abs_path, "rb") as f:
            data = f.read()
        after = os.stat(entry.abs_path)
    except OSError:
        return None
    if after.st_size != len(data) or after.st_mtime_ns != before.st_mtime_ns:
        return None
    return data.decode("utf-8", errors="replace")


class SearchIndex:
    """索引文件 search.sqlite3；每次操作在自己的线程里新建并关闭连接。"""

    def __init__(self, db_path: str, timeout: float = BUSY_TIMEOUT_MS / 1000) -> None:
        self.db_path = db_path
        self._timeout = timeout
        self._lock = threading.Lock()
        self._ready = False

    def sync(
        self,
        source: str,
        root: str,
        entries: list[FileEntry],
        *,
        complete: bool = True,
        deadline: float | None = None,
    ) -> SyncStats:
        """按源增量更新新增、变化、删除的文件，整批写入在一个事务里。"""
        conn = self._open()
        try:
            self._begin(conn)
            try:
                stats = self._sync_rows(conn, source, root, entries, complete, deadline)
                conn.execute("COMMIT")
            except BaseException as exc:
                self._rollback(conn)
                if isinstance(exc, sqlite3.Error):
                    raise FtsIndexError(f"索引写入失败：{exc}") from exc
                raise
            return stats
        finally:
            conn.close()

    def search(self, source: str, pattern: str, limit: int) -> list[tuple[str, float]]:
        """返回 [(相对路径, bm25 分数)]，按相关性升序（越负越相关）。"""
        conn = self._open()
        try:
            try:
                rows = conn.execute(
                    "SELECT f.rel, bm25(content) FROM content "
                    "JOIN files f ON f.id = content.rowid "
                    "WHERE content MATCH ? AND f.source = ? "
                    "ORDER BY bm25(content) ASC, f.rel ASC LIMIT ?",
                    (fts_phrase(pattern), source, max(1, int(limit))),
                ).fetchall()
            except sqlite3.Error as exc:
                self._ready = False
                raise FtsIndexError(f"索引查询失败：{exc}") from exc
            return [(str(rel), float(rank)) for rel, rank in rows]
        finally:
            conn.close()

    def _open(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(
                self.db_path, timeout=self._timeout, isolation_level=None
            )
        except sqlite3.Error as exc:
            raise FtsIndexError(f"打开索引失败：{exc}") from exc
        conn.execute(f"PRAGMA busy_timeout={int(self._timeout * 1000)}")
        try:
            self._ensure_schema(conn)
        except (FtsUnavailable, FtsIndexError):
            conn.close()
            raise
        except sqlite3.Error as exc:
            conn.close()
            self._ready = False
            raise FtsIndexError(f"索引不可用：{exc}") from exc
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            try:
                conn.execute("BEGIN IMMEDIATE")
                # 事务内重读版本：避免与另一个实例建表竞争时误删新表
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version != SCHEMA_VERSION:
                    conn.execute("DROP TABLE IF EXISTS content")
                    conn.execute("DROP TABLE IF EXISTS files")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS files ("
                    "id INTEGER PRIMARY KEY, source TEXT NOT NULL, root TEXT NOT NULL, "
                    "rel TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL)"
                )
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS files_source_rel "
                    "ON files(source, rel)"
                )
                conn.execute(_FTS_DDL)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                self._rollback(conn)
                message = str(exc)
                lowered = message.lower()
                if "fts5" in lowered or "trigram" in lowered or "tokenizer" in lowered:
                    raise FtsUnavailable(
                        f"运行时不支持 FTS5 trigram：{message}"
                    ) from exc
                raise FtsIndexError(f"初始化索引失败：{message}") from exc
            self._ready = True

    def _begin(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise FtsIndexError(f"等待索引写锁超时：{exc}") from exc

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def prune_sources(self, sources: set[str]) -> None:
        """Remove index rows for sources that are no longer configured."""
        conn = self._open()
        try:
            self._begin(conn)
            if sources:
                placeholders = ",".join("?" for _ in sources)
                params = tuple(sorted(sources))
                conn.execute(
                    "DELETE FROM content WHERE rowid IN "
                    f"(SELECT id FROM files WHERE source NOT IN ({placeholders}))",
                    params,
                )
                conn.execute(
                    f"DELETE FROM files WHERE source NOT IN ({placeholders})",
                    params,
                )
            else:
                conn.execute("DELETE FROM content")
                conn.execute("DELETE FROM files")
            conn.execute("COMMIT")
        except BaseException as exc:
            self._rollback(conn)
            if isinstance(exc, sqlite3.Error):
                raise FtsIndexError(f"索引清理失败：{exc}") from exc
            raise
        finally:
            conn.close()

    def _sync_rows(
        self,
        conn: sqlite3.Connection,
        source: str,
        root: str,
        entries: list[FileEntry],
        complete: bool,
        deadline: float | None,
    ) -> SyncStats:
        root = os.path.realpath(root)
        current = {entry.rel: entry for entry in entries}
        stored_rows = list(
            conn.execute(
                "SELECT id, root, rel, size, mtime_ns FROM files WHERE source=?",
                (source,),
            )
        )
        old_roots = {str(row[1]) for row in stored_rows}
        if old_roots and old_roots != {root}:
            conn.execute(
                "DELETE FROM content WHERE rowid IN "
                "(SELECT id FROM files WHERE source=? AND root<>?)",
                (source, root),
            )
            conn.execute(
                "DELETE FROM files WHERE source=? AND root<>?",
                (source, root),
            )
            stored_rows = [row for row in stored_rows if row[1] == root]
        stored = {
            rel: (row_id, size, mtime_ns)
            for row_id, _root, rel, size, mtime_ns in stored_rows
        }
        added = updated = removed = 0
        unstable: list[str] = []
        truncated = False
        for rel, entry in current.items():
            if deadline is not None and time.monotonic() > deadline:
                truncated = True
                break
            old = stored.get(rel)
            if old and old[1] == entry.size and old[2] == entry.mtime_ns:
                continue
            body = _read_stable(entry)
            if body is None:
                unstable.append(rel)
                continue
            if old:
                conn.execute(
                    "UPDATE files SET root=?, size=?, mtime_ns=? WHERE id=?",
                    (root, entry.size, entry.mtime_ns, old[0]),
                )
                conn.execute("DELETE FROM content WHERE rowid=?", (old[0],))
                conn.execute(
                    "INSERT INTO content(rowid, body) VALUES (?, ?)", (old[0], body)
                )
                updated += 1
            else:
                cursor = conn.execute(
                    "INSERT INTO files(source, root, rel, size, mtime_ns) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (source, root, rel, entry.size, entry.mtime_ns),
                )
                conn.execute(
                    "INSERT INTO content(rowid, body) VALUES (?, ?)",
                    (cursor.lastrowid, body),
                )
                added += 1
        # 遍历或索引被截断时，未出现的文件不能当作已删除
        if complete and not truncated:
            for rel in stored.keys() - current.keys():
                row_id = stored[rel][0]
                conn.execute("DELETE FROM content WHERE rowid=?", (row_id,))
                conn.execute("DELETE FROM files WHERE id=?", (row_id,))
                removed += 1
        total = conn.execute(
            "SELECT COUNT(*) FROM files WHERE source=?", (source,)
        ).fetchone()[0]
        return SyncStats(
            added, updated, removed, tuple(unstable), int(total), truncated
        )

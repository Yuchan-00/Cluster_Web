"""SQLite access for the master.

One connection, one writer. Every database call runs in a worker thread so the event loop
never blocks on the SD card, and an asyncio lock serialises the calls (SQLite in this
process is effectively single-threaded anyway, and the audit chain needs strict ordering).
The CLI uses the same `SyncDB` directly.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib import resources
from typing import Any, TypeVar

T = TypeVar("T")


def now_ms() -> int:
    return int(time.time() * 1000)


class SyncDB:
    def __init__(self, path: str, *, read_only: bool = False) -> None:
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", mode=0o750, exist_ok=True)
        uri = f"file:{path}?mode=ro" if read_only else path
        self.conn = sqlite3.connect(
            uri, uri=read_only, check_same_thread=False, isolation_level=None, timeout=10.0
        )
        self.conn.row_factory = sqlite3.Row
        if not read_only:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            if path != ":memory:":
                os.chmod(path, 0o600)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=10000")

    def close(self) -> None:
        self.conn.close()

    # -- schema ----------------------------------------------------------------------------

    def migrate(self) -> int:
        """Apply migrations/NNNN_*.sql in order; return the schema version.

        Everything (reading the applied versions, the schema statements, the version rows)
        happens in one BEGIN IMMEDIATE transaction, so two processes starting on a fresh
        database serialise instead of one failing on a duplicate version row.
        """
        files = sorted(
            f
            for f in resources.files("cluster_master.migrations").iterdir()
            if f.name.endswith(".sql")
        )
        with self.transaction(immediate=True):
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, "
                "applied_ms INTEGER NOT NULL)"
            )
            applied = {r[0] for r in self.conn.execute("SELECT version FROM schema_version")}
            for entry in files:
                version = int(entry.name.split("_", 1)[0])
                if version in applied:
                    continue
                for statement in split_statements(entry.read_text(encoding="utf-8")):
                    self.conn.execute(statement)
                self.conn.execute(
                    "INSERT INTO schema_version (version, applied_ms) VALUES (?, ?)",
                    (version, now_ms()),
                )
            row = self.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return int(row[0] or 0)

    # -- transactions ------------------------------------------------------------------------

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """BEGIN [IMMEDIATE] ... COMMIT/ROLLBACK. IMMEDIATE takes the write lock up front, which is
        what the audit chain relies on (security.md 13.1)."""
        self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.conn
        except BaseException:
            self._rollback()
            raise
        try:
            self.conn.execute("COMMIT")
        except sqlite3.Error:
            # a failed COMMIT (disk full, busy) must not leave the connection in a transaction
            self._rollback()
            raise

    def _rollback(self) -> None:
        try:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - nothing more can be done here
            pass

    # -- helpers -----------------------------------------------------------------------------

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def backup_to(self, dest_path: str) -> None:
        dest = sqlite3.connect(dest_path)
        try:
            self.conn.backup(dest)
        finally:
            dest.close()


class Database:
    """Async facade: `await db.run(fn)` runs fn(SyncDB) in a thread, one call at a time."""

    def __init__(self, path: str) -> None:
        self.sync = SyncDB(path)
        self._lock = asyncio.Lock()

    async def run(self, fn: Callable[[SyncDB], T]) -> T:
        async with self._lock:
            return await asyncio.to_thread(fn, self.sync)

    async def migrate(self) -> int:
        return await self.run(lambda db: db.migrate())

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self.sync.close)


def split_statements(script: str) -> list[str]:
    """Split an SQL script into complete statements (triggers contain ';' inside BEGIN..END)."""
    statements, buf = [], ""
    for line in script.splitlines():
        stripped = line.strip()
        if not buf and (not stripped or stripped.startswith("--")):
            continue
        buf += line + "\n"
        if sqlite3.complete_statement(buf):
            statements.append(buf.strip())
            buf = ""
    if buf.strip():
        raise ValueError("migration ends with an incomplete statement")
    return statements


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None

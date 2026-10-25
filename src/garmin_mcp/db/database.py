"""The DuckDB mirror: one file, owned by this process.

DuckDB allows a single writing process, so the sync worker and the MCP tools share
this instance. Writes go through one connection behind a lock (a DuckDB connection
must not run two statements at once); the read-only ``garmin_query`` tool gets its own
cursor, which MVCC lets run alongside the writer.

File access is locked down once the schema is in place: nothing but the snapshot
directory is reachable from SQL, so a query cannot read the token file.

A fatal DuckDB error ("Failed to rollback transaction. Cannot continue operation", an
internal assertion) invalidates the database instance: every later statement fails
until the process restarts. Every statement, on the writer connection or a cursor,
reports such an error to ``on_fatal`` before re-raising it; the server installs a
handler that exits the process non-zero so the container restarts.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from .schema import MIGRATIONS

log = logging.getLogger("garmin_mcp.db")

Row = dict[str, Any]

# Exit status when DuckDB can no longer be used (EX_SOFTWARE); Docker restarts the container.
FATAL_EXIT_CODE = 70


def is_fatal(err: BaseException) -> bool:
    """Whether a DuckDB error leaves the database instance unusable."""
    return isinstance(err, (duckdb.FatalException, duckdb.InternalException))


def exit_on_fatal(err: BaseException) -> None:
    """The server's handler: log and end the process so the supervisor restarts it.

    os._exit, not SystemExit: the error may surface on the sync worker, the snapshot
    thread or a tool call's worker thread, none of which can stop the process by raising.
    """
    log.critical("DuckDB fatal error, the mirror cannot be used until the process restarts: %s. Exiting with status %d.", err, FATAL_EXIT_CODE)
    logging.shutdown()
    sys.stderr.flush()
    os._exit(FATAL_EXIT_CODE)


@dataclass
class DatabaseOptions:
    path: str
    snapshot_dir: str
    memory_limit: str | None = None
    threads: int | None = None


class Tx:
    """Statements on the writer connection, inside the write lock."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, on_error: Callable[[BaseException], None] | None = None) -> None:
        self.conn = conn
        self.on_error = on_error

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> duckdb.DuckDBPyConnection:
        try:
            return self.conn.execute(sql, list(params) if params is not None else None)
        except duckdb.Error as err:
            if self.on_error is not None:
                self.on_error(err)
            raise

    def run(self, sql: str, params: Sequence[Any] | None = None) -> None:
        self.execute(sql, params)

    def all(self, sql: str, params: Sequence[Any] | None = None) -> list[Row]:
        cur = self.execute(sql, params)
        if cur.description is None:
            return []
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]

    def one(self, sql: str, params: Sequence[Any] | None = None) -> Row | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        row = self.execute(sql, params).fetchone()
        return row[0] if row else None


class Database:
    def __init__(self, conn: duckdb.DuckDBPyConnection, options: DatabaseOptions) -> None:
        self._conn = conn
        self._lock = threading.RLock()
        self.options = options
        # Called with the error before it propagates, when a statement finds the database unusable.
        self.on_fatal: Callable[[BaseException], None] | None = None

    def report(self, err: BaseException) -> None:
        """Report an error raised by a statement outside Tx (a read-only cursor); fatal ones reach on_fatal."""
        if is_fatal(err) and self.on_fatal is not None:
            self.on_fatal(err)

    @classmethod
    def open(cls, options: DatabaseOptions) -> Database:
        path = options.path
        if path != ":memory:":
            path = str(Path(path).resolve())
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        snapshot_dir = str(Path(options.snapshot_dir).resolve())
        Path(snapshot_dir).mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(path)
        conn.execute("SET GLOBAL TimeZone = 'UTC'")
        if options.memory_limit:
            conn.execute(f"SET memory_limit = '{options.memory_limit}'")
        if options.threads:
            conn.execute(f"SET threads = {int(options.threads)}")
        if path != ":memory:":
            tmp = Path(path).parent / "duckdb-tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            conn.execute(f"SET temp_directory = '{tmp}'")
        db = cls(conn, DatabaseOptions(path, snapshot_dir, options.memory_limit, options.threads))
        db._migrate()
        # From here on SQL can touch only the snapshot directory, and cannot undo that.
        conn.execute(f"SET allowed_directories = ['{snapshot_dir}{os.sep}']")
        conn.execute("SET enable_external_access = false")
        conn.execute("SET lock_configuration = true")
        return db

    def _migrate(self) -> None:
        c = self._conn
        c.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration (version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())"
        )
        applied = {r[0] for r in c.execute("SELECT version FROM schema_migration").fetchall()}
        for index, statements in enumerate(MIGRATIONS):
            version = index + 1
            if version in applied:
                continue
            c.execute("BEGIN TRANSACTION")
            try:
                for sql in statements:
                    c.execute(sql)
                c.execute("INSERT INTO schema_migration (version) VALUES (?)", [version])
                c.execute("COMMIT")
            except Exception as err:
                c.execute("ROLLBACK")
                raise RuntimeError(f"Migration {version} failed: {err}") from err

    @contextmanager
    def write(self) -> Iterator[Tx]:
        """Statements on the writer connection, serialized with every other write."""
        with self._lock:
            yield Tx(self._conn, self.report)

    @contextmanager
    def transaction(self) -> Iterator[Tx]:
        with self._lock:
            tx = Tx(self._conn, self.report)
            tx.run("BEGIN TRANSACTION")
            try:
                yield tx
            except BaseException:
                try:
                    tx.run("ROLLBACK")
                except Exception:
                    pass
                raise
            else:
                tx.run("COMMIT")

    def all(self, sql: str, params: Sequence[Any] | None = None) -> list[Row]:
        with self.write() as tx:
            return tx.all(sql, params)

    def one(self, sql: str, params: Sequence[Any] | None = None) -> Row | None:
        with self.write() as tx:
            return tx.one(sql, params)

    def scalar(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        with self.write() as tx:
            return tx.scalar(sql, params)

    def run(self, sql: str, params: Sequence[Any] | None = None) -> None:
        with self.write() as tx:
            tx.run(sql, params)

    def connect(self) -> duckdb.DuckDBPyConnection:
        """A separate cursor on the same database, for the read-only query tool."""
        with self._lock:
            return self._conn.cursor()

    def catalog(self) -> str:
        """The catalog name of the mirror (the file's basename), for COPY FROM DATABASE."""
        return str(self.scalar("SELECT current_database()"))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

"""A fatal DuckDB error (the instance invalidated itself) must be noticed wherever a
statement runs and end the process non-zero, so the container restarts instead of
serving errors forever.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import duckdb
import pytest

from garmin_mcp.db import database
from garmin_mcp.db.database import FATAL_EXIT_CODE, Database, DatabaseOptions, exit_on_fatal, is_fatal
from garmin_mcp.tools.base import ToolFailure

FATAL = duckdb.FatalException("FATAL Error: Failed to rollback transaction. Cannot continue operation")


class BrokenConnection:
    """A connection whose every statement fails the way an invalidated DuckDB instance does."""

    def __init__(self, err: Exception) -> None:
        self.err = err
        self.description = None

    def execute(self, sql, params=None):
        raise self.err

    def cursor(self):
        return self

    def interrupt(self) -> None:
        pass

    def close(self) -> None:
        pass


def broken_db(err: Exception = FATAL) -> Database:
    tmp = Path(tempfile.mkdtemp(prefix="garmin-mcp-fatal-"))
    return Database(BrokenConnection(err), DatabaseOptions(str(tmp / "garmin.duckdb"), str(tmp / "snapshot")))  # type: ignore[arg-type]


def test_fatal_and_internal_errors_are_fatal_others_are_not():
    assert is_fatal(FATAL)
    assert is_fatal(duckdb.InternalException("INTERNAL Error: assertion"))
    assert not is_fatal(duckdb.BinderException("no such column"))
    assert not is_fatal(duckdb.OutOfMemoryException("Out of Memory Error"))
    assert not is_fatal(RuntimeError("x"))


def test_writer_statements_report_fatal_errors_before_raising():
    db = broken_db()
    seen: list[BaseException] = []
    db.on_fatal = seen.append
    with pytest.raises(duckdb.FatalException):
        db.run("SELECT 1")
    with pytest.raises(duckdb.FatalException):
        db.all("SELECT 1")
    with pytest.raises(duckdb.FatalException):
        with db.transaction() as tx:
            tx.scalar("SELECT 1")
    assert len(seen) == 3 and all(e is FATAL for e in seen)


def test_ordinary_errors_do_not_trigger_the_handler():
    db = broken_db(duckdb.BinderException("Binder Error: no such column"))
    seen: list[BaseException] = []
    db.on_fatal = seen.append
    with pytest.raises(duckdb.BinderException):
        db.run("SELECT nope")
    assert seen == []


def test_read_only_query_tool_reports_fatal_errors():
    from garmin_mcp.tools.mirror_tools import ReadOnlyQuery

    db = broken_db()
    seen: list[BaseException] = []
    db.on_fatal = seen.append

    class M:
        pass

    m = M()
    m.db = db  # type: ignore[attr-defined]
    with pytest.raises(ToolFailure):
        ReadOnlyQuery(m).run("SELECT 1", 10)  # type: ignore[arg-type]
    assert seen == [FATAL]


def test_sync_worker_step_reports_fatal_errors(monkeypatch):
    """The worker loop swallows ordinary errors to keep going; a fatal one must still reach the handler."""
    from garmin_mcp.garmin.auth import PendingTransport
    from garmin_mcp.garmin.client import GarminClient
    from garmin_mcp.sync.engine import SyncEngine

    db = broken_db()
    seen: list[BaseException] = []
    db.on_fatal = seen.append
    engine = SyncEngine(db, GarminClient(PendingTransport()), "/tmp", sleep=lambda s: None)
    with pytest.raises(duckdb.FatalException):
        engine.step()
    assert seen == [FATAL]


def test_server_handler_exits_the_process_non_zero(monkeypatch, caplog):
    exits: list[int] = []
    monkeypatch.setattr(database.os, "_exit", exits.append)
    monkeypatch.setattr(database.logging, "shutdown", lambda: None)
    with caplog.at_level(logging.CRITICAL, logger="garmin_mcp.db"):
        exit_on_fatal(FATAL)
    assert exits == [FATAL_EXIT_CODE] and FATAL_EXIT_CODE != 0
    assert "restarts" in caplog.text and "Cannot continue operation" in caplog.text


def test_runtime_installs_the_exit_handler(tmp_path):
    from garmin_mcp.cli import open_runtime

    rt = open_runtime({"GARMIN_DB_PATH": str(tmp_path / "garmin.duckdb"), "GARMIN_SYNC": "off"}, start=False)
    try:
        assert rt.db.on_fatal is exit_on_fatal
    finally:
        rt.close()

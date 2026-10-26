"""Security regressions on a real synthetic DuckDB; never open a live mirror."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from garmin_mcp.db.database import Database, DatabaseOptions
from garmin_mcp.tools.base import ToolFailure
from garmin_mcp.tools.exports import write_export
from garmin_mcp.tools.mirror_tools import ReadOnlyQuery


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    root = tmp_path_factory.mktemp("query-export")
    db = Database.open(DatabaseOptions(str(root / "mirror.duckdb"), str(root / "snapshot")))
    db.run("CREATE TABLE sentinel AS SELECT 42 AS value")
    db.run("CREATE SEQUENCE sentinel_sequence START 10")
    mirror = SimpleNamespace(db=db, fetch=SimpleNamespace(data_dir=root))
    yield mirror
    db.close()


@pytest.mark.parametrize("sql", [
    "EXPLAIN ANALYZE DELETE FROM sentinel",
    "EXPLAIN ANALYZE UPDATE sentinel SET value = 0",
    "EXPLAIN ANALYZE INSERT INTO sentinel VALUES (0)",
    "EXPLAIN ANALYZE CREATE TABLE bad AS SELECT 0",
    "EXPLAIN ANALYZE DROP TABLE sentinel",
    "WITH c AS (SELECT 1) DELETE FROM sentinel",
    "SELECT nextval('sentinel_sequence')",
    "INSERT INTO sentinel VALUES (0)",
    "DROP TABLE sentinel",
    "CREATE TABLE bad AS SELECT 0",
    "SELECT 1; DELETE FROM sentinel",
    "COMMIT; DELETE FROM sentinel",
])
def test_engine_guard_prevents_mutations_and_cleans_up(synthetic, sql):
    query = ReadOnlyQuery(synthetic)
    with pytest.raises(ToolFailure):
        query.run(sql, 10)
    assert query.run("SELECT * FROM sentinel", 10)["rows"] == [{"value": 42}]
    assert synthetic.db.scalar("SELECT count(*) FROM duckdb_tables() WHERE table_name = 'bad'") == 0
    assert synthetic.db.scalar("SELECT last_value FROM duckdb_sequences() WHERE sequence_name = 'sentinel_sequence'") is None
    # A read-only transaction must never leak onto the writer.
    synthetic.db.run("UPDATE sentinel SET value = 42")


def test_read_only_begin_failure_never_falls_back_to_execution(tmp_path):
    import duckdb

    statements = []
    class Unsupported:
        def cursor(self):
            return self
        def execute(self, sql):
            statements.append(sql)
            raise duckdb.ParserException("READ ONLY unsupported")
        def close(self):
            statements.append("CLOSE")
    db = Database(Unsupported(), DatabaseOptions(":memory:", str(tmp_path)))
    with pytest.raises(ToolFailure, match="read-only transaction required"):
        ReadOnlyQuery(SimpleNamespace(db=db)).run("SELECT 42", 1)
    assert statements == ["BEGIN TRANSACTION READ ONLY", "CLOSE"]


def test_explain_engine_enforcement_not_only_parser(synthetic, monkeypatch):
    query = ReadOnlyQuery(synthetic)
    monkeypatch.setattr(query, "check", lambda sql: None)
    with pytest.raises(ToolFailure, match="read-only mode"):
        query.run("EXPLAIN ANALYZE DELETE FROM sentinel", 1)
    assert synthetic.db.scalar("SELECT value FROM sentinel") == 42


def test_query_cannot_access_protected_files(synthetic, tmp_path):
    secret = tmp_path / "synthetic-token.txt"
    secret.write_text("not-a-real-token")
    query = ReadOnlyQuery(synthetic)
    for sql in [f"SELECT * FROM read_text('{secret}')", f"SELECT * FROM read_blob('{secret}')", "SELECT * FROM read_csv('https://example.invalid/data.csv')"]:
        with pytest.raises(ToolFailure, match="disabled"):
            query.run(sql, 10)
    output = Path(synthetic.db.options.snapshot_dir) / "unexpected.csv"
    with pytest.raises(ToolFailure):
        query.run(f"EXPLAIN ANALYZE COPY sentinel TO '{output}'", 10)
    assert not output.exists()
    for prefix in ["EXPLAIN /* comment */ ANALYZE", "EXPLAIN (ANALYZE, FORMAT JSON)"]:
        with pytest.raises(ToolFailure):
            query.run(f"{prefix} COPY sentinel TO '{output}'", 1)
        assert query.run(f"{prefix} SELECT 1", 1)["row_count"] == 1


def test_query_timeout_rollback_and_concurrency(synthetic, monkeypatch):
    from garmin_mcp.tools import mirror_tools

    monkeypatch.setattr(mirror_tools, "QUERY_TIMEOUT_S", 0.02)
    query = ReadOnlyQuery(synthetic)
    with pytest.raises(ToolFailure, match="interrupted"):
        query.run("SELECT sum(a.i * b.i) FROM range(100000000) a(i), range(100000000) b(i)", 1)
    monkeypatch.setattr(mirror_tools, "QUERY_TIMEOUT_S", 30)
    def read(i):
        # Different handlers still serialize at the shared database boundary.
        return ReadOnlyQuery(synthetic).run(f"SELECT {i} AS n", 1)["rows"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(read, range(12))) == [[{"n": i}] for i in range(12)]
    assert query.run("SELECT * FROM sentinel", 1)["rows"] == [{"value": 42}]


def test_export_protected_locations_symlinks_and_safe_overwrite(synthetic, tmp_path, monkeypatch):
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    config = tmp_path / "config.gpx"
    config.write_bytes(b"configuration")
    monkeypatch.setenv("GARMINTOKENS", str(tokens))
    for path in [Path(synthetic.db.options.path), Path(synthetic.db.options.path + ".wal"), synthetic.fetch.data_dir / "fit" / "activity" / "1.fit", Path(synthetic.db.options.snapshot_dir) / "1.fit", tokens / "1.gpx", config]:
        with pytest.raises(ToolFailure, match="protected"):
            write_export(synthetic, str(path), b"bad", str(config))
    assert config.read_bytes() == b"configuration"
    # Runtime callers can supply a token store independently of os.environ.
    explicit = tmp_path / "explicit-token-store"
    synthetic.tokenstore = str(explicit)
    try:
        with pytest.raises(ToolFailure, match="protected"):
            write_export(synthetic, str(explicit / "1.fit"), b"bad", str(config))
    finally:
        synthetic.tokenstore = None
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "linked.gpx").symlink_to(config)
    (tmp_path / "linked-dir").symlink_to(exports, target_is_directory=True)
    for target in [exports / "linked.gpx", tmp_path / "linked-dir" / "1.fit"]:
        with pytest.raises(ToolFailure):
            write_export(synthetic, str(target), b"bad", str(config))
    # Replacing a hardlink is safe: do not truncate its protected inode.
    hardlink = exports / "hardlink.gpx"
    hardlink.hardlink_to(config)
    write_export(synthetic, str(hardlink), b"new", str(config))
    assert config.read_bytes() == b"configuration"
    assert hardlink.read_bytes() == b"new"
    target = exports / "nested" / "1.fit"
    assert write_export(synthetic, str(target), b"first", str(config)) == str(target)
    write_export(synthetic, str(target), b"second", str(config))
    assert target.read_bytes() == b"second"
    assert not list(target.parent.glob(".garmin-export-*"))

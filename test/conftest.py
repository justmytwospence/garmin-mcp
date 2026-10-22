from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from garmin_mcp.db.database import Database, DatabaseOptions  # noqa: E402
from garmin_mcp.db.raw import land  # noqa: E402


@pytest.fixture(scope="session")
def fixtures() -> list[dict]:
    return json.loads((Path(__file__).parent / "fixtures" / "payloads.json").read_text())


def open_db(tmp: Path) -> Database:
    return Database.open(DatabaseOptions(str(tmp / "garmin.duckdb"), str(tmp / "snapshot")))


@pytest.fixture
def db():
    tmp = Path(tempfile.mkdtemp(prefix="garmin-mcp-test-"))
    d = open_db(tmp)
    try:
        yield d
    finally:
        d.close()


@pytest.fixture(scope="module")
def landed_db(fixtures):
    """A mirror with every fixture landed (module scoped: read-mostly tests share it)."""
    tmp = Path(tempfile.mkdtemp(prefix="garmin-mcp-test-"))
    d = open_db(tmp)
    with d.transaction() as tx:
        for f in fixtures:
            land(tx, f["type"], f["key"], json.dumps(f["body"]))
    yield d
    d.close()

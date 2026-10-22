"""Wide projections stay within the memory limit the image runs with.

DuckDB parses JSON afresh for every extraction expression; before projections parsed
each body once, a page of 100 activity summaries (200 columns) exhausted 384 MB.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from garmin_mcp.db.database import Database, DatabaseOptions
from garmin_mcp.db.raw import land_many


def test_a_page_of_activities_lands_under_the_image_memory_limit(fixtures):
    summary = next(f for f in fixtures if f["type"] == "activity_summary")
    page = []
    for i in range(100):
        body = dict(summary["body"])
        body["activityId"] = 10_000 + i
        # Real summaries are ~6 KB; pad the scrubbed fixture to that size.
        body.update({f"unmapped_padding_{j}": "x" * 40 for j in range(120)})
        page.append((str(10_000 + i), json.dumps(body)))
    tmp = Path(tempfile.mkdtemp(prefix="garmin-mcp-mem-"))
    db = Database.open(DatabaseOptions(str(tmp / "garmin.duckdb"), str(tmp / "snapshot"), memory_limit="384MB", threads=2))
    try:
        with db.transaction() as tx:
            assert len(land_many(tx, "activity_summary", page)) == 100
        assert db.one("SELECT count(*) AS n FROM activity")["n"] == 100
    finally:
        db.close()

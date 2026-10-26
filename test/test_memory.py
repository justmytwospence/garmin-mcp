"""Memory: wide projections stay within a DuckDB memory limit below the image's default
(512 MB), projections and the drift check read only what they need.

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


def test_projecting_one_activity_does_not_read_every_stored_detail(fixtures):
    detail = next(f for f in fixtures if f["type"] == "activity")
    tmp = Path(tempfile.mkdtemp(prefix="garmin-mcp-mem-"))
    pad = {f"unmapped_padding_{j}": "x" * 1000 for j in range(1000)}  # ~1 MB, like real details
    db = Database.open(DatabaseOptions(str(tmp / "garmin.duckdb"), str(tmp / "snapshot")))
    with db.transaction() as tx:
        for i in range(300):
            body = dict(detail["body"], activityId=20_000 + i, **pad)
            land_many(tx, "activity", [(str(20_000 + i), json.dumps(body))])
    db.close()
    db = Database.open(DatabaseOptions(str(tmp / "garmin.duckdb"), str(tmp / "snapshot"), memory_limit="384MB", threads=2))
    try:
        with db.transaction() as tx:
            land_many(tx, "activity", [("19999", json.dumps(dict(detail["body"], activityId=19_999, **pad)))])
        assert db.one("SELECT count(*) AS n FROM activity WHERE activity_id = 19999")["n"] == 1
    finally:
        db.close()


def test_landing_summaries_between_large_payloads_stays_under_the_limit(fixtures):
    """The list_recent path on a real mirror: small summaries stored in the same vectors as
    megabyte activity details. Scanning raw_payload for the summaries' bodies read every
    co-located detail too (a 20-row projection went past a 1.5 GB limit); staging the
    bodies by hash reads only the rows asked for."""
    summary = next(f for f in fixtures if f["type"] == "activity_summary")
    detail = next(f for f in fixtures if f["type"] == "activity")
    chart = next(f for f in fixtures if f["type"] == "activity_details")
    pad = {f"unmapped_padding_{j}": "x" * 1000 for j in range(1000)}  # ~1 MB, like real chart details
    tmp = Path(tempfile.mkdtemp(prefix="garmin-mcp-mem-"))
    db = Database.open(DatabaseOptions(str(tmp / "garmin.duckdb"), str(tmp / "snapshot")))
    with db.transaction() as tx:
        for i in range(240):
            aid = str(30_000 + i)
            land_many(tx, "activity_summary", [(aid, json.dumps(dict(summary["body"], activityId=30_000 + i)))])
            land_many(tx, "activity_details", [(aid, json.dumps(dict(chart["body"], activityId=30_000 + i, **pad)))])
            land_many(tx, "activity", [(aid, json.dumps(dict(detail["body"], activityId=30_000 + i)))])
    db.close()
    db = Database.open(DatabaseOptions(str(tmp / "garmin.duckdb"), str(tmp / "snapshot"), memory_limit="384MB", threads=2))
    try:
        page = [(str(30_000 + i), json.dumps(dict(summary["body"], activityId=30_000 + i, activityName=f"Renamed {i}"))) for i in range(20)]
        with db.transaction() as tx:
            assert len(land_many(tx, "activity_summary", page)) == 20
        assert db.one("SELECT count(*) AS n FROM activity WHERE activity_name LIKE 'Renamed %'")["n"] == 20
        from garmin_mcp.db.raw import current

        assert json.loads(current(db, "activity_summary", "30005")["body"])["activityName"] == "Renamed 5"
    finally:
        db.close()


def _scans(node: dict) -> list[dict]:
    out = [node] if node.get("name") in ("SEQ_SCAN", "TABLE_SCAN") else []
    for child in node.get("children", []):
        out.extend(_scans(child))
    return out


def test_drift_check_samples_the_newest_payloads_without_parsing_every_body(landed_db):
    """The drift query must not parse stored bodies in the table scan: a json_type() filter
    pushed below the sampling semi-join would parse every payload of the type (gigabytes on
    a mirror of many years). Only the sampled rows reach json_keys()."""
    from garmin_mcp.db.projections import PROJECTIONS, drift

    checked = 0
    with landed_db.write() as tx:
        for p in PROJECTIONS:
            for shape in p.shapes:
                if shape.known is None:
                    continue
                plan = tx.all(f"EXPLAIN (FORMAT json) SELECT unnest(json_keys(o)) AS k FROM ({shape.objects}) WHERE o IS NOT NULL")[0]["explain_value"]
                for scan in _scans(json.loads(plan)[0]):
                    info = json.dumps(scan.get("extra_info", {}))
                    if "raw_payload" not in info:
                        continue
                    assert "json" not in info.lower(), (p.type, info)
                    assert "LIMIT" not in info  # the sample is a semi-join on raw_latest, not a scan filter
                    checked += 1
        assert checked > 50
        assert drift(tx) == []


def test_sync_status_reuses_the_drift_check(landed_db):
    from garmin_mcp.tools.mirror_tools import DriftCache

    class M:
        db = landed_db

    calls = []
    cache = DriftCache(M(), ttl=3600)
    import garmin_mcp.tools.mirror_tools as mt

    original = mt.drift
    mt.drift = lambda tx: calls.append(1) or original(tx)
    try:
        assert cache.get() == [] and cache.get() == []
        assert len(calls) == 1
        cache._at = float("-inf")  # expired, whatever the monotonic clock reads
        cache.get()
        assert len(calls) == 2
    finally:
        mt.drift = original

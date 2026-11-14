"""The canonical activity export (schema v1) written from a snapshot, and its HTTP route."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import httpx
import pytest

from garmin_mcp.db.raw import land, mark_not_found
from garmin_mcp.export import CanonicalExporter
from garmin_mcp.fit.decode import land_fit
from garmin_mcp.garmin.client import GarminClient
from garmin_mcp.http import build_app
from garmin_mcp.mirror import Mirror
from garmin_mcp.server import create_server
from garmin_mcp.snapshot import SnapshotPublisher
from garmin_mcp.sync.engine import SyncEngine
from test_weather import SyntheticMeteo

from fake import FakeTransport
from fitgen import activity_fit

SCHEMA = json.loads((Path(__file__).parent / "fixtures" / "schema-v1.json").read_text())
START = datetime(2026, 1, 2, 10, 0, tzinfo=UTC)


@pytest.fixture
def setup(db, fixtures, tmp_path):
    summary = next(f for f in fixtures if f["type"] == "activity_summary")
    aid = int(summary["key"])
    with db.transaction() as tx:
        land(tx, summary["type"], summary["key"], json.dumps(summary["body"]))
        land_fit(tx, "activity_fit", str(aid), activity_fit(START))
    pub = SnapshotPublisher(db)
    exp = CanonicalExporter(tmp_path / "export" / "canonical-v1", importer="garmin-mcp@test")
    return db, summary, aid, pub, exp


def manifest(exp):
    return json.loads(exp.manifest_path.read_text())


def test_more_garmin_types_have_a_canonical_sport():
    from garmin_mcp.export import INDOOR_TYPES, SPORTS, cadence_unit

    assert SPORTS["street_running"] == "run" and SPORTS["ultra_run"] == "run"
    assert SPORTS["virtual_run"] == "treadmill_run" and SPORTS["indoor_running"] == "treadmill_run"
    assert {"virtual_run", "indoor_running"} <= INDOOR_TYPES
    assert SPORTS["cyclocross"] == "gravel_ride" and SPORTS["track_cycling"] == "ride"
    assert SPORTS["rucking"] == "hike" and SPORTS["speed_walking"] == "walk"
    assert SPORTS["backcountry_skiing_snowboarding_ws"] == "backcountry_ski"
    assert cadence_unit(SPORTS["street_running"]) == "cycles" and cadence_unit(SPORTS["cyclocross"]) == "rpm"


def test_export_writes_manifest_and_parquet(setup):
    db, _, aid, pub, exp = setup
    stats = exp.run(pub.publish())
    assert stats["activities"] == 1 and stats["written"] == 1
    m = manifest(exp)
    assert m["schema_version"] == 1 and m["importer"] == "garmin-mcp@test"
    [e] = m["activities"]
    assert e["id"] == f"garmin:{aid}" and e["sources"] == [{"kind": "garmin", "id": str(aid)}] and e["n"] == 30
    assert e["hr_sensor"] is False  # fitgen writes no device info
    assert e["tags"] == []
    f = exp.out_dir / e["file"]
    assert e["sha256"] == hashlib.sha256(f.read_bytes()).hexdigest()
    con = duckdb.connect()
    cols = con.execute(f"DESCRIBE SELECT * FROM '{f}'").fetchall()
    assert [(c[0], c[1]) for c in cols] == [tuple(c) for c in SCHEMA["columns"]]
    rows = con.execute(f"SELECT t, lat FROM '{f}' ORDER BY t").fetchall()
    assert [r[0] for r in rows] == list(range(30)) and len(rows) == e["n"]
    assert abs(rows[1][1] - 45e7) < 1e6


def test_race_event_type_is_tagged(setup):
    db, summary, _, pub, exp = setup
    body = {**summary["body"], "eventType": {"typeId": 1, "typeKey": "race", "sortOrder": 5}}
    with db.transaction() as tx:
        land(tx, summary["type"], summary["key"], json.dumps(body))
    exp.run(pub.publish())
    assert manifest(exp)["activities"][0]["tags"] == ["race"]


def test_unchanged_export_rewrites_nothing(setup):
    _, _, _, pub, exp = setup
    snap = pub.publish()
    exp.run(snap)
    f = exp.out_dir / manifest(exp)["activities"][0]["file"]
    mtime = f.stat().st_mtime_ns
    assert exp.run(snap)["written"] == 0
    assert f.stat().st_mtime_ns == mtime


def test_changed_fit_is_rewritten(setup):
    db, _, aid, pub, exp = setup
    exp.run(pub.publish())
    with db.transaction() as tx:
        land_fit(tx, "activity_fit", str(aid), activity_fit(START, seconds=40))
    stats = exp.run(pub.publish())
    assert stats["written"] == 1 and manifest(exp)["activities"][0]["n"] == 40


def test_deleted_activity_is_removed(setup):
    db, summary, _, pub, exp = setup
    exp.run(pub.publish())
    f = exp.out_dir / manifest(exp)["activities"][0]["file"]
    with db.transaction() as tx:
        mark_not_found(tx, "activity_summary", summary["key"])
    stats = exp.run(pub.publish())
    assert stats["removed"] == 1 and manifest(exp)["activities"] == [] and not f.exists()


def test_export_conforms_to_schema_v1(setup):
    _, _, _, pub, exp = setup
    exp.run(pub.publish())
    m = manifest(exp)
    assert list(m.keys()) == SCHEMA["manifest_keys"]
    assert list(m["activities"][0].keys()) == SCHEMA["entry_keys"]


def test_export_route_requires_token(setup, tmp_path):
    db, _, _, pub, exp = setup
    exp.run(pub.publish())
    client = GarminClient(FakeTransport(), background_gap=0, interactive_gap=0, jitter=0, sleep=lambda s: None)
    engine = SyncEngine(db, client, str(tmp_path), sleep=lambda s: None)
    server = create_server(Mirror(db, engine, client), None)
    app = build_app(server, snapshot=None, exporter=exp, export_token="t")
    disabled = build_app(server, snapshot=None, exporter=exp, export_token="")

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            base = "/export/canonical/v1/"
            assert (await c.get(base + "manifest.json")).status_code == 401
            ok = await c.get(base + "manifest.json", headers={"authorization": "Bearer t"})
            assert ok.status_code == 200 and ok.json()["schema_version"] == 1
            for bad in ("../garmin.duckdb", "activities/x.parquet", "activities/garmin-1.parquet"):
                assert (await c.get(base + bad, headers={"authorization": "Bearer t"})).status_code == 404
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=disabled), base_url="http://test") as c:
            assert (await c.get("/export/canonical/v1/manifest.json", headers={"authorization": "Bearer t"})).status_code == 404

    asyncio.run(go())


def weather_exporter(exp, session):
    return CanonicalExporter(exp.out_dir, importer="garmin-mcp@test", weather=True, weather_session=session)


def test_export_carries_modelled_weather(setup):
    _, _, _, pub, exp = setup
    session = SyntheticMeteo()
    wexp = weather_exporter(exp, session)
    wexp.run(pub.publish())
    [e] = manifest(wexp)["activities"]
    w = e["weather"]
    assert w["version"] == 1 and w["source"] == "open-meteo" and w["api"] == "historical-forecast"
    assert w["t"][0] == 0 and w["temp"][0] == 15.0 and len(w["t"]) == len(w["elev"]) == len(w["dew"]) == len(w["wind"]) == len(w["wind_dir"]) >= 2
    assert session.calls >= 1 and "latitude=45.0000" in session.urls[0]
    # the cache is a sidecar file, never in the served export
    assert (wexp.out_dir.parent / "weather-v1.json").exists() and not list(wexp.out_dir.rglob("weather*"))
    assert list(manifest(wexp)["activities"][0].keys()) == SCHEMA["entry_keys"]


def test_export_weather_off_is_null(setup):
    _, _, _, pub, exp = setup
    exp.run(pub.publish())
    assert manifest(exp)["activities"][0]["weather"] is None
    assert not (exp.out_dir.parent / "weather-v1.json").exists()


def test_weather_is_not_asked_for_again(setup):
    _, _, _, pub, exp = setup
    session = SyntheticMeteo()
    wexp = weather_exporter(exp, session)
    snap = pub.publish()
    wexp.run(snap)
    asked = session.calls
    first = manifest(wexp)["activities"][0]
    wexp.run(snap)
    assert session.calls == asked and manifest(wexp)["activities"][0]["weather"] == first["weather"]
    # a new exporter (a restart) reads the same cache
    weather_exporter(exp, session).run(snap)
    assert session.calls == asked


def test_changed_recording_gets_new_weather(setup):
    db, _, aid, pub, exp = setup
    session = SyntheticMeteo()
    wexp = weather_exporter(exp, session)
    wexp.run(pub.publish())
    asked = session.calls
    with db.transaction() as tx:
        land_fit(tx, "activity_fit", str(aid), activity_fit(START, seconds=40))
    wexp.run(pub.publish())
    assert session.calls > asked and manifest(wexp)["activities"][0]["weather"] is not None


def test_a_daily_limit_does_not_fail_the_export(setup):
    _, _, _, pub, exp = setup
    session = SyntheticMeteo(429, "Daily API request limit exceeded")
    wexp = weather_exporter(exp, session)
    snap = pub.publish()
    wexp.run(snap)
    assert manifest(wexp)["activities"][0]["weather"] is None and session.calls == 1
    wexp.run(snap)
    assert session.calls == 1  # paused until the next UTC day


def test_a_weather_failure_does_not_fail_the_export(setup, monkeypatch):
    _, _, _, pub, exp = setup

    class Boom:
        def get(self, url, timeout=0):
            raise ConnectionError("no network")

    wexp = weather_exporter(exp, Boom())
    wexp.run(pub.publish())
    assert manifest(wexp)["activities"][0]["weather"] is None


def lap(uuid, status="end", mfr="garmin", name="Test Climb", t0=5, t1=15, dist=1000.0, lat=450000000, lng=-450000000):
    def iso(s):
        return (START + timedelta(seconds=s)).isoformat().replace("+00:00", "Z")

    return {
        "uuid": uuid, "status": status, "manufacturer": mfr, "name": name, "start_time": iso(t0), "timestamp": iso(t1),
        "total_distance": dist, "start_position_lat": lat, "start_position_long": lng,
        "end_position_lat": lat + 1000000, "end_position_long": lng + 1000000,
    }


def add_laps(db, aid, laps):
    with db.transaction() as tx:
        fid = tx.execute("SELECT fit_file_id FROM fit_record WHERE activity_id = ? LIMIT 1", [aid]).fetchone()[0]
        for i, fields in enumerate(laps):
            tx.execute(
                "INSERT INTO fit_message (activity_id, fit_file_id, mesg, idx, fields) VALUES (?, ?, 'segment_lap_mesgs', ?, ?)",
                [aid, fid, 900 + i, json.dumps(fields)],
            )


def test_device_segment_efforts_and_segments_json(setup, tmp_path):
    db, _, aid, pub, exp = setup
    add_laps(db, aid, [
        lap("seg-strava", mfr="strava", name="Neutral Hill", t0=20, t1=28, dist=500.0),
        lap("seg-watch", name="Neutral Flat", t0=5, t1=15, dist=1000.0),
        lap("seg-watch", name="Neutral Flat Renamed", t0=16, t1=19, dist=1200.0, lat=460000000),
        lap("seg-fail", status="fail", t0=1, t1=2),
        lap(None, t0=3, t1=4),
        lap("seg-before", t0=-5, t1=3),
    ])
    exp.run(pub.publish())
    [e] = manifest(exp)["activities"]
    assert list(e.keys()) == SCHEMA["entry_keys"]
    src = f"garmin:{aid}"
    assert e["segment_efforts"] == [
        {"segment": "garmin:seg-watch", "t0": 5, "t1": 15, "from": "device", "src": src},
        {"segment": "garmin:seg-watch", "t0": 16, "t1": 19, "from": "device", "src": src},
        {"segment": "strava:seg-strava", "t0": 20, "t1": 28, "from": "device", "src": src},
    ]
    for x in e["segment_efforts"]:
        assert list(x.keys()) == [k for k in SCHEMA["segment_effort_keys"] if k in x]
    doc = json.loads(exp.segments_path.read_text())
    assert list(doc.keys()) == SCHEMA["segments_keys"] and doc["schema_version"] == 1 and doc["importer"] == "garmin-mcp@test"
    assert [list(s.keys()) for s in doc["segments"]] == [SCHEMA["segment_keys"]] * 2
    watch, strava = doc["segments"]
    assert watch["id"] == "garmin:seg-watch" and watch["name"] == "Neutral Flat Renamed" and watch["distance"] == 1100.0
    assert watch["start"] == [round(460000000 * 180 / 2**31, 6), round(-450000000 * 180 / 2**31, 6)]
    assert watch["end"] == [round(461000000 * 180 / 2**31, 6), round(-449000000 * 180 / 2**31, 6)]
    assert watch["sport"] == e["sport"] and watch["starred"] is False and watch["from"] == "device" and watch["created_from"] is None
    assert strava["id"] == "strava:seg-strava" and strava["distance"] == 500.0
    ids = {s["id"] for s in doc["segments"]}
    assert not ids & {"garmin:seg-fail", "strava:seg-fail", "garmin:seg-before", "garmin:None"}

    client = GarminClient(FakeTransport(), background_gap=0, interactive_gap=0, jitter=0, sleep=lambda s: None)
    engine = SyncEngine(db, client, str(tmp_path), sleep=lambda s: None)
    app = build_app(create_server(Mirror(db, engine, client), None), snapshot=None, exporter=exp, export_token="t")

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            url = "/export/canonical/v1/segments.json"
            assert (await c.get(url)).status_code == 401
            r = await c.get(url, headers={"authorization": "Bearer t"})
            assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
            assert r.json() == doc

    asyncio.run(go())


def test_no_segment_laps_gives_empty_efforts(setup):
    _, _, _, pub, exp = setup
    exp.run(pub.publish())
    assert manifest(exp)["activities"][0]["segment_efforts"] == []
    assert json.loads(exp.segments_path.read_text())["segments"] == []


def test_rider_position_from_power_pedal_events(db, fixtures, tmp_path):
    summary = next(f for f in fixtures if f["type"] == "activity_summary")
    aid = int(summary["key"])
    events = [(0, 0), (100, 3), (103, 1), (150, 1), (200, 2), (203, 0)]
    with db.transaction() as tx:
        land(tx, summary["type"], summary["key"], json.dumps(summary["body"]))
        land_fit(tx, "activity_fit", str(aid), activity_fit(START, seconds=300, rider_positions=events))
    exp = CanonicalExporter(tmp_path / "export" / "canonical-v1", importer="garmin-mcp@test")
    exp.run(SnapshotPublisher(db).publish())
    [e] = manifest(exp)["activities"]
    assert list(e.keys()) == SCHEMA["entry_keys"]
    # standing from 100 s (the transition) to 200 s; the repeats (at 0, 103, 150, 203) are dropped
    assert e["rider_position"] == {"t": [100, 200], "standing": [1, 0]}


def test_no_rider_position_events_gives_null(setup):
    _, _, _, pub, exp = setup
    exp.run(pub.publish())
    assert manifest(exp)["activities"][0]["rider_position"] is None

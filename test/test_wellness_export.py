"""The wellness export (wellness-v1) written from a snapshot, and its HTTP route.

test/fixtures/wellness-v1.json is a copy of wellness-lab's schema/wellness-v1.json (justmytwospence/wellness-lab).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import httpx
import pytest

from garmin_mcp.cli import export_kinds, validate_settings
from garmin_mcp.db.raw import land
from garmin_mcp.fit.decode import land_fit
from garmin_mcp.garmin.client import GarminClient
from garmin_mcp.http import build_app
from garmin_mcp.mirror import Mirror
from garmin_mcp.server import create_server
from garmin_mcp.snapshot import SnapshotPublisher
from garmin_mcp.sync.engine import SyncEngine
from garmin_mcp.wellness_export import WellnessExporter

from fake import FakeTransport
from fitgen import hrv_status_fit, sleep_fit, wellness_fit

SCHEMA = json.loads((Path(__file__).parent / "fixtures" / "wellness-v1.json").read_text())
WELLNESS_TYPES = {"sleep", "hrv", "user_summary", "stress", "respiration", "heart_rates", "training_readiness", "max_metrics", "naps", "lifestyle_logging", "sleep_stats"}
DAY1 = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
DAY2 = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)


@pytest.fixture
def setup(db, fixtures, tmp_path):
    with db.transaction() as tx:
        for f in fixtures:
            if f["type"] in WELLNESS_TYPES:
                land(tx, f["type"], f["key"], json.dumps(f["body"]))
        land_fit(tx, "wellness_fit", "2026-10-21", wellness_fit(DAY1, minutes=10))
    pub = SnapshotPublisher(db)
    exp = WellnessExporter(tmp_path / "export" / "wellness-v1", importer="garmin-mcp@test")
    return db, pub, exp


def manifest(exp):
    return json.loads(exp.manifest_path.read_text())


def describe(path):
    con = duckdb.connect()
    return [(c[0], c[1]) for c in con.execute(f"DESCRIBE SELECT * FROM '{path}'").fetchall()]


def test_writes_manifest_and_files(setup):
    db, pub, exp = setup
    stats = exp.run(pub.publish())
    m = manifest(exp)
    assert m["schema_version"] == 1 and m["importer"] == "garmin-mcp@test" and m["source"] == {"kind": "garmin-mcp"}
    assert stats["files"] == len(m["files"]) and stats["written"] == len(m["files"])
    tables = {f["table"] for f in m["files"]}
    assert {"days", "sleeps", "stages", "activities", "tags", "devices", "series"} <= tables
    for f in m["files"]:
        p = exp.out_dir / f["path"]
        assert p.exists() and f["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
        if f["table"] == "series":
            assert describe(p) == [tuple(c) for c in SCHEMA["series"][f["name"]]], f["path"]
        elif f["table"] == "rr":
            assert describe(p) == [tuple(c) for c in SCHEMA["rr"]]
        else:
            assert describe(p) == [tuple(c) for c in SCHEMA["tables"][f["table"]]], f["path"]


def test_series_values(setup):
    db, pub, exp = setup
    exp.run(pub.publish())
    con = duckdb.connect()
    hr = con.execute(f"SELECT t, v FROM read_parquet('{exp.out_dir}/series/hr/2026-10.parquet') ORDER BY t").fetchall()
    assert [r[1] for r in hr][:10] == [60 + i for i in range(10)]
    assert hr[0][0] == int(DAY1.timestamp() * 1000)
    stress = con.execute(f"SELECT v, q FROM read_parquet('{exp.out_dir}/series/stress/2026-10.parquet') ORDER BY t").fetchall()
    assert stress[:3] == [(20.0, 0), (21.0, 0), (22.0, 0)]
    steps = con.execute(f"SELECT v FROM read_parquet('{exp.out_dir}/series/steps/2026-10.parquet') ORDER BY t").fetchall()
    assert [r[0] for r in steps][1:5] == [200.0] * 4
    alt = con.execute(f"SELECT v FROM read_parquet('{exp.out_dir}/series/altitude/2026-10.parquet') ORDER BY t").fetchall()
    assert alt and all(abs(r[0] - 2000) < 0.5 for r in alt)


def test_stages_and_hrv_from_fit(setup):
    db, pub, exp = setup
    with db.transaction() as tx:
        land_fit(tx, "wellness_fit", "2026-11-03", sleep_fit(DAY2, [(30, "light"), (20, "deep"), (10, "rem")]))
        land_fit(tx, "wellness_fit", "2026-11-03", hrv_status_fit(DAY2, [50, 55, 60]))
    exp.run(pub.publish())
    con = duckdb.connect()
    st = con.execute(f"SELECT stage, \"end\" - \"start\" FROM read_parquet('{exp.out_dir}/stages.parquet') ORDER BY \"start\" DESC LIMIT 3").fetchall()
    assert sorted(s[0] for s in st) == [0, 1, 2]  # deep, light, rem
    hrv = con.execute(f"SELECT v FROM read_parquet('{exp.out_dir}/series/hrv5/2026-11.parquet') ORDER BY t").fetchall()
    assert [round(r[0]) for r in hrv] == [50, 55, 60]


def test_unchanged_rewrites_nothing(setup):
    db, pub, exp = setup
    snap = pub.publish()
    exp.run(snap)
    f = exp.out_dir / "series" / "hr" / "2026-10.parquet"
    mtime = f.stat().st_mtime_ns
    assert exp.run(snap)["written"] == 0
    assert f.stat().st_mtime_ns == mtime


def test_changed_month_only(setup):
    db, pub, exp = setup
    exp.run(pub.publish())
    before = {f["path"]: f["sha256"] for f in manifest(exp)["files"]}
    with db.transaction() as tx:
        land_fit(tx, "wellness_fit", "2026-11-03", wellness_fit(DAY2, minutes=5))
    exp.run(pub.publish())
    after = {f["path"]: f["sha256"] for f in manifest(exp)["files"]}
    assert "series/hr/2026-11.parquet" in after and "series/hr/2026-11.parquet" not in before
    assert after["series/hr/2026-10.parquet"] == before["series/hr/2026-10.parquet"]


def make_app(db, exp, tmp_path, token="t"):
    client = GarminClient(FakeTransport(), background_gap=0, interactive_gap=0, jitter=0, sleep=lambda s: None)
    engine = SyncEngine(db, client, str(tmp_path), sleep=lambda s: None)
    server = create_server(Mirror(db, engine, client), None)
    return build_app(server, snapshot=None, wellness_exporter=exp, export_token=token, export_cors_origins=["https://lab.example"])


def test_route_auth_and_cors(setup, tmp_path):
    db, pub, exp = setup
    exp.run(pub.publish())
    app = make_app(db, exp, tmp_path)
    disabled = make_app(db, None, tmp_path)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            base = "/export/wellness/v1/"
            assert (await c.get(base + "manifest.json")).status_code == 401
            ok = await c.get(base + "manifest.json", headers={"authorization": "Bearer t", "origin": "https://lab.example"})
            assert ok.status_code == 200 and ok.json()["schema_version"] == 1
            assert ok.headers["access-control-allow-origin"] == "https://lab.example"
            pre = await c.options(base + "manifest.json", headers={"origin": "https://lab.example"})
            assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == "https://lab.example"
            meta = await c.get(base + "auth")
            assert meta.json()["client_id"] == "wellness-lab" and meta.json()["authorization_endpoint"].startswith("http://test/export/wellness/v1/")
            for bad in ("../garmin.duckdb", "series/x/y.parquet", "rr/garmin-1.parquet", "series/hr/2026-99.parquet"):
                assert (await c.get(base + bad, headers={"authorization": "Bearer t"})).status_code == 404
            f = await c.get(base + "series/hr/2026-10.parquet", headers={"authorization": "Bearer t"})
            assert f.status_code == 200 and f.content[:4] == b"PAR1"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=disabled), base_url="http://test") as c:
            r = await c.get("/export/wellness/v1/manifest.json", headers={"authorization": "Bearer t"})
            assert r.status_code == 404 and "GARMIN_EXPORT=wellness" in r.text

    asyncio.run(go())


def test_settings():
    validate_settings({"GARMIN_EXPORT": "canonical,wellness"})
    assert export_kinds({"GARMIN_EXPORT": "canonical, wellness"}) == {"canonical", "wellness"}
    assert export_kinds({"GARMIN_EXPORT": "off"}) == set() and export_kinds({}) == set()
    with pytest.raises(ValueError, match='comma list of "canonical" and "wellness"'):
        validate_settings({"GARMIN_EXPORT": "bogus"})

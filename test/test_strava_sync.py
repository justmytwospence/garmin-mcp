"""The optional Strava sync: the pure decisions (title pairing and rules, course matching),
then whole runs against the fake Garmin and a synthetic strava-mcp snapshot: renames,
hand-edit conflicts, caps, dry-run, the stale and empty snapshot guards, and the tools.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
from fake import FakeTransport

from garmin_mcp.garmin.client import GarminClient, Response
from garmin_mcp.mirror import Mirror
from garmin_mcp.server import create_server
from garmin_mcp.strava_sync import StravaSync, StravaSyncConfig
from garmin_mcp.strava_sync.config import STRAVA_COURSE_CONSUMERS
from garmin_mcp.strava_sync.reconcile import (
    GarminActivity,
    GarminCourse,
    TitleLink,
    haversine_m,
    is_default_name,
    plan_courses,
    plan_titles,
)
from garmin_mcp.strava_sync.source import StravaActivity, StravaRoute, StravaSnapshot, polyline_start
from garmin_mcp.sync.engine import SyncEngine

STRAVA = next(iter(STRAVA_COURSE_CONSUMERS))
OTHER = "fe4c3a94-87c9-4bab-b4a1-977467cfa4b4"
T0 = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)


# ----------------------------------------------------------------- pure parts


def test_config_is_off_by_default_and_validates():
    assert StravaSyncConfig.from_env({}) is None
    assert StravaSyncConfig.from_env({"STRAVA_SYNC": "off"}) is None
    c = StravaSyncConfig.from_env({"STRAVA_SYNC": "titles", "STRAVA_SNAPSHOT": "/x/strava.duckdb"})
    assert c is not None and c.parts == ["titles"] and c.courses_mode == "dry-run" and not c.is_url
    c = StravaSyncConfig.from_env({"STRAVA_SYNC": "all", "STRAVA_SNAPSHOT": "http://strava-mcp:8000/snapshot.duckdb", "STRAVA_SYNC_COURSES": "on", "STRAVA_SYNC_TITLES_SINCE": "2026-10-01"})
    assert c is not None and c.parts == ["titles", "courses"] and c.is_url and c.courses_mode == "on" and c.titles_since == date(2026, 10, 1)
    c = StravaSyncConfig.from_env({"STRAVA_SYNC": "titles", "STRAVA_EXPORT_URL": "http://strava-mcp:8000/export/garmin-sync", "STRAVA_SNAPSHOT_TOKEN": "t"})
    assert c is not None and c.export_url and c.snapshot is None and c.describe()["source"] == "export" and c.interval == timedelta(minutes=2)
    for bad in ({"STRAVA_SYNC": "titles"}, {"STRAVA_SYNC": "titles", "STRAVA_SNAPSHOT": "x", "STRAVA_EXPORT_URL": "http://y"}, {"STRAVA_SYNC": "titles", "STRAVA_EXPORT_URL": "strava-mcp:8000"}, {"STRAVA_SYNC": "maps", "STRAVA_SNAPSHOT": "x"}, {"STRAVA_SYNC": "courses", "STRAVA_SNAPSHOT": "x", "STRAVA_SYNC_COURSES": "yes"},
                {"STRAVA_SYNC": "titles", "STRAVA_SNAPSHOT": "x", "STRAVA_SYNC_TITLES_SINCE": "last week"}, {"STRAVA_SYNC": "titles", "STRAVA_SNAPSHOT": "x", "STRAVA_SYNC_MAX_DELETES": "-1"}):
        with pytest.raises(ValueError):
            StravaSyncConfig.from_env(bad)


def test_polyline_start_and_distance():
    # Google's documented example: _p~iF~ps|U starts at (38.5, -120.2)
    assert polyline_start("_p~iF~ps|U_ulLnnqC_mqNvxq`@") == pytest.approx((38.5, -120.2))
    assert polyline_start(None) is None and polyline_start("_p~iF") is None
    assert haversine_m((44.0, -121.3), (44.0, -121.3)) == 0
    assert 1100 < haversine_m((44.0, -121.3), (44.01, -121.3)) < 1120


def test_default_names():
    for n in ("Morning Run", "Lunch Ride", "Afternoon Trail Run", "Evening Weight Training", "Night Walk"):
        assert is_default_name(n)
    for n in ("Shevlin", "Morning", "Maize", "Bend Trail Running"):
        assert not is_default_name(n)


def act(i: int, name: str, offset_s: int = 0) -> tuple[StravaActivity, GarminActivity]:
    start = T0 + timedelta(days=i)
    return StravaActivity(100 + i, name, start + timedelta(seconds=offset_s), 3600), GarminActivity(900 + i, f"Garmin {i}", start)


def test_plan_titles_rules():
    s1, g1 = act(1, "Shevlin")  # first sync: rename
    s2, g2 = act(2, "Afternoon Run")  # Strava default: skipped
    s3, g3 = act(3, "Pinkerton")  # unchanged since the last sync
    s4, g4 = act(4, "New name")  # Strava renamed again, Garmin still what we set: rename
    s5, g5 = act(5, "Another")  # Garmin edited by hand since: conflict
    s6, g6 = act(6, "Garmin 6")  # already equal: adopt
    s7, _ = act(7, "Nowhere")  # no Garmin activity
    s8, g8 = act(8, "Late", offset_s=45)  # within 60 s: matched
    s9, g9 = act(9, "Too late", offset_s=90)  # beyond 60 s: unmatched
    links = {
        103: TitleLink(103, 903, "Pinkerton", "Pinkerton"),
        104: TitleLink(104, 904, "Old name", "Garmin 4"),
        105: TitleLink(105, 905, "Before", "Before"),
    }
    plan = plan_titles([s1, s2, s3, s4, s5, s6, s7, s8, s9], [g1, g2, g3, g4, g5, g6, g8, g9], links)
    assert [(r.garmin_id, r.strava_name, r.first) for r in plan.renames] == [(901, "Shevlin", True), (904, "New name", False), (908, "Late", True)]
    assert [r.garmin_id for r in plan.conflicts] == [905]
    assert [r.garmin_id for r in plan.adopt] == [906]
    assert plan.skipped == {"no_garmin_activity": 2, "strava_default_name": 1, "unchanged": 1}


def test_plan_titles_ambiguous_pairs_are_skipped():
    start = T0
    s = [StravaActivity(1, "A", start, 60), StravaActivity(2, "B", start + timedelta(seconds=30), 60)]
    g = [GarminActivity(9, "x", start + timedelta(seconds=10))]
    plan = plan_titles(s, g, {})
    assert not plan.renames and plan.skipped == {"ambiguous_match": 2}


def course(cid: int, name: str, dist: float, start: tuple[float, float] | None, consumer: str | None = STRAVA, days: int = 0) -> GarminCourse:
    return GarminCourse(cid, name, dist, start, consumer, T0 + timedelta(days=days))


def test_plan_courses():
    routes = [
        StravaRoute(1, "Gray Butte", 10000.0, (44.4, -121.1)),
        StravaRoute(2, "Renamed on Strava", 20000.0, (44.0, -121.3)),
        StravaRoute(3, "Never synced", 5000.0, (45.0, -122.0)),
        StravaRoute(4, "Linked", 7000.0, (46.0, -122.0)),
    ]
    courses = [
        course(10, "Gray Butte", 10010.0, (44.4, -121.1), days=1),
        course(11, "Gray Butte", 10010.0, (44.4, -121.1), days=5),  # newer copy: kept, 10 is the duplicate
        course(12, "Old name", 20150.0, (44.0005, -121.3005)),  # geometry match
        course(13, "Unstarred route", 8000.0, (44.2, -121.2)),  # orphan
        course(14, "Old name 2", 26000.0, (44.0, -121.3)),  # 30% longer: orphan
        course(15, "Mine", 1000.0, None, consumer=None),  # made in Garmin: untouched
        course(16, "Ride with GPS", 1000.0, None, consumer=OTHER),  # another app: untouched
        course(17, "Whatever it is called now", 1.0, None),  # linked by an earlier run
    ]
    plan = plan_courses(courses, routes, STRAVA_COURSE_CONSUMERS, {17: 4})
    assert {(d.course_id, d.route_id, d.how) for d in plan.keep} == {(11, 1, "name"), (12, 2, "geometry"), (17, 4, "link")}
    assert {d.course_id: d.reason.split(":")[0] for d in plan.delete} == {10: "duplicate", 13: "orphan", 14: "orphan"}
    assert (plan.considered, plan.other_sources, plan.starred_routes, plan.starred_routes_without_course) == (6, 2, 4, 1)


def test_plan_courses_prefers_a_name_match_over_a_newer_geometry_match():
    routes = [StravaRoute(1, "10.21 mi Sisters Trail Run Route", 16430.0, (44.29, -121.55))]
    courses = [
        course(20, "10.21 mi Sisters Trail Run Route", 16430.0, (44.29, -121.55), days=0),
        course(21, "10.24 mi Sisters Trail Run Route", 16488.0, (44.2901, -121.5501), days=1),
    ]
    plan = plan_courses(courses, routes, STRAVA_COURSE_CONSUMERS)
    assert [d.course_id for d in plan.keep] == [20] and [d.course_id for d in plan.delete] == [21]
    # an edited route: Strava's newer same-name copy replaces the linked older one
    plan = plan_courses([course(30, "Caves", 14280.0, None, days=0), course(31, "Caves", 15826.0, None, days=14)], [StravaRoute(2, "Caves", 15826.0, None)], STRAVA_COURSE_CONSUMERS, {30: 2})
    assert [d.course_id for d in plan.keep] == [31] and [d.course_id for d in plan.delete] == [30]


# ----------------------------------------------------------------- whole runs


def make_snapshot(path: Path, *, routes: list[dict], activities: list[dict], route_list_checked: datetime | None) -> None:
    path.unlink(missing_ok=True)
    con = duckdb.connect(str(path))
    con.execute("SET TimeZone = 'UTC'")
    con.execute("CREATE TABLE raw_latest (object_type VARCHAR, object_key VARCHAR, status VARCHAR, content_hash VARCHAR, fetched_at TIMESTAMPTZ, checked_at TIMESTAMPTZ)")
    con.execute("CREATE TABLE sync_state (name VARCHAR, value VARCHAR, updated_at TIMESTAMPTZ)")
    con.execute("CREATE TABLE route (id BIGINT, name VARCHAR, distance DOUBLE, starred BOOLEAN, summary_polyline VARCHAR)")
    con.execute("CREATE TABLE activity (id BIGINT, name VARCHAR, start_date TIMESTAMPTZ, elapsed_time INTEGER, external_id VARCHAR, deleted_at TIMESTAMPTZ)")
    if route_list_checked is not None:
        con.execute("INSERT INTO raw_latest VALUES ('route_list', 'self', 'ok', 'h', ?, ?)", [route_list_checked, route_list_checked])
    con.execute("INSERT INTO sync_state VALUES ('last:list_recent', '2026-10-25T10:26:35.424Z', now())")
    for r in routes:
        con.execute("INSERT INTO route VALUES (?, ?, ?, ?, ?)", [r["id"], r["name"], r["distance"], r.get("starred", True), r.get("polyline")])
    for a in activities:
        con.execute("INSERT INTO activity VALUES (?, ?, ?, ?, ?, ?)", [a["id"], a["name"], a["start"], 3600, f"garmin_ping_{a['id']}", a.get("deleted_at")])
    con.close()


class CourseTransport(FakeTransport):
    """The fake Garmin with a controllable course list that honours DELETE."""

    def __init__(self, courses: list[dict]) -> None:
        super().__init__()
        self.by_type["courses"] = {"self": courses}

    def _write(self, method: str, path: str, body: Any) -> Response:
        if method == "DELETE" and path.startswith("/course-service/course/"):
            self.writes.append((method, path, body))
            cid = int(path.rsplit("/", 1)[1])
            self.by_type["courses"]["self"] = [c for c in self.by_type["courses"]["self"] if c["courseId"] != cid]
            return Response(204, b"", "")
        return super()._write(method, path, body)


def garmin_course(cid: int, name: str, dist: float, lat: float, lon: float, consumer: str | None = STRAVA, created_ms: int = 1791669761000) -> dict:
    return {"courseId": cid, "courseName": name, "distanceInMeters": dist, "startLatitude": lat, "startLongitude": lon, "consumer": consumer,
            "createdDate": created_ms, "updatedDate": created_ms, "activityType": {"typeId": 6, "typeKey": "trail_running"}, "sourceTypeId": 3}


class World:
    def __init__(self, env: dict[str, str] | None = None, courses: list[dict] | None = None) -> None:
        from conftest import open_db

        self.tmp = Path(tempfile.mkdtemp(prefix="garmin-mcp-strava-"))
        self.db = open_db(self.tmp)
        self.transport = CourseTransport(courses or [])
        self.client = GarminClient(self.transport, background_gap=0, interactive_gap=0, jitter=0, sleep=lambda s: None)
        self.engine = SyncEngine(self.db, self.client, str(self.tmp), history_start="2026-10-10", sleep=lambda s: None)
        # Only the activity listing is needed (the sync re-fetches the course list itself); a full sync is slow.
        self.engine.run_job("list_recent", "self", 5)
        self.db.run("DELETE FROM sync_job")
        self.snapshot_path = self.tmp / "strava.duckdb"
        cfg_env = {"STRAVA_SYNC": "titles,courses", "STRAVA_SNAPSHOT": str(self.snapshot_path), "STRAVA_SYNC_TITLES_SINCE": "2026-10-01", **(env or {})}
        if "STRAVA_EXPORT_URL" in cfg_env:
            del cfg_env["STRAVA_SNAPSHOT"]
        self.config = StravaSyncConfig.from_env(cfg_env)
        assert self.config is not None
        self.sync = StravaSync(self.db, self.engine, self.config)
        self.engine.strava_sync = self.sync
        self.mirror = Mirror(self.db, self.engine, self.client)

    def garmin_activities(self) -> list[dict]:
        return self.db.all("SELECT activity_id, activity_name, start_time_gmt FROM activity WHERE deleted_at IS NULL AND start_time_gmt >= '2026-10-01' ORDER BY start_time_gmt")

    def snapshot(self, *, routes: list[dict] | None = None, activities: list[dict] | None = None, checked: datetime | None = None, fresh: bool = True) -> None:
        make_snapshot(self.snapshot_path, routes=routes or [], activities=activities or [], route_list_checked=(checked or datetime.now(UTC)) if fresh else checked)

    def puts(self) -> list[tuple[str, Any]]:
        return [(p, b) for m, p, b in self.transport.writes if m == "PUT"]

    def deletes(self) -> list[str]:
        return [p for m, p, _ in self.transport.writes if m == "DELETE"]

    def call(self, name: str, args: dict | None = None):
        from mcp import Client

        server = create_server(self.mirror)

        async def go():
            async with Client(server) as c:
                return await c.call_tool(name, args or {})

        return asyncio.run(go())

    def close(self) -> None:
        self.db.close()


@pytest.fixture
def world():
    w = World()
    yield w
    w.close()


def test_titles_run_renames_then_respects_hand_edits(world):
    acts = world.garmin_activities()
    assert len(acts) >= 3
    a, b, c = acts[-1], acts[-2], acts[-3]
    world.snapshot(activities=[
        {"id": 1, "name": "Shevlin", "start": a["start_time_gmt"]},
        {"id": 2, "name": "Afternoon Run", "start": b["start_time_gmt"]},
        {"id": 3, "name": c["activity_name"], "start": c["start_time_gmt"]},
    ])
    r = world.sync.run()
    assert [x["strava_name"] for x in r["titles"]["renamed"]] == ["Shevlin"]
    assert world.puts() == [(f"/activity-service/activity/{a['activity_id']}", {"activityId": a["activity_id"], "activityName": "Shevlin"})]
    assert r["titles"]["adopted"] == 1 and r["titles"]["skipped"] == {"strava_default_name": 1}
    # nothing new: no write
    world.transport.writes.clear()
    world.sync.run()
    assert world.puts() == []
    # renamed by hand on Garmin, then renamed again on Strava: left alone, reported once
    world.transport.by_type["activity_summary"][str(a["activity_id"])]["activityName"] = "My own name"
    world.transport.by_type.setdefault("activity", {})[str(a["activity_id"])] = {"activityId": a["activity_id"], "activityName": "My own name"}
    world.snapshot(activities=[{"id": 1, "name": "Shevlin loop", "start": a["start_time_gmt"]}])
    r = world.sync.run()
    assert world.puts() == [] and [x["garmin_id"] for x in r["titles"]["conflicts"]] == [a["activity_id"]]
    r = world.sync.run()
    assert world.puts() == [] and r["titles"]["conflicts"] == []
    assert world.db.one("SELECT decided FROM strava_link WHERE kind = 'activity' AND strava_id = 1")["decided"] == "conflict"


def test_titles_cap_defers_the_rest(world):
    world.sync.config = world.config = StravaSyncConfig.from_env({"STRAVA_SYNC": "titles", "STRAVA_SNAPSHOT": str(world.snapshot_path), "STRAVA_SYNC_TITLES_SINCE": "2026-10-01", "STRAVA_SYNC_MAX_RENAMES": "2"})
    acts = world.garmin_activities()
    world.snapshot(activities=[{"id": i, "name": f"Name {i}", "start": x["start_time_gmt"]} for i, x in enumerate(acts)])
    r = world.sync.run()
    assert len(r["titles"]["renamed"]) == 2 and r["titles"]["deferred"] == len(acts) - 2
    r = world.sync.run()
    assert len(r["titles"]["renamed"]) == 2


def test_titles_since_defaults_to_first_run(world):
    world.sync.config = StravaSyncConfig.from_env({"STRAVA_SYNC": "titles", "STRAVA_SNAPSHOT": str(world.snapshot_path)})
    world.sync._now = lambda: datetime(2026, 10, 30, tzinfo=UTC)
    acts = world.garmin_activities()
    world.snapshot(activities=[{"id": 1, "name": "Old one", "start": acts[-1]["start_time_gmt"]}])
    r = world.sync.run()
    assert r["titles"]["renamed"] == [] and world.engine.get_state("strava_sync:titles_since") == "2026-10-30"


ROUTES = [{"id": 1, "name": "Gray Butte", "distance": 10000.0}, {"id": 2, "name": "Lava Castle", "distance": 12000.0}]


def courses_world(env: dict[str, str]) -> World:
    courses = [
        garmin_course(10, "Gray Butte", 10000.0, 44.4, -121.1),
        garmin_course(11, "Unstarred", 8000.0, 44.2, -121.2),
        garmin_course(12, "Also unstarred", 9000.0, 44.3, -121.2),
        garmin_course(13, "Made in Garmin", 9000.0, 44.3, -121.2, consumer=None),
    ]
    return World({"STRAVA_SYNC": "courses", **env}, courses=courses)


def test_courses_dry_run_deletes_nothing_then_on_deletes_orphans():
    w = courses_world({})
    try:
        w.snapshot(routes=ROUTES)
        r = w.sync.run()
        assert r["courses"]["orphaned"] == 2 and r["courses"]["deleted"] == [] and "dry-run" in r["courses"]["held"]
        assert w.deletes() == []
        w.sync.config = StravaSyncConfig.from_env({"STRAVA_SYNC": "courses", "STRAVA_SNAPSHOT": str(w.snapshot_path), "STRAVA_SYNC_COURSES": "on"})
        r = w.sync.run()
        assert sorted(w.deletes()) == ["/course-service/course/11", "/course-service/course/12"]
        assert w.db.all("SELECT course_id FROM course ORDER BY 1") == [{"course_id": 10}, {"course_id": 13}]
        assert w.db.all("SELECT strava_id, garmin_id FROM strava_link WHERE kind = 'course'") == [{"strava_id": 1, "garmin_id": 10}]
    finally:
        w.close()


def test_courses_guards():
    w = courses_world({"STRAVA_SYNC_COURSES": "on", "STRAVA_SYNC_MAX_DELETES": "1"})
    try:
        # over the cap: nothing deleted
        w.snapshot(routes=ROUTES)
        r = w.sync.run()
        assert r["courses"]["deleted"] == [] and "over the cap" in r["courses"]["held"] and w.deletes() == []
        # stale route list: courses skipped entirely
        w.snapshot(routes=ROUTES, checked=datetime.now(UTC) - timedelta(days=3))
        r = w.sync.run()
        assert "courses" not in r and any("old" in p for p in r["problems"])
        # no route list at all
        w.snapshot(routes=ROUTES, fresh=False)
        r = w.sync.run()
        assert "courses" not in r and any("no route list" in p for p in r["problems"])
        # no starred routes: refuse to delete every Strava course, even on request
        w.snapshot(routes=[])
        r = w.sync.run(manual={"courses"})
        assert r["courses"]["deleted"] == [] and w.deletes() == []
        # missing snapshot
        w.snapshot_path.unlink()
        r = w.sync.run()
        assert r["problems"] and "no Strava snapshot" in r["problems"][0]
    finally:
        w.close()


def test_engine_schedules_the_job():
    w = courses_world({})
    try:
        w.snapshot(routes=ROUTES)
        w.engine.schedule()
        assert w.db.one("SELECT priority FROM sync_job WHERE kind = 'strava_sync'")["priority"] == 12
        w.db.run("DELETE FROM sync_job WHERE kind <> 'strava_sync'")
        assert w.engine.step() == "worked"
        assert json.loads(w.engine.get_state("strava_sync:last_run"))["courses"]["orphaned"] == 2
        assert w.db.one("SELECT count(*) AS n FROM sync_job WHERE kind = 'strava_sync'")["n"] == 0
    finally:
        w.close()


def test_tools_preview_then_apply():
    w = courses_world({})
    try:
        w.snapshot(routes=ROUTES)
        status = json.loads(w.call("strava_sync_status").content[0].text)
        assert status["settings"]["parts"] == ["courses"]
        plan = json.loads(w.call("strava_sync_plan").content[0].text)
        assert {d["course_id"] for d in plan["courses"]["delete"]} == {11, 12}
        preview = json.loads(w.call("strava_sync_apply", {"part": "courses"}).content[0].text)
        assert preview["status"] == "needs_confirmation" and w.deletes() == []
        assert w.call("strava_sync_apply", {"part": "titles"}).is_error
        done = json.loads(w.call("strava_sync_apply", {"part": "courses", "confirm": True}).content[0].text)
        assert len(done["courses"]["deleted"]) == 2 and len(w.deletes()) == 2
    finally:
        w.close()


def test_tools_absent_when_disabled():
    from conftest import open_db

    tmp = Path(tempfile.mkdtemp(prefix="garmin-mcp-strava-off-"))
    db = open_db(tmp)
    try:
        client = GarminClient(FakeTransport(), background_gap=0, interactive_gap=0, jitter=0, sleep=lambda s: None)
        engine = SyncEngine(db, client, str(tmp), history_start="2026-10-10", sleep=lambda s: None)
        names = [t.name for t in asyncio.run(create_server(Mirror(db, engine, client)).list_tools())]
        assert not [n for n in names if n.startswith("strava_sync")]
        engine.schedule()
        assert db.one("SELECT count(*) AS n FROM sync_job WHERE kind = 'strava_sync'")["n"] == 0
    finally:
        db.close()


# ----------------------------------------------------------------- live export, refresh economy, listing trigger


class ExportServer:
    """A stand-in for strava-mcp's GET /export/garmin-sync."""

    def __init__(self, token: str = "secret") -> None:
        import http.server
        import threading

        self.token = token
        self.body: dict[str, Any] = {"route_list_checked_at": datetime.now(UTC).isoformat(), "activity_list_checked_at": None, "starred_routes": [], "activities": []}
        self.requests: list[tuple[str, str | None]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.requests.append((self.path, self.headers.get("Authorization")))
                if self.headers.get("Authorization") != f"Bearer {outer.token}":
                    self.send_response(401)
                    self.end_headers()
                    return
                data = json.dumps(outer.body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/export/garmin-sync"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


def test_export_source_reads_strava_mcp():
    srv = ExportServer()
    try:
        cfg = StravaSyncConfig.from_env({"STRAVA_SYNC": "titles,courses", "STRAVA_EXPORT_URL": srv.url, "STRAVA_SNAPSHOT_TOKEN": "secret"})
        srv.body["starred_routes"] = [{"id": "3548464556455591588", "name": "Gray Butte", "distance": 10000.5, "start": [44.4, -121.1]}, {"id": "7", "name": "No map", "distance": None, "start": None}]
        srv.body["activities"] = [{"id": "20717784937", "name": "Maize", "start_date": "2026-10-24T21:00:00.000Z", "elapsed_time": 900}]
        from garmin_mcp.strava_sync.source import SourceUnavailable, StravaExport

        data = StravaExport(cfg).read(datetime(2026, 10, 1, tzinfo=UTC))
        assert [r.id for r in data.starred_routes] == [3548464556455591588, 7] and data.starred_routes[0].start == (44.4, -121.1)
        assert data.activities[0].start == datetime(2026, 10, 24, 21, 0, tzinfo=UTC) and data.activities[0].name == "Maize"
        assert "since=2026-10-01" in srv.requests[-1][0] and srv.requests[-1][1] == "Bearer secret"
        bad = StravaSyncConfig.from_env({"STRAVA_SYNC": "titles", "STRAVA_EXPORT_URL": srv.url, "STRAVA_SNAPSHOT_TOKEN": "wrong"})
        with pytest.raises(SourceUnavailable, match="401"):
            StravaExport(bad).read(None)
        srv.body = {"unexpected": True}
        with pytest.raises(SourceUnavailable, match="shape"):
            StravaExport(cfg).read(None)
    finally:
        srv.close()


def course_list_requests(w: World) -> int:
    return sum(1 for m, p, _ in w.transport.calls if m == "GET" and p.startswith("/course-service/course/owner/"))


def test_course_list_is_fetched_only_when_it_could_matter():
    srv = ExportServer()
    w = World({"STRAVA_SYNC": "courses", "STRAVA_EXPORT_URL": srv.url, "STRAVA_SNAPSHOT_TOKEN": "secret"},
              courses=[garmin_course(10, "Gray Butte", 10000.0, 44.4, -121.1), garmin_course(11, "Unstarred", 8000.0, 44.2, -121.2)])
    try:
        w.sync.config = StravaSyncConfig.from_env({"STRAVA_SYNC": "courses", "STRAVA_EXPORT_URL": srv.url, "STRAVA_SNAPSHOT_TOKEN": "secret"})
        srv.body["starred_routes"] = [{"id": "1", "name": "Gray Butte", "distance": 10000.0, "start": [44.4, -121.1]}, {"id": "2", "name": "Unstarred", "distance": 8000.0, "start": [44.2, -121.2]}]
        w.sync.run()
        assert course_list_requests(w) == 1
        w.sync.run()
        w.sync.run()
        assert course_list_requests(w) == 1, "nothing changed on Strava: plan from the mirror"
        srv.body["starred_routes"] = srv.body["starred_routes"][:1]  # unstarred on Strava
        r = w.sync.run()
        assert course_list_requests(w) == 2 and r["courses"]["orphaned"] == 1 and w.deletes() == []  # dry-run
        w.sync._now = lambda: datetime.now(UTC) + timedelta(hours=2)
        w.sync.run()
        assert course_list_requests(w) == 3, "an hour-old course list is re-fetched"
        # deletions always act on a fresh list
        w.sync._now = lambda: datetime.now(UTC)
        w.sync.config = StravaSyncConfig.from_env({"STRAVA_SYNC": "courses", "STRAVA_EXPORT_URL": srv.url, "STRAVA_SNAPSHOT_TOKEN": "secret", "STRAVA_SYNC_COURSES": "on"})
        before = course_list_requests(w)
        r = w.sync.run()
        assert w.deletes() == ["/course-service/course/11"] and course_list_requests(w) == before + 2  # before deciding, after deleting
    finally:
        w.close()
        srv.close()


def test_recent_unmatched_strava_activity_asks_for_the_listing_once(world):
    world.sync._now = lambda: datetime(2026, 10, 25, 18, 0, tzinfo=UTC)
    world.snapshot(activities=[{"id": 50, "name": "Just uploaded", "start": datetime(2026, 10, 25, 16, 0, tzinfo=UTC)},
                               {"id": 51, "name": "Old Wahoo ride", "start": datetime(2026, 10, 20, 16, 0, tzinfo=UTC)}])
    r = world.sync.run()
    assert r["titles"]["listing_requested"] == 1
    assert world.db.one("SELECT priority FROM sync_job WHERE kind = 'list_recent'")["priority"] == 10
    world.db.run("DELETE FROM sync_job")
    r = world.sync.run()
    assert r["titles"]["listing_requested"] == 0 and world.db.one("SELECT count(*) AS n FROM sync_job WHERE kind = 'list_recent'")["n"] == 0


def test_deleted_strava_activities_are_ignored(world):
    a = world.garmin_activities()[-1]
    world.snapshot(activities=[{"id": 1, "name": "Deleted on Strava", "start": a["start_time_gmt"], "deleted_at": datetime.now(UTC)}])
    r = world.sync.run()
    assert r["titles"]["renamed"] == [] and world.puts() == []

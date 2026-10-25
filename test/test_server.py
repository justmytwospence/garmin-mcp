"""The MCP server end to end against the fake Garmin: tools through an in-process
client (provenance blocks, mirror reads, writes with re-fetch), the read-only query
guard, tool filtering, the snapshot publisher and its Range serving, and the HTTP
bearer checks.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import duckdb
import httpx
import pytest

from garmin_mcp.garmin.client import GarminClient
from garmin_mcp.http import bearer_matches, build_app
from garmin_mcp.mirror import Mirror
from garmin_mcp.server import create_server
from garmin_mcp.snapshot import SnapshotPublisher
from garmin_mcp.sync.engine import SyncEngine
from garmin_mcp.tools.base import ToolFilter
from garmin_mcp.tools.mirror_tools import ReadOnlyQuery
from garmin_mcp.tools.profiles import TOOL_PROFILES

from fake import FakeTransport


class World:
    def __init__(self, filters: ToolFilter | None = None, synced: bool = True) -> None:
        from conftest import open_db

        self.tmp = Path(tempfile.mkdtemp(prefix="garmin-mcp-server-"))
        self.db = open_db(self.tmp)
        self.transport = FakeTransport()
        self.client = GarminClient(self.transport, background_gap=0, interactive_gap=0, jitter=0, sleep=lambda s: None)
        self.snapshot = SnapshotPublisher(self.db)
        self.engine = SyncEngine(self.db, self.client, str(self.tmp), history_start="2026-10-10", sleep=lambda s: None, on_change=self.snapshot.mark_dirty)
        self.mirror = Mirror(self.db, self.engine, self.client)
        self.server = create_server(self.mirror, filters)
        if synced:
            self.engine.schedule()
            while self.engine.step() == "worked":
                pass

    def call(self, name: str, args: dict | None = None):
        from mcp import Client

        async def go():
            async with Client(self.server) as c:
                return await c.call_tool(name, args or {})

        return asyncio.run(go())

    def names(self) -> list[str]:
        return [t.name for t in asyncio.run(self.server.list_tools())]

    def close(self) -> None:
        self.db.close()


@pytest.fixture(scope="module")
def world():
    w = World()
    yield w
    w.close()


def text(result) -> str:
    return result.content[0].text


def provenance(result) -> str:
    assert len(result.content) >= 2, "every tool result ends with a provenance block"
    return result.content[-1].text


# ----------------------------------------------------------------- tool surface


def test_every_upstream_profile_name_is_a_registered_tool(world):
    names = set(world.names())
    for profile, tools in TOOL_PROFILES.items():
        if tools is None:
            continue
        assert tools <= names, (profile, sorted(tools - names))
    assert {"garmin_query", "garmin_schema", "garmin_sync_status"} <= names
    assert len(names) >= 150


def test_tool_filters_profile_allowlist_denylist():
    f = ToolFilter.from_env({"GARMIN_TOOL_PROFILE": "minimal", "GARMIN_ENABLED_TOOLS": "get_gear", "GARMIN_DISABLED_TOOLS": "get_stats"})
    assert f.allows("get_activity") and f.allows("get_gear") and not f.allows("get_stats") and not f.allows("upload_workout")
    w = World(f, synced=False)
    try:
        names = set(w.names())
        assert "get_gear" in names and "get_stats" not in names and "upload_workout" not in names
    finally:
        w.close()
    with pytest.raises(ValueError):
        ToolFilter.from_env({"GARMIN_TOOL_PROFILE": "nope"})


# ----------------------------------------------------------------- reads from the mirror


def test_mirror_backed_read_says_where_it_came_from(world):
    r = world.call("get_stats", {"date": "2026-10-21"})
    assert not r.is_error
    body = json.loads(text(r))
    assert body["total_steps"] == 6477
    assert "served from the local mirror" in provenance(r)


def test_activity_detail_and_computed_trends(world):
    aid = world.db.scalar("SELECT activity_id FROM activity ORDER BY start_time_gmt DESC LIMIT 1")
    r = world.call("get_activity", {"activity_id": str(aid)})
    assert json.loads(text(r))["id"] == aid
    r = world.call("get_hrv_trend", {"start_date": "2026-10-15", "end_date": "2026-10-22"})
    body = json.loads(text(r))
    assert body["days_with_data"] >= 1 and any(e.get("last_night_avg_hrv_ms") for e in body["trend"])
    r = world.call("get_vo2max_trend", {"start_date": "2026-10-15", "end_date": "2026-10-22"})
    assert json.loads(text(r))["data_points"] >= 1


def test_fit_analysis_comes_from_the_mirror_tables(world):
    aid = world.db.scalar("SELECT activity_id FROM activity WHERE deleted_at IS NULL ORDER BY start_time_gmt DESC LIMIT 1")
    calls_before = len(world.transport.calls)
    r = world.call("get_activity_fit_data", {"activity_id": aid})
    assert not r.is_error, text(r)
    body = json.loads(text(r))
    assert body["session"]["sport"] and body["record_count"] > 0
    assert "decoded FIT tables" in provenance(r)
    assert len(world.transport.calls) == calls_before, "no Garmin request for a mirrored FIT file"
    r = world.call("get_activity_fit_messages", {"activity_id": aid, "message_types": ["session"]})
    assert json.loads(text(r))["returned"] == 1


def test_empty_answers_are_text_not_errors(world):
    r = world.call("get_gear", {})
    assert not r.is_error and "No gear found" in text(r)
    r = world.call("get_all_day_events", {"date": "2026-10-21"})
    assert not r.is_error and text(r).startswith("No daily wellness events")


def test_invalid_date_is_an_error(world):
    r = world.call("get_sleep_summary", {"date": "21/10/2026"})
    assert r.is_error and "YYYY-MM-DD" in text(r)


def test_stale_copy_when_garmin_is_unavailable(world):
    from garmin_mcp.garmin.client import Unavailable

    world.call("get_user_profile", {})
    world.db.run("UPDATE raw_latest SET checked_at = now() - INTERVAL 30 DAY WHERE object_type = 'user_settings'")
    world.transport.fail_with = Unavailable("API Error 503", 503)
    try:
        r = world.call("get_user_profile", {})
    finally:
        world.transport.fail_with = None
        world.client.state.errors = 0
    assert not r.is_error
    assert "stale" in provenance(r)


# ----------------------------------------------------------------- writes


def test_write_goes_to_garmin_then_refetches(world):
    aid = world.db.scalar("SELECT activity_id FROM activity WHERE deleted_at IS NULL ORDER BY start_time_gmt LIMIT 1")
    r = world.call("set_activity_name", {"activity_id": aid, "activity_name": "Renamed by test"})
    assert not r.is_error, text(r)
    body = json.loads(text(r))
    assert body["success"] and body["write_committed"] is True
    assert ("PUT", f"/activity-service/activity/{aid}") in [(m, p) for m, p, _ in world.transport.writes]
    assert "accepted by Garmin" in provenance(r) and "re-fetched" in provenance(r)
    assert world.db.scalar("SELECT activity_name FROM activity WHERE activity_id = ?", [aid]) == "Renamed by test"


def test_destructive_tools_need_confirmation(world):
    aid = world.db.scalar("SELECT activity_id FROM activity WHERE deleted_at IS NULL ORDER BY start_time_gmt LIMIT 1")
    r = world.call("delete_activity", {"activity_id": aid})
    assert json.loads(text(r))["status"] == "needs_confirmation"
    assert not any(m == "DELETE" for m, _, _ in world.transport.writes)


def test_workout_builder_uploads_and_schedules(world):
    r = world.call("create_run_workout", {"name": "Easy", "run_seconds": 1200, "warmup_min": 5, "cooldown_min": 5})
    assert not r.is_error, text(r)
    assert json.loads(text(r))["workout_id"] == 999001
    r = world.call("schedule_workout", {"workout_id": 999001, "calendar_date": "2026-11-02"})
    assert json.loads(text(r))["status"] == "success"
    assert ("POST", "/workout-service/schedule/999001") in [(m, p) for m, p, _ in world.transport.writes]


def test_write_failure_reports_commit_state(world):
    from garmin_mcp.garmin.client import Unavailable

    world.transport.fail_with = Unavailable("API Error 503", 503)
    try:
        r = world.call("set_activity_description", {"activity_id": 1, "description": "x"})
    finally:
        world.transport.fail_with = None
    assert r.is_error
    assert json.loads(text(r).split("tool set_activity_description: ", 1)[1])["write_committed"] == "unknown"


# ----------------------------------------------------------------- the query guard


@pytest.mark.parametrize(
    "sql",
    ["INSERT INTO sync_state VALUES ('a', 'b', now())", "ATTACH '/tmp/x.duckdb' AS x", "COPY (SELECT 1) TO '/tmp/out.csv'", "SELECT 1; SELECT 2", "CREATE TABLE t AS SELECT 1", "SET memory_limit = '1GB'", "CALL duckdb_tables()"],
)
def test_query_guard_rejects_non_select(world, sql):
    r = world.call("garmin_query", {"sql": sql})
    assert r.is_error, sql
    assert "read-only" in text(r) or "exactly one statement" in text(r)


def test_query_guard_cannot_read_files(world):
    r = world.call("garmin_query", {"sql": "SELECT * FROM read_csv('/etc/passwd')"})
    assert r.is_error and "disabled" in text(r)


def test_query_select_describe_and_row_cap(world):
    r = world.call("garmin_query", {"sql": "SELECT activity_id FROM activity ORDER BY 1", "max_rows": 2})
    body = json.loads(text(r))
    assert body["row_count"] == 2 and "Truncated" in body["note"]
    r = world.call("garmin_query", {"sql": "DESCRIBE activity"})
    assert not r.is_error
    r = world.call("garmin_schema", {"table": "activity"})
    assert json.loads(text(r))["columns"][0]["comment"]
    q = ReadOnlyQuery(world.mirror)
    with pytest.raises(Exception):
        q.check("DELETE FROM activity")


def test_sync_status_reports_login_and_coverage(world):
    body = json.loads(text(world.call("garmin_sync_status", {})))
    assert body["coverage"]["activities"] == 12
    assert body["login"]["state"] == "ready"
    assert "reload_quota" in body and "pacing" in body and "unmapped_fields" in body


# ----------------------------------------------------------------- snapshot and HTTP


def test_snapshot_publish_and_range_serving(world):
    world.snapshot.mark_dirty()
    path = world.snapshot.publish()
    assert path.exists() and not world.snapshot.dirty
    copy = duckdb.connect(str(path), read_only=True)
    assert copy.execute("SELECT count(*) FROM activity").fetchone()[0] == 12
    copy.close()
    app = build_app(world.server, snapshot=world.snapshot, snapshot_token="snap", mcp_token="mcp")
    size = path.stat().st_size

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            assert (await c.get("/health")).json()["status"] == "ok"
            assert (await c.get("/snapshot.duckdb")).status_code == 401
            assert (await c.get("/snapshot.duckdb", headers={"authorization": "Bearer wrong"})).status_code == 401
            head = await c.head("/snapshot.duckdb", headers={"authorization": "Bearer snap"})
            assert head.status_code == 200 and head.headers["accept-ranges"] == "bytes" and int(head.headers["content-length"]) == size
            part = await c.get("/snapshot.duckdb", headers={"authorization": "Bearer snap", "range": "bytes=0-11"})
            assert part.status_code == 206 and part.headers["content-range"] == f"bytes 0-11/{size}" and len(part.content) == 12
            assert part.content[8:12] == b"DUCK"
            tail = await c.get("/snapshot.duckdb", headers={"authorization": "Bearer snap", "range": "bytes=-4"})
            assert tail.status_code == 206 and len(tail.content) == 4
            bad = await c.get("/snapshot.duckdb", headers={"authorization": "Bearer snap", "range": f"bytes={size + 10}-"})
            assert bad.status_code == 416
            assert (await c.post("/mcp", json={})).status_code == 401
            assert (await c.post("/", json={})).status_code == 401
            assert (await c.get("/nothing")).status_code == 404

    asyncio.run(go())


def test_snapshot_disabled_without_token(world):
    app = build_app(world.server, snapshot=world.snapshot, snapshot_token="")

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            assert (await c.get("/snapshot.duckdb", headers={"authorization": "Bearer snap"})).status_code == 404

    asyncio.run(go())


def test_bearer_compare():
    assert bearer_matches("abc", "Bearer abc")
    assert not bearer_matches("abc", "Bearer abd")
    assert not bearer_matches("abc", None)
    assert not bearer_matches("abc", "abc")


class Lifespan:
    """Drive an ASGI app's lifespan (httpx's ASGITransport does not)."""

    def __init__(self, app) -> None:
        self.app = app
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.started = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def __aenter__(self):
        async def receive():
            return await self.inbox.get()

        async def send(message):
            if message["type"] == "lifespan.startup.complete":
                self.started.set()

        self.task = asyncio.create_task(self.app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send))
        await self.inbox.put({"type": "lifespan.startup"})
        await asyncio.wait_for(self.started.wait(), 10)
        return self

    async def __aexit__(self, *exc):
        await self.inbox.put({"type": "lifespan.shutdown"})
        await asyncio.wait_for(self.task, 10)


def test_mcp_over_http_lists_tools(world):
    app = build_app(world.server, snapshot=None, mcp_token="mcp")
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    headers = {"authorization": "Bearer mcp", "accept": "application/json, text/event-stream", "content-type": "application/json"}

    async def go():
        async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            async with c.stream("POST", "/mcp", json=init, headers=headers) as r:
                assert r.status_code == 200
                sid = r.headers.get("mcp-session-id")
                body = ""
                async for chunk in r.aiter_text():
                    body += chunk
                    if '"result"' in body:
                        break
            assert "garmin-mcp" in body
            h2 = {**headers, "mcp-session-id": sid} if sid else headers
            await c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h2)
            async with c.stream("POST", "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=h2) as r:
                body = ""
                async for chunk in r.aiter_text():
                    body += chunk
                    if '"tools"' in body:
                        break
            assert "garmin_query" in body

    asyncio.run(go())

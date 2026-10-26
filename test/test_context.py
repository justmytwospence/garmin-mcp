"""Offline MCP wire contracts against an empty/synthetic mirror and no-network transport."""

import asyncio
import json

import pytest
from mcp import Client

from garmin_mcp.cli import open_runtime
from garmin_mcp.server import create_server
from garmin_mcp.tools.base import CHARACTER_LIMIT, ToolFilter


class NoNetwork:
    display_name = None
    def request(self, *args, **kwargs):
        pytest.fail("Context/discovery must never contact Garmin")
    def call(self, *args, **kwargs):
        pytest.fail("Context/discovery must never call Garmin methods")


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    root = tmp_path_factory.mktemp("mcp-context")
    rt = open_runtime({"GARMIN_DB_PATH": str(root / "garmin.duckdb"), "GARMIN_SYNC": "off", "STRAVA_SYNC": "titles,courses", "STRAVA_SNAPSHOT": str(root / "unused-strava.duckdb")}, start=False)
    rt.client.transport = NoNetwork()
    rt.db.run("INSERT INTO activity (activity_id, activity_name, start_time_local, summary_fetched_at) VALUES (123, 'Synthetic session', '2026-01-01T10:00:00', now())")
    rt.db.run("INSERT INTO activity_lap (activity_id, lap_index) SELECT 123, i FROM range(1, 56) t(i)")
    rt.db.run("INSERT INTO training_readiness (calendar_date, timestamp, score) SELECT DATE '2026-01-01', TIMESTAMPTZ '2026-01-01' + i * INTERVAL 1 HOUR, 70 FROM range(15) t(i)")
    yield rt
    rt.close()


def bookkeeping(rt):
    return {t: rt.db.all(f"SELECT * FROM {t}") for t in ["sync_job", "sync_state", "reload_request", "raw_latest", "request_log"]}


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_discovery_resources_prompts_are_local_bounded_and_honest(runtime, mode):
    before = bookkeeping(runtime)
    server = create_server(runtime.mirror)
    async def go():
        async with Client(server, mode=mode) as c:
            assert "General Garmin quota is unknown" in c.instructions
            assert c.server_capabilities.tools is not None
            assert c.server_capabilities.resources is not None
            assert c.server_capabilities.prompts is not None
            resources = (await c.list_resources()).resources
            assert {"garmin://usage", "garmin://schema", "garmin://status", "garmin://coverage"} <= {str(r.uri) for r in resources}
            # Hermes may omit template discovery and initialization instructions:
            # resources/read of the static guide alone must be sufficient.
            guide = (await c.read_resource("garmin://usage")).contents[0].text
            for example in ["garmin://schema/activity", "garmin://activities/123", "garmin://days/2026-01-01", "activity_review", "recovery_review", "50 laps", "10 readiness", "zero"]:
                assert example in guide
            templates = (await c.list_resource_templates()).resource_templates
            assert {r.uri_template for r in templates} == {"garmin://schema/{table}", "garmin://activities/{activity_id}", "garmin://days/{date}"}
            for resource in resources:
                response = await c.read_resource(str(resource.uri))
                assert sum(len(part.text) for part in response.contents) <= CHARACTER_LIMIT
            for uri in ["garmin://schema/activity", "garmin://activities/123", "garmin://activities/999", "garmin://days/2026-01-01", "garmin://days/2026-01-02"]:
                response = await c.read_resource(uri)
                assert len(response.contents[0].text) <= CHARACTER_LIMIT
                body = json.loads(response.contents[0].text)
                assert body["garmin_requests"] == 0
                assert "potentially stale" in body["coverage_note"]
                data = body["data"]
                if uri.endswith("activities/123"):
                    assert len(data["laps"]) == 50 and data["laps_truncated"]
                    assert data["summary"]["summary_fetched_at"]
                if uri.endswith("activities/999"):
                    assert data["available"] is False
                if uri.endswith("days/2026-01-01"):
                    assert len(data["readiness"]) == 10 and data["readiness_truncated"]
                    assert "sleep" in data["missing"]
            status = json.loads((await c.read_resource("garmin://status")).contents[0].text)["data"]
            assert status["pacing"]["general_quota"]["remaining"] is None
            assert status["pacing"]["observed_at"] is None
            prompts = (await c.list_prompts()).prompts
            assert {p.name for p in prompts} == {"activity_review", "recovery_review"}
            for name, args in [("activity_review", {"activity_id": "123"}), ("recovery_review", {"date": "2026-01-01"})]:
                result = await c.get_prompt(name, args)
                assert "without changing anything" in result.messages[0].content.text
    asyncio.run(go())
    assert before == bookkeeping(runtime)
    assert runtime.client.state.requests == 0


@pytest.mark.parametrize("uri", ["garmin://activities/-1", "garmin://activities/1%20", "garmin://activities/9223372036854775808", "garmin://activities/1;DELETE", "garmin://days/2026-02-30", "garmin://days/20260101", "garmin://schema/activity%27", "garmin://schema/../tokens", "garmin://unknown"])
def test_invalid_resource_uri_fails_without_account_activity(runtime, uri):
    async def go():
        async with Client(create_server(runtime.mirror)) as c:
            with pytest.raises(Exception):
                await c.read_resource(uri)
    asyncio.run(go())
    assert runtime.client.state.requests == 0


def test_invalid_prompt_arguments_and_filtered_discovery(runtime):
    async def go():
        async with Client(create_server(runtime.mirror)) as c:
            for name, args in [("activity_review", {"activity_id": "bad"}), ("recovery_review", {"date": "2026-02-30"})]:
                with pytest.raises(Exception):
                    await c.get_prompt(name, args)
        server = create_server(runtime.mirror, ToolFilter(enabled={"get_stats"}))
        async with Client(server) as c:
            assert not (await c.list_resource_templates()).resource_templates
            assert not (await c.list_prompts()).prompts
            assert {str(r.uri) for r in (await c.list_resources()).resources if str(r.uri).startswith("garmin:")} == {"garmin://usage"}
            for uri in ["garmin://status", "garmin://schema", "garmin://activities/123", "garmin://days/2026-01-01"]:
                with pytest.raises(Exception):
                    await c.read_resource(uri)
    asyncio.run(go())


def test_annotation_inventory_and_compatible_structured_data(runtime):
    writes = {
        "set_activity_name", "set_activity_type", "set_activity_description", "set_activity_event_type", "set_perceived_effort", "set_activity_feel", "set_activity_exercise_sets", "create_manual_activity", "delete_activity",
        "request_reload", "upload_workout", "upload_workouts", "delete_workout", "delete_workouts", "schedule_workout", "schedule_workouts", "reschedule_workout", "unschedule_workout", "unschedule_workouts", "create_walk_run_workout", "create_run_workout", "create_run_interval_workout", "create_z2_walk_workout", "create_strength_workout", "schedule_week",
        "set_heart_rate_zones", "push_workout_to_device", "add_gear_to_activity", "remove_gear_from_activity", "delete_weigh_ins", "add_weigh_in", "add_weigh_in_with_timestamps", "add_body_composition", "set_blood_pressure", "add_hydration_data",
        "download_activity_file", "set_fit_download_dir", "download_course_gpx", "upload_course", "delete_course", "strava_sync_apply", "set_nutrition_daily_settings", "create_custom_food", "update_custom_food", "delete_custom_food", "log_custom_food", "log_food", "delete_food_log", "upsert_and_log",
    }
    local = {"garmin_query", "garmin_schema", "garmin_sync_status", "strava_sync_status", "set_fit_download_dir"}
    async def go():
        async with Client(create_server(runtime.mirror)) as c:
            tools = (await c.list_tools()).tools
            assert len(tools) == 163
            assert {t.name for t in tools if not t.annotations.read_only_hint} == writes
            assert {t.name for t in tools if not t.annotations.open_world_hint} == local
            for t in tools:
                a = t.annotations
                assert t.title and t.description and t.input_schema
                assert "garmin://usage" in t.description
                assert all(isinstance(getattr(a, k), bool) for k in ["read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint"])
                assert t.output_schema is None  # legacy heterogeneous shapes are not falsely declared stable
                if a.read_only_hint:
                    assert not a.destructive_hint and a.idempotent_hint
                if t.name.startswith("set_activity_") or t.name.startswith("download_") and t.name != "download_workout":
                    assert a.destructive_hint
            result = await c.call_tool("garmin_query", {"sql": "SELECT 42 AS n"})
            assert json.loads(result.content[0].text)["rows"] == [{"n": 42}]
            assert result.structured_content["data"] == json.loads(result.content[0].text)
            assert result.structured_content["provenance"] == result.content[-1].text
            truncated = await c.call_tool("garmin_query", {"sql": "SELECT repeat('x', 70000) AS big"})
            assert "truncated" in truncated.content[0].text
            assert truncated.structured_content is None
            rejected = await c.call_tool("garmin_query", {"sql": "EXPLAIN ANALYZE DELETE FROM activity"})
            assert rejected.is_error
            for name, args in [("download_course_gpx", {"course_id": 1, "output_path": str(runtime.mirror.fetch.data_dir / "fit" / "x.gpx")}), ("download_activity_file", {"activity_id": 1, "output_dir": str(runtime.mirror.fetch.data_dir / "fit")})]:
                rejected = await c.call_tool(name, args)
                assert rejected.is_error and "protected" in rejected.content[0].text
    asyncio.run(go())
    assert runtime.client.state.requests == 0

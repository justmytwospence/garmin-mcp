"""The sync engine end-to-end against a fake Garmin: scheduling, planning, draining the
queue, pacing, backoff, auth failure, archive reload quota, idempotent planning.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fake import FakeTransport, FlakyTransport, RateLimitedTransport

from garmin_mcp.garmin.client import AuthError, GarminClient, RateLimited
from garmin_mcp.sync.engine import RELOADS_PER_DAY, SyncEngine


def make_engine(transport=None, **kw):
    tmp = Path(tempfile.mkdtemp(prefix="garmin-mcp-engine-"))
    from conftest import open_db

    db = open_db(tmp)
    transport = transport or FakeTransport()
    client = GarminClient(transport, background_gap=0, interactive_gap=0, jitter=0, sleep=lambda s: None)
    engine = SyncEngine(db, client, str(tmp), history_start="2026-10-10", sleep=lambda s: None, **kw)
    return db, transport, client, engine


def drain(engine, limit=5000):
    worked = 0
    while worked < limit:
        r = engine.step()
        if r == "idle":
            return worked
        if r == "waiting":
            return worked
        worked += 1
    return worked


def test_first_run_lands_profile_and_activities_then_backfills():
    db, transport, client, engine = make_engine()
    engine.schedule()
    assert db.scalar("SELECT count(*) FROM sync_job") > 0
    drain(engine)
    assert db.scalar("SELECT count(*) FROM user_profile") == 1
    assert db.scalar("SELECT count(*) FROM activity") == 12
    # planning after the profile and activities filled the queue with per-day, per-activity and FIT jobs
    engine.plan()
    drain(engine)
    kinds = {r["kind"] for r in db.all("SELECT DISTINCT kind FROM sync_log")}
    assert "day:heart_rates" in kinds and "act:activity" in kinds and "activity_fit" in kinds and "wellness_fit" in kinds
    assert "range:daily_summary_v2" in kinds and "snap:device_settings" in kinds and "snap:workout" in kinds
    assert db.scalar("SELECT count(*) FROM sync_job WHERE attempts = 0") == 0
    assert db.scalar("SELECT count(*) FROM activity WHERE detail_fetched_at IS NOT NULL") >= 4
    assert db.scalar("SELECT count(*) FROM fit_record") > 0
    assert db.scalar("SELECT count(*) FROM fit_monitoring") > 0
    assert db.scalar("SELECT count(*) FROM raw_file WHERE kind = 'activity_fit'") == 12
    assert (Path(engine.fetch.data_dir) / "fit" / "activities").exists()
    # days without data are marked not_found, so they are not re-planned
    assert db.scalar("SELECT count(*) FROM raw_latest WHERE status = 'not_found'") > 0
    # planning again adds nothing: the planner is idempotent on data that never matches
    before = db.scalar("SELECT count(*) FROM sync_job")
    assert engine.plan() == 0
    assert db.scalar("SELECT count(*) FROM sync_job") == before
    status = engine.status()
    assert status["coverage"]["activities"] == 12
    assert status["pacing"]["requests"] > 50


def test_device_poll_triggers_window_refresh():
    db, transport, client, engine = make_engine()
    engine.schedule()
    drain(engine)
    engine.set_state("device_upload_time", "1970-01-01T00:00:00")
    engine.run_job("poll", "self", 10)
    kinds = {r["kind"] for r in db.all("SELECT kind FROM sync_job")}
    assert "window" in kinds and "list_recent" in kinds
    calls_before = len(transport.calls)
    engine.run_job("window", "self", 10)
    assert len(transport.calls) - calls_before >= 20


def test_rate_limit_stops_background_work():
    db, transport, client, engine = make_engine(RateLimitedTransport())
    engine.schedule()
    assert engine.step() == "waiting"
    assert client.backoff_remaining() > 0
    assert db.scalar("SELECT count(*) FROM sync_log WHERE outcome = 'rate_limited'") == 1
    with pytest.raises(RateLimited):
        client.get_text("/x", priority="interactive")
    assert client.state.requests == 1  # the interactive call never reached Garmin


def test_auth_error_stops_everything_without_retrying_login():
    db, transport, client, engine = make_engine()
    transport.fail_with = AuthError("API Error 401", 401)
    engine.schedule()
    assert engine.step() == "waiting"
    assert client.state.auth_error
    assert engine.step() == "waiting"
    assert len(transport.calls) == 1
    assert engine.status()["pacing"]["auth_error"]


def test_server_errors_are_retried():
    db, transport, client, engine = make_engine(FlakyTransport(failures=2))
    assert client.get_text("/userprofile-service/userprofile/settings", priority="interactive")
    assert client.state.errors == 2


def test_pacing_keeps_a_gap_between_calls():
    slept: list[float] = []
    transport = FakeTransport()
    client = GarminClient(transport, background_gap=1.5, interactive_gap=0.5, jitter=0, sleep=slept.append)
    client.get_text("/userprofile-service/userprofile/settings")
    client.state.last_request_at = __import__("time").monotonic()
    client.get_text("/userprofile-service/userprofile/settings")
    assert slept and 1.4 < slept[-1] <= 1.5


def test_archive_reload_quota():
    db, transport, client, engine = make_engine()
    engine.schedule()
    drain(engine)
    engine.plan()
    drain(engine)
    # make 40 old days look archived: summary with steps, heart rate day with no samples
    old = datetime.now(UTC).date() - timedelta(days=400)
    for i in range(40):
        d = (old - timedelta(days=i)).isoformat()
        db.run("INSERT OR REPLACE INTO daily_summary (calendar_date, total_steps, includes_wellness_data, fetched_at) VALUES (?, 1000, true, now())", [d])
        db.run("INSERT OR REPLACE INTO heart_rate_day (calendar_date, sample_count, fetched_at) VALUES (?, 0, now())", [d])
        # ... and their wellness FIT download came back missing, so only a reload can fill them
        db.run("INSERT OR REPLACE INTO raw_latest VALUES ('wellness_fit', ?, 'not_found', NULL, NULL, now())", [d])
    engine.plan()
    assert db.scalar("SELECT count(*) FROM sync_job WHERE kind = 'reload'") == 40
    drain(engine)
    assert db.scalar("SELECT count(*) FROM reload_request") == RELOADS_PER_DAY
    assert db.scalar("SELECT count(*) FROM sync_job WHERE kind = 'reload' AND not_before > now()") == 40 - RELOADS_PER_DAY
    assert db.scalar("SELECT count(*) FROM sync_job WHERE kind = 'refetch_day' AND not_before > now()") == RELOADS_PER_DAY
    # planning again does not duplicate requests
    engine.plan()
    assert db.scalar("SELECT count(*) FROM sync_job WHERE kind = 'reload'") == 40 - RELOADS_PER_DAY
    # a due refetch re-fetches intraday and records it
    db.run("UPDATE sync_job SET not_before = now() - INTERVAL 1 MINUTE WHERE kind = 'refetch_day'")
    engine.step()
    assert db.scalar("SELECT count(*) FROM reload_request WHERE refetched_at IS NOT NULL") == 1


def test_archived_days_covered_by_wellness_fit_need_no_reload():
    db, transport, client, engine = make_engine()
    engine.schedule()
    drain(engine)
    engine.plan()
    drain(engine)
    old = datetime.now(UTC).date() - timedelta(days=400)
    days = [(old - timedelta(days=i)).isoformat() for i in range(3)]
    for d in days:
        db.run("INSERT OR REPLACE INTO daily_summary (calendar_date, total_steps, includes_wellness_data, fetched_at) VALUES (?, 1000, true, now())", [d])
        db.run("INSERT OR REPLACE INTO heart_rate_day (calendar_date, sample_count, fetched_at) VALUES (?, 0, now())", [d])
        db.run("DELETE FROM raw_latest WHERE object_type = 'wellness_fit' AND object_key = ?", [d])
        db.run("DELETE FROM sync_job WHERE kind = 'reload' AND key = ?", [d])
    covered, missing, pending = days
    db.run("INSERT OR REPLACE INTO raw_latest VALUES ('wellness_fit', ?, 'ok', 'h', now(), now())", [covered])
    db.run("INSERT INTO fit_monitoring (calendar_date, fit_file_id, idx, heart_rate) VALUES (?, 'w', 0, 60)", [covered])
    db.run("INSERT OR REPLACE INTO raw_latest VALUES ('wellness_fit', ?, 'not_found', NULL, NULL, now())", [missing])
    # a reload queued before the FIT arrived is dropped once the FIT covers the day
    db.run("INSERT INTO sync_job (kind, key, priority) VALUES ('reload', ?, 50)", [covered])
    engine.plan()
    queued = {r["key"] for r in db.all("SELECT key FROM sync_job WHERE kind = 'reload'")}
    assert missing in queued
    assert covered not in queued and pending not in queued

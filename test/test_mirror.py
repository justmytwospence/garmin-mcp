"""The mirror against scrubbed real payloads: landing, projection into typed tables,
drift, deletion, history, FIT decoding.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from fitgen import activity_fit, wellness_fit

from garmin_mcp.db.projections import PROJECTIONS, drift, rebuild_all
from garmin_mcp.db.raw import land, land_many, mark_not_found
from garmin_mcp.db.tables import TABLES
from garmin_mcp.fit.decode import land_fit
from garmin_mcp.fit.tables import FIT_TABLES
from garmin_mcp.garmin.datasets import DATASETS


def count(db, table: str, where: str = "true") -> int:
    return int(db.scalar(f"SELECT count(*) FROM {table} WHERE {where}"))


def test_every_typed_table_matches_its_definition(landed_db):
    for t in TABLES + FIT_TABLES:
        cols = [r["name"] for r in landed_db.all("SELECT column_name AS name FROM duckdb_columns() WHERE table_name = ? ORDER BY column_index", [t.name])]
        assert cols == [c.name for c in t.columns], t.name


def test_every_table_and_typed_column_has_a_comment(landed_db):
    typed = [t.name for t in TABLES + FIT_TABLES]
    missing = landed_db.all(
        "SELECT table_name, column_name FROM duckdb_columns() WHERE schema_name = 'main' AND NOT internal AND comment IS NULL AND table_name IN (SELECT unnest(?::VARCHAR[]))",
        [typed],
    )
    assert missing == []
    tables = landed_db.all("SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main' AND NOT internal AND NOT temporary AND comment IS NULL AND table_name <> 'schema_migration'")
    assert tables == []


def test_every_dataset_has_a_projection():
    types = {p.type for p in PROJECTIONS}
    for ds in DATASETS:
        assert ds.name in types, ds.name
    assert "activity_summary" in types


def test_no_field_garmin_returned_is_left_unmapped(landed_db):
    with landed_db.write() as tx:
        assert drift(tx) == []


def test_activities_merge_summary_and_detail(landed_db, fixtures):
    summaries = [f for f in fixtures if f["type"] == "activity_summary"]
    details = [f for f in fixtures if f["type"] == "activity"]
    assert count(landed_db, "activity") == len(summaries)
    assert count(landed_db, "activity", "detail_fetched_at IS NOT NULL") == len(details)
    assert count(landed_db, "activity", "time_zone IS NOT NULL") == len(details)
    assert count(landed_db, "activity", "start_time_gmt IS NOT NULL AND activity_type IS NOT NULL AND distance IS NOT NULL") == len(summaries)
    assert count(landed_db, "activity_lap") > 0
    assert count(landed_db, "activity_lap_event") > 0
    assert count(landed_db, "activity_typed_split") > 0
    assert count(landed_db, "activity_split_summary") > 0
    assert count(landed_db, "activity_weather") == len(details)
    assert count(landed_db, "activity_time_in_zone", "kind = 'hr'") > 0
    assert count(landed_db, "activity_time_in_zone", "kind = 'power'") > 0
    assert count(landed_db, "activity_polyline") == len(details)
    assert count(landed_db, "activity_detail_metric") > 0


def test_detail_only_activity_gets_summary_columns_from_summary_dto(db, fixtures):
    detail = next(f for f in fixtures if f["type"] == "activity")
    with db.transaction() as tx:
        land(tx, "activity", detail["key"], json.dumps(detail["body"]))
    row = db.one("SELECT activity_type, distance, start_time_gmt, summary_fetched_at, detail_fetched_at FROM activity")
    assert row is not None
    assert row["activity_type"] and row["distance"] is not None and row["start_time_gmt"] is not None
    assert row["summary_fetched_at"] is None and row["detail_fetched_at"] is not None


def test_daily_wellness_tables(landed_db):
    assert count(landed_db, "daily_summary") == 2
    assert count(landed_db, "heart_rate_day") == 2
    assert count(landed_db, "heart_rate_sample") > 0
    assert count(landed_db, "heart_rate_day", "sample_count = 0") == 1  # the archived day
    assert count(landed_db, "stress_sample", "body_battery_level IS NOT NULL AND stress_level IS NOT NULL") > 0
    assert count(landed_db, "respiration_sample") > 0
    assert count(landed_db, "floors_interval") > 0
    assert count(landed_db, "sleep") == 2
    assert count(landed_db, "sleep", "sleep_score IS NOT NULL AND sleep_start_timestamp_gmt IS NOT NULL") == 2
    assert count(landed_db, "sleep_level") > 0
    assert count(landed_db, "sleep_movement") > 0
    assert landed_db.scalar("SELECT count(DISTINCT series) FROM sleep_sample") == 6
    assert count(landed_db, "hrv") >= 2
    assert count(landed_db, "hrv_reading") > 0
    assert count(landed_db, "hrv", "weekly_avg IS NOT NULL") >= 4  # summaries from the range endpoint too
    assert count(landed_db, "training_readiness") > 0
    assert count(landed_db, "training_status", "vo2max IS NOT NULL AND training_status_code IS NOT NULL") == 2
    assert count(landed_db, "max_metrics", "vo2max IS NOT NULL") > 0
    assert count(landed_db, "endurance_score") > 0
    assert count(landed_db, "hill_score") > 0
    assert count(landed_db, "fitness_age") > 0
    assert count(landed_db, "sleep_stats") > 0
    assert count(landed_db, "steps_daily") > 0
    assert count(landed_db, "daily_summary_v2", "steps IS NOT NULL") > 0
    assert count(landed_db, "wellness_metric") > 0
    assert count(landed_db, "body_battery_day") > 0
    assert count(landed_db, "body_battery_event") > 0
    assert count(landed_db, "all_day_event") > 0
    assert count(landed_db, "nap") > 0
    assert count(landed_db, "hydration") > 0
    assert count(landed_db, "steps_interval") > 0
    assert count(landed_db, "intensity_minutes_day") > 0
    assert count(landed_db, "spo2_day") > 0
    assert count(landed_db, "cycling_ability") > 0


def test_profile_and_snapshots(landed_db):
    assert count(landed_db, "user_profile") == 1
    row = landed_db.one("SELECT display_name, time_zone, measurement_system FROM user_profile")
    assert row["display_name"] and row["time_zone"]
    assert count(landed_db, "user_settings") == 1
    assert count(landed_db, "device") > 0
    assert count(landed_db, "device", "NOT historical") > 0
    assert count(landed_db, "device_settings") == 1
    assert count(landed_db, "device_last_used") == 1
    assert count(landed_db, "sensor") > 0
    assert count(landed_db, "heart_rate_zones") == 3
    assert count(landed_db, "power_zones") > 0
    assert count(landed_db, "personal_record") > 0
    assert count(landed_db, "badge", "earned") > 0
    assert count(landed_db, "badge", "NOT earned") > 0
    assert count(landed_db, "challenge") > 0
    assert count(landed_db, "goal") > 0
    assert count(landed_db, "workout") > 0
    assert count(landed_db, "workout", "detail IS NOT NULL") == 1
    assert count(landed_db, "calendar_item") > 0
    assert count(landed_db, "course") > 0
    assert count(landed_db, "course", "detail IS NOT NULL") == 1
    assert count(landed_db, "snapshot") >= 10


def test_unchanged_payload_adds_no_history(landed_db, fixtures):
    f = next(x for x in fixtures if x["type"] == "activity_summary")
    before = count(landed_db, "raw_payload")
    with landed_db.transaction() as tx:
        changed = land(tx, f["type"], f["key"], json.dumps(f["body"]))
    assert changed is False
    assert count(landed_db, "raw_payload") == before


def test_edit_adds_version_and_updates_typed_row(landed_db, fixtures):
    f = next(x for x in fixtures if x["type"] == "activity_summary")
    edited = {**f["body"], "activityName": "Renamed"}
    with landed_db.transaction() as tx:
        changed = land_many(tx, f["type"], [(f["key"], json.dumps(edited))])
    assert changed == [f["key"]]
    assert count(landed_db, "raw_payload", f"object_type = 'activity_summary' AND object_key = '{f['key']}'") == 2
    assert count(landed_db, "activity", f"activity_id = {f['key']} AND activity_name = 'Renamed'") == 1


def test_64bit_ids_keep_precision(db):
    big = "9223372036854775000"
    with db.transaction() as tx:
        land(tx, "activity_summary", big, json.dumps({"activityId": int(big), "activityName": "big", "startTimeGMT": "2026-01-01 10:00:00"}))
    assert count(db, "activity", f"activity_id = {big}") == 1


def test_not_found_marks_activity_deleted_but_keeps_data(landed_db, fixtures):
    f = [x for x in fixtures if x["type"] == "activity_summary"][-1]
    with landed_db.transaction() as tx:
        mark_not_found(tx, "activity_summary", f["key"])
    assert count(landed_db, "activity", f"activity_id = {f['key']} AND deleted_at IS NOT NULL") == 1
    assert count(landed_db, "raw_payload", f"object_type = 'activity_summary' AND object_key = '{f['key']}'") == 1


def test_not_found_day_leaves_no_typed_row(db):
    with db.transaction() as tx:
        mark_not_found(tx, "heart_rates", "2020-01-01")
    assert count(db, "heart_rate_day") == 0
    assert db.one("SELECT status FROM raw_latest WHERE object_type = 'heart_rates'")["status"] == "not_found"


def test_race_predictions_keep_history(landed_db, fixtures):
    f = next(x for x in fixtures if x["type"] == "race_predictions_latest")
    with landed_db.transaction() as tx:
        land(tx, "race_predictions", "self", json.dumps(f["body"]))
        land(tx, "race_predictions", "self", json.dumps({**f["body"], "time5K": 1}), at=datetime(2030, 1, 1, tzinfo=UTC))
    assert count(landed_db, "race_prediction") == 2


def test_rebuild_from_raw_is_idempotent(landed_db):
    before = {t.name: count(landed_db, t.name) for t in TABLES}
    with landed_db.transaction() as tx:
        rebuild_all(tx)
    after = {t.name: count(landed_db, t.name) for t in TABLES}
    assert before == after


def test_fit_decoding(db):
    with db.transaction() as tx:
        s = land_fit(tx, "activity_fit", "123", activity_fit(datetime(2026, 1, 2, 10, 0, tzinfo=UTC)))
        w = land_fit(tx, "wellness_fit", "2026-01-02", wellness_fit(datetime(2026, 1, 2, 0, 0, tzinfo=UTC)))
    assert s["errors"] == [] and w["errors"] == []
    assert count(db, "fit_file") == 2
    assert count(db, "fit_record", "activity_id = 123") == 30
    rec = db.one("SELECT position_lat, position_long, speed, heart_rate, power, extra FROM fit_record WHERE idx = 1")
    assert abs(rec["position_lat"] - 45.0) < 0.01 and abs(rec["position_long"] + 120.0) < 0.01
    assert rec["speed"] == 3.0 and rec["heart_rate"] == 121 and rec["power"] == 201
    assert json.loads(rec["extra"]) == {"250": 7}  # the unknown field is kept
    assert count(db, "fit_session") == 1 and count(db, "fit_lap") == 1
    assert count(db, "fit_event") == 2
    assert count(db, "fit_hrv") == 3  # invalid intervals dropped
    assert count(db, "fit_message", "mesg = '300'") == 1  # unknown message kept whole
    assert count(db, "fit_message", "mesg = 'activity_mesgs'") == 1
    assert count(db, "fit_monitoring", "calendar_date = DATE '2026-01-02' AND steps IS NOT NULL") == 10
    assert count(db, "fit_stress_level") == 10
    assert count(db, "fit_respiration_rate", "respiration_rate = 15.0") == 10
    # re-landing replaces rather than duplicates
    with db.transaction() as tx:
        land_fit(tx, "activity_fit", "123", activity_fit(datetime(2026, 1, 2, 10, 0, tzinfo=UTC)))
    assert count(db, "fit_record") == 30


def test_fit_file_prefix_isolation(db):
    """Deleting activity 12 must not touch activity 123."""
    with db.transaction() as tx:
        land_fit(tx, "activity_fit", "12", activity_fit(datetime(2026, 1, 2, 10, 0, tzinfo=UTC), 5))
        land_fit(tx, "activity_fit", "123", activity_fit(datetime(2026, 1, 2, 10, 0, tzinfo=UTC), 7))
        land_fit(tx, "activity_fit", "12", activity_fit(datetime(2026, 1, 2, 10, 0, tzinfo=UTC), 5))
    assert count(db, "fit_record", "activity_id = 123") == 7
    assert count(db, "fit_record", "activity_id = 12") == 5

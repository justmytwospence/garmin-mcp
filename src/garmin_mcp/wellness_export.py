"""The wellness export: the mirror's non-activity data (sleep, HRV, heart rate, stress, respiration, SpO2, Body
Battery, steps, altitude, daily summaries) in Wellness Lab's `wellness-v1` format, written from each published snapshot
and served at /export/wellness/v1/. The contract is wellness-lab's schema/SPEC.md; test/fixtures/wellness-v1.json lists
every column and DuckDB's DESCRIBE of each file must equal it.

Layout of the export directory (GARMIN_EXPORT_DIR, default <data dir>/export, plus wellness-v1/):

    manifest.json                       schema_version, importer, generated_at, source and one entry per file
    days.parquet sleeps.parquet stages.parquet activities.parquet tags.parquet devices.parquet
    series/<name>/<YYYY-MM>.parquet     intraday series, one file per UTC month
    rr/garmin-<activity id>.parquet     beat-to-beat intervals (ms) of activities recorded with Log HRV

Turned on with GARMIN_EXPORT=wellness (or canonical,wellness); served with bearer EXPORT_TOKEN. The exporter reads the
snapshot over its own read-only DuckDB connection, never the live mirror, and rewrites only the files whose content
changed (a series month by its row count, time range and value sum; a table by the hash of its bytes), so a routine
publish writes a few files.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
from starlette.requests import Request
from starlette.responses import FileResponse, Response

from .export import SPORTS, _sha256

log = logging.getLogger("garmin_mcp.wellness_export")

SCHEMA_VERSION = 1
_SERVABLE = re.compile(r"manifest\.json|(days|sleeps|stages|activities|tags|devices)\.parquet|series/[a-z0-9_]+/\d{4}-\d{2}\.parquet|rr/garmin-\d+\.parquet")
DAY = "(calendar_date - DATE '1970-01-01')::INTEGER"

# name -> (SQL giving t BIGINT plus the series' columns, the columns after t). Each series falls back to the API table
# only for the calendar days the FIT table has no rows for. Every column is cast to its schema type.
SERIES: dict[str, tuple[str, list[str]]] = {
    "hr": (
        """
        SELECT t, v FROM (
          SELECT epoch_ms(timestamp)::BIGINT AS t, heart_rate::FLOAT AS v, 0 AS pri
            FROM g.fit_monitoring WHERE timestamp IS NOT NULL AND heart_rate > 0
          UNION ALL
          SELECT epoch_ms(timestamp_gmt)::BIGINT, heart_rate::FLOAT, 1
            FROM g.heart_rate_sample
            WHERE heart_rate > 0
              AND calendar_date NOT IN (SELECT DISTINCT calendar_date FROM g.fit_monitoring WHERE heart_rate > 0)
        ) QUALIFY row_number() OVER (PARTITION BY t ORDER BY pri) = 1
        """,
        ["v"],
    ),
    "stress": (
        """
        SELECT t, v, q FROM (
          SELECT epoch_ms(stress_level_time)::BIGINT AS t,
                 CASE WHEN stress_level_value >= 0 THEN stress_level_value END::FLOAT AS v,
                 CASE WHEN stress_level_value >= 0 THEN 0 WHEN stress_level_value = -2 THEN 2 ELSE 1 END::INTEGER AS q, 0 AS pri
            FROM g.fit_stress_level WHERE stress_level_time IS NOT NULL AND stress_level_value IS NOT NULL
          UNION ALL
          SELECT epoch_ms(timestamp_gmt)::BIGINT,
                 CASE WHEN stress_level >= 0 THEN stress_level END::FLOAT,
                 CASE WHEN stress_level >= 0 THEN 0 WHEN stress_level = -2 THEN 2 ELSE 1 END::INTEGER, 1
            FROM g.stress_sample
            WHERE stress_level IS NOT NULL
              AND calendar_date NOT IN (SELECT DISTINCT calendar_date FROM g.fit_stress_level)
        ) QUALIFY row_number() OVER (PARTITION BY t ORDER BY pri) = 1
        """,
        ["v", "q"],
    ),
    "body_battery": (
        """
        SELECT t, v, q FROM (
          SELECT epoch_ms(timestamp_gmt)::BIGINT AS t, body_battery_level::FLOAT AS v,
                 CASE WHEN body_battery_status IS NULL OR body_battery_status = 'MEASURED' THEN 0 ELSE 1 END::INTEGER AS q, 0 AS pri
            FROM g.stress_sample WHERE body_battery_level IS NOT NULL
          UNION ALL
          SELECT epoch_ms(timestamp_gmt)::BIGINT, body_battery_level::FLOAT, 0::INTEGER, 1
            FROM g.body_battery_sample WHERE body_battery_level IS NOT NULL
        ) QUALIFY row_number() OVER (PARTITION BY t ORDER BY pri) = 1
        """,
        ["v", "q"],
    ),
    "resp": (
        """
        SELECT t, v FROM (
          SELECT epoch_ms(timestamp)::BIGINT AS t, respiration_rate::FLOAT AS v, 0 AS pri
            FROM g.fit_respiration_rate WHERE timestamp IS NOT NULL AND respiration_rate > 0
          UNION ALL
          SELECT epoch_ms(timestamp_gmt)::BIGINT, respiration::FLOAT, 1
            FROM g.respiration_sample
            WHERE respiration > 0
              AND calendar_date NOT IN (SELECT DISTINCT calendar_date FROM g.fit_respiration_rate WHERE respiration_rate > 0)
        ) QUALIFY row_number() OVER (PARTITION BY t ORDER BY pri) = 1
        """,
        ["v"],
    ),
    "spo2": (
        """
        SELECT t, v, conf, mode FROM (
          SELECT epoch_ms(timestamp)::BIGINT AS t, reading_spo2::FLOAT AS v, reading_confidence::INTEGER AS conf,
                 CASE mode WHEN 'periodic' THEN 0 WHEN 'continuous' THEN 0 WHEN 'spot_check' THEN 1 ELSE 2 END::INTEGER AS mode
            FROM g.fit_spo2 WHERE timestamp IS NOT NULL AND reading_spo2 BETWEEN 50 AND 100
        ) QUALIFY row_number() OVER (PARTITION BY t ORDER BY mode) = 1
        """,
        ["v", "conf", "mode"],
    ),
    # steps are two per cycle of walking or running; cycles are cumulative from the day's start, across the day's files
    "steps": (
        """
        WITH m AS (
          SELECT calendar_date AS cd, epoch_ms(timestamp)::BIGINT AS t, activity_type, max(cycles) AS cum, max(intensity) AS intensity
            FROM g.fit_monitoring
            WHERE timestamp IS NOT NULL AND cycles IS NOT NULL AND activity_type IN ('walking', 'running')
            GROUP BY ALL),
        d AS (
          SELECT t, intensity, cum - coalesce(lag(cum) OVER (PARTITION BY cd, activity_type ORDER BY t), 0) AS inc FROM m)
        SELECT t, (2 * sum(greatest(inc, 0)))::FLOAT AS v, max(intensity)::INTEGER AS intensity FROM d GROUP BY t
        """,
        ["v", "intensity"],
    ),
    "hrv5": (
        """
        SELECT t, v, day FROM (
          SELECT epoch_ms(timestamp)::BIGINT AS t, value::FLOAT AS v, (calendar_date - DATE '1970-01-01')::INTEGER AS day, 0 AS pri
            FROM g.fit_hrv_value WHERE timestamp IS NOT NULL AND value > 0
          UNION ALL
          SELECT epoch_ms(reading_time_gmt)::BIGINT, hrv_value::FLOAT, (calendar_date - DATE '1970-01-01')::INTEGER, 1
            FROM g.hrv_reading
            WHERE hrv_value > 0 AND calendar_date NOT IN (SELECT DISTINCT calendar_date FROM g.fit_hrv_value WHERE value > 0)
        ) QUALIFY row_number() OVER (PARTITION BY t ORDER BY pri) = 1
        """,
        ["v", "day"],
    ),
    "sleep_move": (
        """
        SELECT epoch_ms(start_gmt)::BIGINT AS t, activity_level::FLOAT AS v FROM g.sleep_movement WHERE start_gmt IS NOT NULL
        QUALIFY row_number() OVER (PARTITION BY start_gmt ORDER BY fetched_at DESC) = 1
        """,
        ["v"],
    ),
    # Garmin's undocumented monitoring message 279: altitude, raw / 5 - 500 metres (FIT altitude encoding)
    "altitude": (
        """
        SELECT t, min(v)::FLOAT AS v FROM (
          SELECT ((try_cast(json_extract_string(fields, '$."253"') AS BIGINT) + 631065600) * 1000)::BIGINT AS t,
                 (try_cast(json_extract_string(fields, '$."0"') AS DOUBLE) / 5 - 500)::FLOAT AS v
            FROM g.fit_message WHERE mesg = '279')
        WHERE v >= -500 AND v <= 9000 AND t IS NOT NULL GROUP BY t
        """,
        ["v"],
    ),
}

# the sleep-score sub-scores come from the sleep FIT files' assessment messages (the API payload has qualifiers only)
DAYS_SQL = """
WITH tr AS (SELECT * FROM g.training_readiness QUALIFY row_number() OVER (PARTITION BY calendar_date ORDER BY timestamp) = 1),
     w AS (SELECT calendar_date, weight / 1000.0 AS weight_kg FROM g.weigh_in
           QUALIFY row_number() OVER (PARTITION BY calendar_date ORDER BY timestamp_gmt DESC) = 1),
     sa AS (SELECT * FROM g.fit_sleep_assessment QUALIFY row_number() OVER (PARTITION BY calendar_date ORDER BY fit_file_id DESC, idx DESC) = 1),
     cal AS (SELECT calendar_date FROM g.daily_summary UNION SELECT calendar_date FROM g.sleep UNION SELECT calendar_date FROM g.hrv)
SELECT
  (cal.calendar_date - DATE '1970-01-01')::INTEGER AS day,
  round((epoch(ds.wellness_start_time_local::TIMESTAMP) - epoch(ds.wellness_start_time_gmt)) / 60)::INTEGER AS utc_offset_min,
  coalesce(s.resting_heart_rate, hd.resting_heart_rate, ds.resting_heart_rate)::FLOAT AS rhr,
  coalesce(ds.last_seven_days_avg_resting_heart_rate, hd.last_seven_days_avg_resting_heart_rate)::FLOAT AS rhr7,
  ds.min_heart_rate::FLOAT AS hr_min, ds.max_heart_rate::FLOAT AS hr_max,
  ds.total_steps::INTEGER AS steps, ds.moderate_intensity_minutes::INTEGER AS mod_min, ds.vigorous_intensity_minutes::INTEGER AS vig_min,
  ds.average_stress_level::FLOAT AS stress_avg, ds.max_stress_level::FLOAT AS stress_max,
  ds.rest_stress_duration::INTEGER AS rest_s, ds.low_stress_duration::INTEGER AS low_s, ds.medium_stress_duration::INTEGER AS med_s,
  ds.high_stress_duration::INTEGER AS high_s, ds.activity_stress_duration::INTEGER AS activity_s,
  ds.uncategorized_stress_duration::INTEGER AS uncat_s,
  ds.body_battery_charged_value::FLOAT AS bb_charged, ds.body_battery_drained_value::FLOAT AS bb_drained,
  ds.body_battery_highest_value::FLOAT AS bb_high, ds.body_battery_lowest_value::FLOAT AS bb_low,
  ds.body_battery_at_wake_time::FLOAT AS bb_wake,
  ds.average_spo2::FLOAT AS spo2_avg, ds.lowest_spo2::FLOAT AS spo2_low,
  ds.avg_waking_respiration_value::FLOAT AS resp_awake_avg, ds.lowest_respiration_value::FLOAT AS resp_low,
  ds.highest_respiration_value::FLOAT AS resp_high, ds.average_monitoring_environment_altitude::FLOAT AS altitude_avg,
  s.sleep_score::FLOAT AS sleep_score,
  sa.sleep_quality_score::FLOAT AS sleep_quality_score, sa.sleep_duration_score::FLOAT AS sleep_duration_score,
  sa.sleep_recovery_score::FLOAT AS sleep_recovery_score, sa.deep_sleep_score::FLOAT AS sleep_deep_score,
  sa.rem_sleep_score::FLOAT AS sleep_rem_score, sa.light_sleep_score::FLOAT AS sleep_light_score,
  sa.awakenings_count_score::FLOAT AS sleep_awakenings_score, sa.awake_time_score::FLOAT AS sleep_awake_time_score,
  sa.sleep_restlessness_score::FLOAT AS sleep_restfulness_score, sa.interruptions_score::FLOAT AS sleep_interruptions_score,
  s.sleep_score_feedback::VARCHAR AS sleep_feedback, s.sleep_need_actual::FLOAT AS sleep_need_min,
  ss.skin_temp_c::FLOAT AS skin_temp_dev,
  h.last_night_avg::FLOAT AS hrv_night, h.weekly_avg::FLOAT AS hrv_week, h.last_night_5min_high::FLOAT AS hrv_5min_high,
  h.baseline_balanced_low::FLOAT AS hrv_bal_lo, h.baseline_balanced_upper::FLOAT AS hrv_bal_hi,
  h.baseline_low_upper::FLOAT AS hrv_low_upper, h.status::VARCHAR AS hrv_status,
  tr.score::FLOAT AS tr_score, tr.level::VARCHAR AS tr_level,
  tr.sleep_score_factor_percent::FLOAT AS tr_sleep_pct, tr.recovery_time_factor_percent::FLOAT AS tr_recovery_pct,
  tr.acwr_factor_percent::FLOAT AS tr_acwr_pct, tr.stress_history_factor_percent::FLOAT AS tr_stress_hist_pct,
  tr.hrv_factor_percent::FLOAT AS tr_hrv_pct, tr.sleep_history_factor_percent::FLOAT AS tr_sleep_hist_pct,
  tr.acute_load::FLOAT AS tr_acute_load, tr.recovery_time::FLOAT AS tr_recovery_min,
  NULL::FLOAT AS hs_hrv, NULL::FLOAT AS hs_hrv_lo, NULL::FLOAT AS hs_hrv_hi,
  NULL::FLOAT AS hs_hr, NULL::FLOAT AS hs_hr_lo, NULL::FLOAT AS hs_hr_hi,
  NULL::FLOAT AS hs_resp, NULL::FLOAT AS hs_resp_lo, NULL::FLOAT AS hs_resp_hi,
  NULL::FLOAT AS hs_spo2, NULL::FLOAT AS hs_spo2_lo, NULL::FLOAT AS hs_spo2_hi,
  NULL::FLOAT AS hs_skin, NULL::FLOAT AS hs_skin_lo, NULL::FLOAT AS hs_skin_hi,
  mm.vo2max_precise::FLOAT AS vo2max, mm.heat_acclimation_percentage::FLOAT AS heat_acclim_pct,
  mm.altitude_acclimation::FLOAT AS altitude_acclim_m, w.weight_kg::FLOAT AS weight_kg
FROM cal
LEFT JOIN g.daily_summary ds USING (calendar_date)
LEFT JOIN g.sleep s USING (calendar_date)
LEFT JOIN g.sleep_stats ss USING (calendar_date)
LEFT JOIN g.hrv h USING (calendar_date)
LEFT JOIN g.heart_rate_day hd USING (calendar_date)
LEFT JOIN g.max_metrics mm USING (calendar_date)
LEFT JOIN tr USING (calendar_date)
LEFT JOIN w USING (calendar_date)
LEFT JOIN sa USING (calendar_date)
WHERE cal.calendar_date IS NOT NULL
ORDER BY day
"""

SLEEPS_SQL = """
SELECT * FROM (
  SELECT 'main:' || epoch_ms(sleep_start_timestamp_gmt)::BIGINT AS id, (calendar_date - DATE '1970-01-01')::INTEGER AS day,
         0::INTEGER AS kind, epoch_ms(sleep_start_timestamp_gmt)::BIGINT AS "start", epoch_ms(sleep_end_timestamp_gmt)::BIGINT AS "end",
         sleep_window_confirmation_type::VARCHAR AS confirmation, sleep_time_seconds::INTEGER AS sleep_s,
         deep_sleep_seconds::INTEGER AS deep_s, light_sleep_seconds::INTEGER AS light_s, rem_sleep_seconds::INTEGER AS rem_s,
         awake_sleep_seconds::INTEGER AS awake_s, unmeasurable_sleep_seconds::INTEGER AS unmeasurable_s,
         awake_count::INTEGER AS awake_count, restless_moments_count::INTEGER AS restless_count,
         avg_sleep_stress::FLOAT AS avg_stress, avg_heart_rate::FLOAT AS avg_hr, resting_heart_rate::FLOAT AS rhr,
         average_respiration_value::FLOAT AS resp_avg, lowest_respiration_value::FLOAT AS resp_low,
         highest_respiration_value::FLOAT AS resp_high, average_spo2_value::FLOAT AS spo2_avg, lowest_spo2_value::FLOAT AS spo2_low,
         breathing_disruption_severity::VARCHAR AS breathing_disruption
    FROM g.sleep WHERE sleep_start_timestamp_gmt IS NOT NULL AND sleep_end_timestamp_gmt IS NOT NULL
  UNION ALL
  SELECT 'nap:' || epoch_ms(nap_start_timestamp_gmt)::BIGINT, (calendar_date - DATE '1970-01-01')::INTEGER, 1::INTEGER,
         epoch_ms(nap_start_timestamp_gmt)::BIGINT, epoch_ms(nap_end_timestamp_gmt)::BIGINT, NULL::VARCHAR, nap_time_sec::INTEGER,
         NULL::INTEGER, NULL::INTEGER, NULL::INTEGER, NULL::INTEGER, NULL::INTEGER, NULL::INTEGER, NULL::INTEGER,
         NULL::FLOAT, NULL::FLOAT, NULL::FLOAT, NULL::FLOAT, NULL::FLOAT, NULL::FLOAT, NULL::FLOAT, NULL::FLOAT, NULL::VARCHAR
    FROM g.nap WHERE nap_start_timestamp_gmt IS NOT NULL AND nap_end_timestamp_gmt IS NOT NULL
) ORDER BY "start"
"""

# stage segments: each FIT stage change lasts until the next one in its file; the last one until the end of the main
# sleep containing it (else a minute). Days without FIT stage files use the API segments.
STAGES_SQL = """
WITH fit AS (
  SELECT fit_file_id, epoch_ms(timestamp)::BIGINT AS s, 
         CASE sleep_level WHEN 'deep' THEN 0 WHEN 'light' THEN 1 WHEN 'rem' THEN 2 WHEN 'awake' THEN 3 ELSE 4 END::INTEGER AS stage,
         calendar_date
    FROM g.fit_sleep_level WHERE timestamp IS NOT NULL),
fit_e AS (
  SELECT s AS "start", stage, calendar_date,
         coalesce(lead(s) OVER (PARTITION BY fit_file_id ORDER BY s), -1) AS nxt
    FROM fit),
main AS (
  SELECT epoch_ms(sleep_start_timestamp_gmt)::BIGINT AS s, epoch_ms(sleep_end_timestamp_gmt)::BIGINT AS e,
         (calendar_date - DATE '1970-01-01')::INTEGER AS day
    FROM g.sleep WHERE sleep_start_timestamp_gmt IS NOT NULL AND sleep_end_timestamp_gmt IS NOT NULL),
fit_r AS (
  SELECT f."start",
         CASE WHEN f.nxt >= 0 THEN f.nxt ELSE coalesce((SELECT min(m.e) FROM main m WHERE f."start" >= m.s AND f."start" < m.e), f."start" + 60000) END AS "end",
         f.stage, f.calendar_date
    FROM fit_e f),
api AS (
  SELECT epoch_ms(start_gmt)::BIGINT AS "start", epoch_ms(end_gmt)::BIGINT AS "end",
         CASE activity_level::INTEGER WHEN 0 THEN 0 WHEN 1 THEN 1 WHEN 2 THEN 2 WHEN 3 THEN 3 ELSE 4 END::INTEGER AS stage, calendar_date
    FROM g.sleep_level
   WHERE calendar_date NOT IN (SELECT DISTINCT calendar_date FROM g.fit_sleep_level) AND start_gmt IS NOT NULL AND end_gmt IS NOT NULL),
u AS (SELECT * FROM fit_r UNION ALL SELECT * FROM api)
SELECT u."start"::BIGINT AS "start", u."end"::BIGINT AS "end", u.stage::INTEGER AS stage,
       (SELECT min(m.day) FROM main m WHERE u."start" >= m.s AND u."start" < m.e)::INTEGER AS day
  FROM u WHERE u."end" > u."start" ORDER BY "start"
"""

ACTIVITIES_SQL = """
SELECT 'garmin:' || a.activity_id AS id, epoch_ms(a.start_time_gmt)::BIGINT AS "start",
       (epoch_ms(a.start_time_gmt) + (coalesce(a.elapsed_duration, a.duration, 0) * 1000)::BIGINT)::BIGINT AS "end",
       coalesce(round((epoch(a.start_time_local::TIMESTAMP) - epoch(a.start_time_gmt)) / 60), 0)::INTEGER AS utc_offset_min,
       CASE a.activity_type {sports} ELSE 'other' END::VARCHAR AS sport, a.activity_type::VARCHAR AS source_sport,
       a.activity_name::VARCHAR AS name, a.activity_training_load::FLOAT AS load, a.average_hr::FLOAT AS avg_hr,
       a.max_hr::FLOAT AS max_hr, coalesce(h.n, 0)::INTEGER AS rr_beats
  FROM g.activity a
  LEFT JOIN (SELECT activity_id, count(*) AS n FROM g.fit_hrv GROUP BY 1) h USING (activity_id)
 WHERE a.deleted_at IS NULL AND a.start_time_gmt IS NOT NULL
 ORDER BY "start"
"""

TAGS_SQL = """
SELECT (l.calendar_date - DATE '1970-01-01')::INTEGER AS day, NULL::BIGINT AS t,
       json_extract_string(e.value, '$.name')::VARCHAR AS name,
       coalesce(json_extract_string(e.value, '$.status'), json_extract_string(e.value, '$.measurementValue'), 'YES')::VARCHAR AS value,
       'garmin_lifestyle'::VARCHAR AS source
  FROM g.lifestyle_logging l, json_each(l.daily_logs_report) e
 WHERE json_type(l.daily_logs_report) = 'ARRAY' AND json_extract_string(e.value, '$.name') IS NOT NULL
 ORDER BY day, name
"""

DEVICES_SQL = """
SELECT (min(key::DATE) - DATE '1970-01-01')::INTEGER AS first_day, (max(key::DATE) - DATE '1970-01-01')::INTEGER AS last_day,
       garmin_product::VARCHAR AS product, serial_number::VARCHAR AS serial
  FROM g.fit_file WHERE kind = 'wellness_fit' AND (garmin_product IS NOT NULL OR serial_number IS NOT NULL)
 GROUP BY garmin_product, serial_number ORDER BY first_day
"""

TABLES = {"days": DAYS_SQL, "sleeps": SLEEPS_SQL, "stages": STAGES_SQL, "activities": ACTIVITIES_SQL, "tags": TAGS_SQL, "devices": DEVICES_SQL}


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sports_case() -> str:
    return " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in SPORTS.items())


class WellnessExporter:
    """Writes the wellness export from a snapshot file. `run` is idempotent and cheap when nothing changed."""

    def __init__(self, out_dir: Path, *, importer: str, memory_limit: str = "256MB") -> None:
        self.out_dir = Path(out_dir)
        self.importer = importer
        self.memory_limit = memory_limit

    @property
    def manifest_path(self) -> Path:
        return self.out_dir / "manifest.json"

    def _previous(self) -> dict[str, dict[str, Any]]:
        if not self.manifest_path.exists():
            return {}
        try:
            return {f["path"]: f for f in json.loads(self.manifest_path.read_text()).get("files", [])}
        except (OSError, ValueError):
            return {}

    def run(self, snapshot: Path) -> dict[str, Any]:
        t_start = time.time()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        stage = self.out_dir.parent / "wellness-stage"
        tmp = self.out_dir.parent / "wellness-tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        previous = self._previous()
        files: list[dict[str, Any]] = []
        written = 0
        now = _iso(datetime.now(UTC))
        con = duckdb.connect()
        try:
            con.execute(f"SET memory_limit = '{self.memory_limit}'")
            con.execute("SET threads = 1")
            con.execute("SET TimeZone = 'UTC'")
            con.execute(f"SET temp_directory = '{tmp}'")
            con.execute("SET preserve_insertion_order = true")
            con.execute(f"ATTACH '{snapshot}' AS g (READ_ONLY)")

            # --- tables: rewritten only when their bytes changed
            for name, sql in TABLES.items():
                sql = sql.replace("{sports}", _sports_case())
                part = stage / f"{name}.parquet"
                con.execute(f"COPY ({sql}) TO '{part}' (FORMAT parquet, COMPRESSION snappy, ROW_GROUP_SIZE 1000000)")
                rows = con.execute(f"SELECT count(*) FROM read_parquet('{part}')").fetchone()[0]
                digest = _sha256(part)
                rel = f"{name}.parquet"
                dest = self.out_dir / rel
                old = previous.get(rel)
                if old is None or old.get("sha256") != digest or not dest.exists():
                    os.replace(part, dest)
                    written += 1
                    updated = now
                else:
                    updated = old.get("updated_at", now)
                files.append({"path": rel, "table": name, "rows": int(rows), "sha256": digest, "fingerprint": f"{rows}|{digest[:16]}", "updated_at": updated})

            # --- series: one file per UTC month, rewritten only when the month's fingerprint changed
            for name, (sql, cols) in SERIES.items():
                try:
                    con.execute(f"CREATE OR REPLACE TEMP TABLE s_{name} AS SELECT strftime(epoch_ms(t), '%Y-%m') AS month, * FROM ({sql}) ORDER BY t")
                except duckdb.Error as err:
                    raise RuntimeError(f"wellness export: series {name} failed: {err}") from err
                agg = con.execute(
                    f"SELECT month, count(*), min(t), max(t), coalesce(sum(v), 0) FROM s_{name} GROUP BY month ORDER BY month"
                ).fetchall()
                changed: list[str] = []
                months = []
                for month, n, tmin, tmax, vsum in agg:
                    rel = f"series/{name}/{month}.parquet"
                    fp = f"{n}|{tmin}|{tmax}|{float(vsum)!r}"
                    old = previous.get(rel)
                    months.append((month, rel, int(n), int(tmin), int(tmax), fp, old))
                    if old is None or old.get("fingerprint") != fp or not (self.out_dir / rel).exists():
                        changed.append(month)
                if changed:
                    sdir = stage / f"s_{name}"
                    in_list = ", ".join(f"'{m}'" for m in changed)
                    con.execute(
                        f"COPY (SELECT * FROM s_{name} WHERE month IN ({in_list}) ORDER BY t) TO '{sdir}' "
                        "(FORMAT parquet, COMPRESSION snappy, PARTITION_BY (month), OVERWRITE_OR_IGNORE)"
                    )
                for month, rel, n, tmin, tmax, fp, old in months:
                    dest = self.out_dir / rel
                    if month in changed:
                        parts = sorted((stage / f"s_{name}" / f"month={month}").glob("*.parquet"))
                        if len(parts) != 1:
                            raise RuntimeError(f"wellness export: expected one parquet file for {rel}, found {len(parts)}")
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(parts[0], dest)
                        digest = _sha256(dest)
                        written += 1
                        updated = now
                    else:
                        digest = old["sha256"]  # type: ignore[index]
                        updated = old.get("updated_at", now)  # type: ignore[union-attr]
                    files.append({"path": rel, "table": "series", "name": name, "month": month, "rows": n, "t_min": tmin, "t_max": tmax,
                                  "sha256": digest, "fingerprint": fp, "updated_at": updated})
                con.execute(f"DROP TABLE s_{name}")
                shutil.rmtree(stage / f"s_{name}", ignore_errors=True)

            # --- beat-to-beat intervals of activities recorded with Log HRV
            beats = con.execute("SELECT activity_id, count(*) FROM g.fit_hrv GROUP BY 1 HAVING count(*) >= 500 ORDER BY 1").fetchall()
            for aid, n in beats:
                rel = f"rr/garmin-{int(aid)}.parquet"
                fp = str(int(n))
                old = previous.get(rel)
                dest = self.out_dir / rel
                if old is None or old.get("fingerprint") != fp or not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    part = stage / f"rr-{int(aid)}.parquet"
                    con.execute(f"COPY (SELECT (rr_interval * 1000)::FLOAT AS rr FROM g.fit_hrv WHERE activity_id = {int(aid)} ORDER BY idx, pos) TO '{part}' (FORMAT parquet, COMPRESSION snappy)")
                    os.replace(part, dest)
                    digest = _sha256(dest)
                    written += 1
                    updated = now
                else:
                    digest = old["sha256"]
                    updated = old.get("updated_at", now)
                files.append({"path": rel, "table": "rr", "activity": str(int(aid)), "rows": int(n), "sha256": digest, "fingerprint": fp, "updated_at": updated})
        finally:
            con.close()
            shutil.rmtree(stage, ignore_errors=True)

        manifest = {"schema_version": SCHEMA_VERSION, "importer": self.importer, "generated_at": now, "source": {"kind": "garmin-mcp"}, "files": files}
        tmp_manifest = self.manifest_path.with_suffix(".json.tmp")
        tmp_manifest.write_text(json.dumps(manifest, indent=1))
        os.replace(tmp_manifest, self.manifest_path)
        keep = {f["path"] for f in files} | {"manifest.json"}
        removed = 0
        for f in list(self.out_dir.rglob("*.parquet")):
            if f.relative_to(self.out_dir).as_posix() not in keep:
                f.unlink()
                removed += 1
        stats = {"files": len(files), "written": written, "removed": removed, "seconds": round(time.time() - t_start, 1)}
        log.info("wellness export: %d files, %d written, %d removed in %.1f s", stats["files"], stats["written"], stats["removed"], stats["seconds"])
        return stats

    def serve(self, rel: str, request: Request) -> Response:
        """The manifest or one data file; 404 for anything else."""
        if not _SERVABLE.fullmatch(rel):
            return Response("Not found", status_code=404, media_type="text/plain")
        path = self.out_dir / rel
        if not path.is_file():
            return Response("Not found", status_code=404, media_type="text/plain")
        media = "application/json" if rel == "manifest.json" else "application/octet-stream"
        return FileResponse(str(path), media_type=media)

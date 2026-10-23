"""Projections: raw payloads -> typed tables.

Each raw object type has one projection that (re)builds the typed rows derived from
the current payloads of a set of keys, so landing a payload and rebuilding the whole
mirror from raw_payload run the same SQL. Projections run inside the caller's
transaction on the writer connection; the keys in scope sit in the temp table _scope.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .database import Tx
from .tabledef import Column, Table, consumed_keys, insert_sql, upsert_sql
from .tables import table

SCOPE = "(SELECT object_key FROM _scope)"


def scoped_raw(type_: str) -> str:
    """raw_current restricted to the scoped keys of one type, with the scope on both sides of the join.

    Not the raw_current view: its scope filter reaches only raw_latest, so the join would
    build a hash table over every stored body of the type (activity details are ~1 MB each).
    """
    return (
        "(SELECT l.object_type, l.object_key, l.status, l.checked_at, p.fetched_at, p.body"
        f" FROM (SELECT * FROM raw_latest WHERE object_type = '{type_}' AND object_key IN {SCOPE}) l"
        f" LEFT JOIN (SELECT * FROM raw_payload WHERE object_type = '{type_}' AND object_key IN {SCOPE}) p"
        " USING (object_type, object_key, content_hash))"
    )


def current(type_: str, alias: str = "r") -> str:
    return f"(SELECT * FROM {scoped_raw(type_)} WHERE body IS NOT NULL) {alias}"


def insert(t: Table, source: str, only: Callable[[Column], bool] | None = None, conflict: str = "OR REPLACE") -> str:
    return insert_sql(t, source, only, conflict)


def upsert(t: Table, source: str, only: Callable[[Column], bool]) -> str:
    return upsert_sql(t, source, only)


def children(type_: str, array_expr: str, extra: str = "") -> str:
    """Child rows unnested from an array of each payload, with their 0-based idx."""
    ex = f", {extra}" if extra else ""
    return (
        f"(SELECT r.object_key, r.fetched_at{ex}, unnest(range(json_array_length({array_expr})::BIGINT)) AS idx, "
        f"unnest(CAST({array_expr} AS JSON[])) AS b FROM {current(type_)}) src"
    )


def day_children(type_: str, array_expr: str, extra: str = "") -> str:
    return children(type_, array_expr, f"r.object_key::DATE AS calendar_date{', ' + extra if extra else ''}")


def day_root(type_: str) -> str:
    return f"(SELECT r.object_key::DATE AS calendar_date, r.fetched_at, r.body AS b FROM {current(type_)}) src"


def act_root(type_: str) -> str:
    return f"(SELECT r.object_key::BIGINT AS activity_id, r.fetched_at, r.body AS b FROM {current(type_)}) src"


def act_children(type_: str, array_expr: str, extra: str = "") -> str:
    return children(type_, array_expr, f"r.object_key::BIGINT AS activity_id{', ' + extra if extra else ''}")


def value_rows(type_: str, array_expr: str) -> str:
    """Rows of a Garmin values array ([[ts, v, ...], ...]) as VARCHAR[] in s.v."""
    return (
        f"(SELECT r.object_key::DATE AS calendar_date, r.fetched_at, TRY_CAST(unnest(CAST({array_expr} AS JSON[])) AS VARCHAR[]) AS v "
        f"FROM {current(type_)}) src"
    )


def delete_scope(t: str, column: str, cast: str = "") -> str:
    return f"DELETE FROM {t} WHERE {column} IN (SELECT object_key{cast} FROM _scope)"


@dataclass
class Shape:
    """For the drift check: SQL yielding JSON objects (column o) and the keys the projection understands (None = all)."""

    objects: str
    known: set[str] | None


@dataclass
class Projection:
    type: str
    statements: list[str]
    shapes: list[Shape] = field(default_factory=list)


def objects(type_: str, path: str | None = None) -> str:
    inner = f"SELECT body FROM raw_current WHERE object_type = '{type_}' AND body IS NOT NULL"
    if not path:
        return f"SELECT body AS o FROM ({inner}) WHERE json_type(body) = 'OBJECT'"
    if path == "$[*]":
        return f"SELECT unnest(CAST(body AS JSON[])) AS o FROM ({inner}) WHERE json_type(body) = 'ARRAY'"
    return f"SELECT unnest(CAST(json_extract(body, '{path}') AS JSON[])) AS o FROM ({inner}) WHERE json_type(json_extract(body, '{path}')) = 'ARRAY'"


def known(*tables: Table, extra: Sequence[str] = ()) -> set[str] | None:
    keys: set[str] = set(extra)
    for t in tables:
        for c in t.columns:
            if c.expr == "b":
                return None
        keys |= consumed_keys(t)
    return keys


T = table

PROJECTIONS: list[Projection] = []


def add(type_: str, statements: list[str], shapes: list[Shape] | None = None) -> None:
    PROJECTIONS.append(Projection(type_, statements, shapes or []))


# ---------------------------------------------------------------- activities

activity = T("activity")
_summary_cols = {c.name for c in activity.columns if c.key is not None} | {"summary_fetched_at", "deleted_at", "activity_id"}

ACTIVITY_SOURCE = f"""(
  SELECT
    coalesce(s.object_key, d.object_key) AS object_key,
    s.body AS b, d.body AS d,
    s.checked_at AS summary_fetched_at, d.checked_at AS detail_fetched_at,
    coalesce(s.fetched_at, d.fetched_at) AS fetched_at,
    CASE WHEN s.status = 'not_found' OR d.status = 'not_found'
      THEN greatest(CASE WHEN s.status = 'not_found' THEN s.checked_at END, CASE WHEN d.status = 'not_found' THEN d.checked_at END) END AS deleted_at
  FROM (SELECT l.*, coalesce(l.body, (SELECT p.body FROM raw_payload p WHERE p.object_type = 'activity_summary' AND p.object_key = l.object_key ORDER BY p.fetched_at DESC LIMIT 1)) AS last_body
        FROM {scoped_raw('activity_summary')} l) s
  FULL JOIN {scoped_raw('activity')} d USING (object_key)
) src WHERE b IS NOT NULL"""

# When only the detail exists (no list element yet), its summaryDTO feeds the summary columns.
_activity_insert = insert(activity, ACTIVITY_SOURCE).replace("FROM (\n  SELECT", "FROM (\n  SELECT", 1)
_activity_insert = _activity_insert.replace(
    "    s.body AS b, d.body AS d,",
    "    coalesce(s.body, CASE WHEN d.body IS NULL THEN NULL ELSE json_merge_patch(json_merge_patch(d.body, d.body->'summaryDTO'), "
    "json_object('activityType', d.body->'activityTypeDTO', 'eventType', d.body->'eventTypeDTO', 'activityUUID', d.body->'activityUUID'->>'uuid')) END, s.last_body) AS b, d.body AS d,",
)

add(
    "activity_summary",
    [_activity_insert],
    [Shape(objects("activity_summary"), known(activity))],
)
add(
    "activity",
    [_activity_insert],
    [
        Shape(
            objects("activity"),
            {
                "activityId", "activityUUID", "activityName", "userProfileId", "isMultiSportParent", "activityTypeDTO",
                "eventTypeDTO", "accessControlRuleDTO", "timeZoneUnitDTO", "metadataDTO", "summaryDTO", "locationName",
                "splitSummaries", "connectIQMeasurements", "description",
            },
        ),
        Shape(f"SELECT json_extract(o, '$.summaryDTO') AS o FROM ({objects('activity')})", known(activity)),
    ],
)

lap = T("activity_lap")
lap_event = T("activity_lap_event")
add(
    "activity_splits",
    [
        delete_scope("activity_lap", "activity_id", "::BIGINT"),
        insert(lap, act_children("activity_splits", "r.body->'lapDTOs'")),
        delete_scope("activity_lap_event", "activity_id", "::BIGINT"),
        insert(lap_event, act_children("activity_splits", "r.body->'eventDTOs'")),
    ],
    [
        Shape(objects("activity_splits"), {"activityId", "lapDTOs", "eventDTOs"}),
        Shape(objects("activity_splits", "$.lapDTOs"), known(lap)),
        Shape(objects("activity_splits", "$.eventDTOs"), known(lap_event)),
    ],
)

typed_split = T("activity_typed_split")
add(
    "activity_typed_splits",
    [delete_scope("activity_typed_split", "activity_id", "::BIGINT"), insert(typed_split, act_children("activity_typed_splits", "r.body->'splits'"))],
    [Shape(objects("activity_typed_splits"), {"activityId", "activityUUID", "splits"}), Shape(objects("activity_typed_splits", "$.splits"), known(typed_split))],
)

split_summary = T("activity_split_summary")
add(
    "activity_split_summaries",
    [delete_scope("activity_split_summary", "activity_id", "::BIGINT"), insert(split_summary, act_children("activity_split_summaries", "r.body->'splitSummaries'"))],
    [Shape(objects("activity_split_summaries"), {"activityId", "activityUUID", "splitSummaries"}), Shape(objects("activity_split_summaries", "$.splitSummaries"), known(split_summary))],
)

weather = T("activity_weather")
add("activity_weather", [insert(weather, act_root("activity_weather"))], [Shape(objects("activity_weather"), known(weather))])

tiz = T("activity_time_in_zone")
for _kind, _type in (("hr", "activity_hr_zones"), ("power", "activity_power_zones")):
    add(
        _type,
        [
            f"DELETE FROM activity_time_in_zone WHERE kind = '{_kind}' AND activity_id IN (SELECT object_key::BIGINT FROM _scope)",
            insert(tiz, act_children(_type, "r.body", f"'{_kind}' AS kind")),
        ],
        [Shape(objects(_type, "$[*]"), known(tiz))],
    )

agear = T("activity_gear")
add(
    "activity_gear",
    [delete_scope("activity_gear", "activity_id", "::BIGINT"), insert(agear, act_children("activity_gear", "r.body"))],
    [Shape(objects("activity_gear", "$[*]"), None)],
)

exset = T("activity_exercise_set")
add(
    "activity_exercise_sets",
    [delete_scope("activity_exercise_set", "activity_id", "::BIGINT"), insert(exset, act_children("activity_exercise_sets", "r.body->'exerciseSets'"))],
    [Shape(objects("activity_exercise_sets"), {"activityId", "exerciseSets"}), Shape(objects("activity_exercise_sets", "$.exerciseSets"), None)],
)

poly = T("activity_polyline")
add("activity_polyline", [insert(poly, act_root("activity_polyline"))], [Shape(objects("activity_polyline"), known(poly))])

dmetric = T("activity_detail_metric")
add(
    "activity_details",
    [
        delete_scope("activity_detail_metric", "activity_id", "::BIGINT"),
        insert(
            dmetric,
            act_children(
                "activity_details",
                "r.body->'metricDescriptors'",
                "TRY_CAST(r.body->>'measurementCount' AS INTEGER) AS measurement_count, TRY_CAST(r.body->>'detailsAvailable' AS BOOLEAN) AS details_available",
            ),
        ),
    ],
    [
        Shape(
            objects("activity_details"),
            {"activityId", "measurementCount", "metricsCount", "totalMetricsCount", "metricDescriptors", "activityDetailMetrics", "geoPolylineDTO", "heartRateDTOs", "pendingData", "detailsAvailable"},
        ),
        Shape(objects("activity_details", "$.metricDescriptors"), known(dmetric)),
    ],
)

# ---------------------------------------------------------------- daily wellness

ds = T("daily_summary")
add("user_summary", [insert(ds, day_root("user_summary"))], [Shape(objects("user_summary"), known(ds))])

ds2 = T("daily_summary_v2")
add("daily_summary_v2", [insert(ds2, day_root("daily_summary_v2"))], [Shape(objects("daily_summary_v2"), known(ds2))])

si = T("steps_interval")
add(
    "steps_chart",
    [delete_scope("steps_interval", "calendar_date", "::DATE"), insert(si, day_children("steps_chart", "r.body"))],
    [Shape(objects("steps_chart", "$[*]"), known(si))],
)

hrd, hrs = T("heart_rate_day"), T("heart_rate_sample")
add(
    "heart_rates",
    [
        insert(hrd, day_root("heart_rates")),
        delete_scope("heart_rate_sample", "calendar_date", "::DATE"),
        insert(hrs, value_rows("heart_rates", "r.body->'heartRateValues'")),
    ],
    [Shape(objects("heart_rates"), known(hrd))],
)

sd, ss = T("stress_day"), T("stress_sample")
STRESS_SAMPLES = f"""(
  SELECT calendar_date, fetched_at, ts, max(stress) AS stress, max(bb_status) AS bb_status, max(bb_level) AS bb_level, max(bb_version) AS bb_version
  FROM (
    SELECT r.object_key::DATE AS calendar_date, r.fetched_at, v[1] AS ts, TRY_CAST(v[2] AS INTEGER) AS stress,
           NULL::VARCHAR AS bb_status, NULL::INTEGER AS bb_level, NULL::DOUBLE AS bb_version
    FROM (SELECT object_key, fetched_at, TRY_CAST(unnest(CAST(body->'stressValuesArray' AS JSON[])) AS VARCHAR[]) AS v FROM {current('stress')}) r
    UNION ALL
    SELECT r.object_key::DATE, r.fetched_at, v[1], NULL, v[2], TRY_CAST(v[3] AS INTEGER), TRY_CAST(v[4] AS DOUBLE)
    FROM (SELECT object_key, fetched_at, TRY_CAST(unnest(CAST(body->'bodyBatteryValuesArray' AS JSON[])) AS VARCHAR[]) AS v FROM {current('stress')}) r
  ) GROUP BY ALL
) src"""
add(
    "stress",
    [insert(sd, day_root("stress")), delete_scope("stress_sample", "calendar_date", "::DATE"), insert(ss, STRESS_SAMPLES)],
    [Shape(objects("stress"), known(sd, extra=["bodyBatteryValuesArray"]))],
)

bbd, bbs = T("body_battery_day"), T("body_battery_sample")
add(
    "body_battery",
    [
        insert(bbd, day_root("body_battery")),
        delete_scope("body_battery_sample", "calendar_date", "::DATE"),
        insert(bbs, value_rows("body_battery", "r.body->'bodyBatteryValuesArray'")),
    ],
    [Shape(objects("body_battery"), known(bbd))],
)

bbe = T("body_battery_event")
add(
    "body_battery_events",
    [delete_scope("body_battery_event", "calendar_date", "::DATE"), insert(bbe, day_children("body_battery_events", "r.body"))],
    [Shape(objects("body_battery_events", "$[*]"), known(bbe))],
)

rd, rs = T("respiration_day"), T("respiration_sample")
add(
    "respiration",
    [
        insert(rd, day_root("respiration")),
        delete_scope("respiration_sample", "calendar_date", "::DATE"),
        insert(rs, value_rows("respiration", "r.body->'respirationValuesArray'")),
    ],
    [Shape(objects("respiration"), known(rd))],
)

spo2 = T("spo2_day")
add("spo2", [insert(spo2, day_root("spo2"))], [Shape(objects("spo2"), known(spo2))])

fd, fi = T("floors_day"), T("floors_interval")
add(
    "floors",
    [insert(fd, day_root("floors")), delete_scope("floors_interval", "calendar_date", "::DATE"), insert(fi, value_rows("floors", "r.body->'floorValuesArray'"))],
    [Shape(objects("floors"), known(fd))],
)

im = T("intensity_minutes_day")
add("intensity_minutes", [insert(im, day_root("intensity_minutes"))], [Shape(objects("intensity_minutes"), known(im))])

sleep, slevel, smove, ssample = T("sleep"), T("sleep_level"), T("sleep_movement"), T("sleep_sample")
SLEEP_ROOT = f"(SELECT r.object_key::DATE AS calendar_date, r.fetched_at, r.body AS root, r.body->'dailySleepDTO' AS b FROM {current('sleep')} WHERE (r.body->'dailySleepDTO') IS NOT NULL) src"
SLEEP_SERIES = [
    ("heart_rate", "sleepHeartRate"),
    ("stress", "sleepStress"),
    ("body_battery", "sleepBodyBattery"),
    ("hrv", "hrvData"),
    ("respiration", "wellnessEpochRespirationDataDTOList"),
    ("restless", "sleepRestlessMoments"),
]
SLEEP_SAMPLES = " UNION ALL ".join(
    f"SELECT r.object_key::DATE AS calendar_date, r.fetched_at, '{name}' AS series, unnest(CAST(r.body->'{key}' AS JSON[])) AS b FROM {current('sleep')}"
    for name, key in SLEEP_SERIES
)
add(
    "sleep",
    [
        insert(sleep, SLEEP_ROOT),
        delete_scope("sleep_level", "calendar_date", "::DATE"),
        insert(slevel, day_children("sleep", "r.body->'sleepLevels'"), conflict="OR IGNORE"),
        delete_scope("sleep_movement", "calendar_date", "::DATE"),
        insert(smove, day_children("sleep", "r.body->'sleepMovement'"), conflict="OR IGNORE"),
        delete_scope("sleep_sample", "calendar_date", "::DATE"),
        insert(ssample, f"({SLEEP_SAMPLES}) src", conflict="OR IGNORE"),
    ],
    [
        Shape(
            objects("sleep"),
            {
                "dailySleepDTO", "sleepMovement", "remSleepData", "sleepLevels", "sleepRestlessMoments", "restlessMomentsCount",
                "wellnessEpochRespirationDataDTOList", "wellnessEpochRespirationAveragesList", "respirationVersion",
                "sleepHeartRate", "sleepStress", "sleepBodyBattery", "skinTempDataExists", "hrvData", "breathingDisruptionData",
                "avgOvernightHrv", "hrvStatus", "bodyBatteryChange", "restingHeartRate", "wellnessSpO2SleepSummaryDTO",
            },
        ),
        Shape(f"SELECT json_extract(o, '$.dailySleepDTO') AS o FROM ({objects('sleep')})", known(sleep)),
        Shape(objects("sleep", "$.sleepLevels"), known(slevel)),
    ],
)

hrv, hrvr = T("hrv"), T("hrv_reading")
HRV_SOURCE = f"""(
  SELECT coalesce(d.object_key, s.object_key)::DATE AS calendar_date, coalesce(d.fetched_at, s.fetched_at) AS fetched_at,
         CASE WHEN d.body IS NOT NULL THEN d.body ELSE json_object('hrvSummary', s.body) END AS b
  FROM (SELECT * FROM {scoped_raw('hrv')} WHERE body IS NOT NULL) d
  FULL JOIN (SELECT * FROM {scoped_raw('hrv_summary')} WHERE body IS NOT NULL) s USING (object_key)
) src"""
for _t in ("hrv", "hrv_summary"):
    add(
        _t,
        [
            insert(hrv, HRV_SOURCE),
            delete_scope("hrv_reading", "calendar_date", "::DATE"),
            insert(hrvr, day_children("hrv", "r.body->'hrvReadings'")),
        ],
        [Shape(objects(_t), known(hrv) if _t == "hrv" else {"calendarDate", "weeklyAvg", "lastNightAvg", "lastNight5MinHigh", "baseline", "status", "feedbackPhrase", "createTimeStamp"}), Shape(objects("hrv", "$.hrvReadings"), known(hrvr))] if _t == "hrv" else [Shape(objects(_t), {"calendarDate", "weeklyAvg", "lastNightAvg", "lastNight5MinHigh", "baseline", "status", "feedbackPhrase", "createTimeStamp"})],
    )

tr = T("training_readiness")
add(
    "training_readiness",
    [delete_scope("training_readiness", "calendar_date", "::DATE"), insert(tr, day_children("training_readiness", "r.body"))],
    [Shape(objects("training_readiness", "$[*]"), known(tr))],
)

tstat = T("training_status")
add("training_status", [insert(tstat, day_root("training_status"))], [Shape(objects("training_status"), known(tstat))])

mm = T("max_metrics")
add("max_metrics", [insert(mm, day_root("max_metrics"))], [Shape(objects("max_metrics"), known(mm))])

es = T("endurance_score")
add("endurance_score", [insert(es, day_root("endurance_score"))], [Shape(objects("endurance_score"), known(es))])

hs = T("hill_score")
add("hill_score", [insert(hs, day_root("hill_score"))], [Shape(objects("hill_score"), known(hs))])

fa = T("fitness_age")
add("fitness_age", [insert(fa, day_root("fitness_age"))], [Shape(objects("fitness_age"), known(fa))])

sst = T("sleep_stats")
add("sleep_stats", [insert(sst, day_root("sleep_stats"))], [Shape(objects("sleep_stats"), known(sst))])

sdl = T("steps_daily")
add("steps_daily", [insert(sdl, day_root("steps_daily"))], [Shape(objects("steps_daily"), known(sdl))])

wm = T("wellness_metric")
add(
    "wellness_metrics",
    [
        insert(
            wm,
            f"(SELECT r.fetched_at, m.key AS metric, unnest(CAST(m.value AS JSON[])) AS b FROM {current('wellness_metrics')}, json_each(r.body->'allMetrics'->'metricsMap') m) src",
        )
    ],
    [Shape(objects("wellness_metrics"), {"userProfileId", "statisticsStartDate", "statisticsEndDate", "allMetrics", "groupedMetrics"})],
)

hyd = T("hydration")
add("hydration", [insert(hyd, day_root("hydration"))], [Shape(objects("hydration"), known(hyd))])

ade = T("all_day_event")
add(
    "all_day_events",
    [delete_scope("all_day_event", "calendar_date", "::DATE"), insert(ade, day_children("all_day_events", "r.body"))],
    [Shape(objects("all_day_events", "$[*]"), known(ade))],
)

nap = T("nap")
add("naps", [delete_scope("nap", "calendar_date", "::DATE"), insert(nap, day_children("naps", "r.body"))], [Shape(objects("naps", "$[*]"), known(nap))])

ll = T("lifestyle_logging")
add("lifestyle_logging", [insert(ll, day_root("lifestyle_logging"))], [Shape(objects("lifestyle_logging"), known(ll))])

ca = T("cycling_ability")
add("cycling_ability", [insert(ca, day_root("cycling_ability"))], [Shape(objects("cycling_ability"), known(ca))])

rt = T("running_tolerance")
add("running_tolerance", [insert(rt, day_root("running_tolerance"))], [Shape(objects("running_tolerance"), None)])

wi = T("weigh_in")
add(
    "weigh_ins",
    [
        insert(
            wi,
            f"(SELECT r.fetched_at, unnest(CAST(d->'allWeightMetrics' AS JSON[])) AS b FROM {current('weigh_ins')}, unnest(CAST(r.body->'dailyWeightSummaries' AS JSON[])) t(d)) src",
        )
    ],
    [Shape(objects("weigh_ins"), {"dailyWeightSummaries", "totalAverage", "previousDateWeight", "nextDateWeight"})],
)

bp = T("blood_pressure")
add(
    "blood_pressure",
    [
        insert(
            bp,
            f"(SELECT r.fetched_at, unnest(CAST(d->'measurements' AS JSON[])) AS b FROM {current('blood_pressure')}, unnest(CAST(r.body->'measurementSummaries' AS JSON[])) t(d)) src",
        )
    ],
    [Shape(objects("blood_pressure"), {"from", "until", "measurementSummaries", "categoryStats"})],
)

# ---------------------------------------------------------------- profile and settings

us = T("user_settings")
add("user_settings", ["DELETE FROM user_settings", insert(us, f"(SELECT r.fetched_at, r.body AS b FROM {current('user_settings')}) src")], [Shape(objects("user_settings"), known(us))])

up = T("user_profile")
PROFILE_SOURCE = """(
  SELECT greatest(coalesce(s.fetched_at, b.fetched_at), coalesce(b.fetched_at, s.fetched_at), coalesce(i.fetched_at, s.fetched_at), coalesce(p.fetched_at, s.fetched_at)) AS fetched_at,
         s.body AS settings, b.body AS base, i.body AS info, p.body AS privacy
  FROM (SELECT * FROM raw_current WHERE object_type = 'userprofile_settings' AND body IS NOT NULL) s
  FULL JOIN (SELECT * FROM raw_current WHERE object_type = 'user_profile_base' AND body IS NOT NULL) b ON true
  LEFT JOIN (SELECT * FROM raw_current WHERE object_type = 'personal_information' AND body IS NOT NULL) i ON true
  LEFT JOIN (SELECT * FROM raw_current WHERE object_type = 'privacy_settings' AND body IS NOT NULL) p ON true
) src WHERE base IS NOT NULL"""
for _t in ("userprofile_settings", "user_profile_base", "personal_information", "privacy_settings"):
    add(_t, ["DELETE FROM user_profile", insert(up, PROFILE_SOURCE)], [Shape(objects(_t), None)])

dev = T("device")
_dev_cols = lambda c: c.name not in ("historical",)  # noqa: E731
for _t, _hist in (("devices", "false"), ("historical_devices", "true")):
    add(
        _t,
        [
            upsert(dev, f"(SELECT r.fetched_at, {_hist} AS historical, unnest(CAST(r.body AS JSON[])) AS b FROM {current(_t)}) src", lambda c: True)
            if _t == "devices"
            else insert(dev, f"(SELECT r.fetched_at, {_hist} AS historical, unnest(CAST(r.body AS JSON[])) AS b FROM {current(_t)}) src", conflict="OR IGNORE"),
            "UPDATE device SET historical = false WHERE device_id IN (SELECT TRY_CAST(o->>'deviceId' AS BIGINT) FROM (" + objects("devices", "$[*]") + "))",
        ],
        [Shape(objects(_t, "$[*]"), None)],
    )

dset = T("device_settings")
add("device_settings", [insert(dset, f"(SELECT r.fetched_at, r.body AS b FROM {current('device_settings')}) src")], [Shape(objects("device_settings"), None)])

dlu = T("device_last_used")
add("device_last_used", ["DELETE FROM device_last_used", insert(dlu, f"(SELECT r.fetched_at, r.body AS b FROM {current('device_last_used')}) src")], [Shape(objects("device_last_used"), known(dlu))])

sens = T("sensor")
add("sensors", ["DELETE FROM sensor", insert(sens, f"(SELECT r.fetched_at, unnest(CAST(r.body AS JSON[])) AS b FROM {current('sensors')}) src")], [Shape(objects("sensors", "$[*]"), known(sens))])

gear = T("gear")
GEAR_SOURCE = f"""(
  SELECT g.fetched_at, g.b, s.body AS stats
  FROM (SELECT r.fetched_at, unnest(CAST(r.body AS JSON[])) AS b FROM (SELECT * FROM raw_current WHERE object_type = 'gear' AND body IS NOT NULL) r) g
  LEFT JOIN (SELECT * FROM raw_current WHERE object_type = 'gear_stats' AND body IS NOT NULL) s ON s.object_key = (g.b->>'uuid')
) src"""
add("gear", ["DELETE FROM gear", insert(gear, GEAR_SOURCE)], [Shape(objects("gear", "$[*]"), None)])
add("gear_stats", [insert(gear, GEAR_SOURCE)], [Shape(objects("gear_stats"), None)])

hrz = T("heart_rate_zones")
add("heart_rate_zones", ["DELETE FROM heart_rate_zones", insert(hrz, f"(SELECT r.fetched_at, unnest(CAST(r.body AS JSON[])) AS b FROM {current('heart_rate_zones')}) src")], [Shape(objects("heart_rate_zones", "$[*]"), known(hrz))])

pz = T("power_zones")
add("power_zones", ["DELETE FROM power_zones", insert(pz, f"(SELECT r.fetched_at, unnest(CAST(r.body AS JSON[])) AS b FROM {current('power_zones')}) src")], [Shape(objects("power_zones", "$[*]"), known(pz))])

pr = T("personal_record")
add("personal_records", ["DELETE FROM personal_record", insert(pr, f"(SELECT r.fetched_at, unnest(CAST(r.body AS JSON[])) AS b FROM {current('personal_records')}) src")], [Shape(objects("personal_records", "$[*]"), None)])

badge = T("badge")
add("earned_badges", [insert(badge, f"(SELECT r.fetched_at, true AS earned, unnest(CAST(r.body AS JSON[])) AS b FROM {current('earned_badges')}) src")], [Shape(objects("earned_badges", "$[*]"), None)])
add("available_badges", [insert(badge, f"(SELECT r.fetched_at, false AS earned, unnest(CAST(r.body AS JSON[])) AS b FROM {current('available_badges')}) src", conflict="OR IGNORE")], [Shape(objects("available_badges", "$[*]"), None)])

chal = T("challenge")
for _t, _kind in (
    ("badge_challenges", "badge_completed"),
    ("available_badge_challenges", "badge_available"),
    ("non_completed_badge_challenges", "badge_non_completed"),
    ("inprogress_virtual_challenges", "virtual_in_progress"),
    ("adhoc_challenges", "adhoc"),
):
    add(
        _t,
        [f"DELETE FROM challenge WHERE kind = '{_kind}'", insert(chal, f"(SELECT r.fetched_at, '{_kind}' AS kind, unnest(CAST(r.body AS JSON[])) AS b FROM {current(_t)}) src")],
        [Shape(objects(_t, "$[*]"), None)],
    )

goal = T("goal")
for _t, _kind in (("user_goals", "user"), ("goals_active", "active"), ("goals_past", "past")):
    add(
        _t,
        [f"DELETE FROM goal WHERE kind = '{_kind}'", insert(goal, f"(SELECT r.fetched_at, '{_kind}' AS kind, unnest(CAST(r.body AS JSON[])) AS b FROM {current(_t)}) src")],
        [Shape(objects(_t, "$[*]"), None)],
    )

wk = T("workout")
WORKOUT_SOURCE = f"""(
  SELECT w.fetched_at, w.b, d.body AS detail
  FROM (SELECT r.fetched_at, unnest(CAST(r.body AS JSON[])) AS b FROM (SELECT * FROM raw_current WHERE object_type = 'workouts' AND body IS NOT NULL) r) w
  LEFT JOIN (SELECT * FROM raw_current WHERE object_type = 'workout' AND body IS NOT NULL) d ON d.object_key = (w.b->>'workoutId')
) src"""
add("workouts", ["DELETE FROM workout", insert(wk, WORKOUT_SOURCE)], [Shape(objects("workouts", "$[*]"), None)])
add("workout", [insert(wk, WORKOUT_SOURCE)], [Shape(objects("workout"), None)])

ci = T("calendar_item")
add(
    "calendar_month",
    [
        "DELETE FROM calendar_item WHERE month IN (SELECT object_key FROM _scope)",
        insert(ci, children("calendar_month", "r.body->'calendarItems'", "r.object_key AS month")),
    ],
    [Shape(objects("calendar_month", "$.calendarItems"), None)],
)

course = T("course")
COURSE_SOURCE = f"""(
  SELECT c.fetched_at, c.b, d.body AS detail, d.checked_at AS detail_fetched_at
  FROM (SELECT r.fetched_at, unnest(CAST(r.body AS JSON[])) AS b FROM (SELECT * FROM raw_current WHERE object_type = 'courses' AND body IS NOT NULL) r) c
  LEFT JOIN (SELECT * FROM raw_current WHERE object_type = 'course' AND body IS NOT NULL) d ON d.object_key = (c.b->>'courseId')
) src"""
add("courses", ["DELETE FROM course", insert(course, COURSE_SOURCE)], [Shape(objects("courses", "$[*]"), None)])
add("course", [insert(course, COURSE_SOURCE)], [Shape(objects("course"), None)])

tp = T("training_plan")
add("training_plans", ["DELETE FROM training_plan", insert(tp, f"(SELECT r.fetched_at, unnest(CAST(r.body->'trainingPlanList' AS JSON[])) AS b FROM {current('training_plans')}) src")], [Shape(objects("training_plans"), {"trainingPlanList", "searchFilter"})])

ce = T("calendar_event")
add("calendar_events", ["DELETE FROM calendar_event", insert(ce, f"(SELECT r.fetched_at, unnest(CAST(r.body AS JSON[])) AS b FROM {current('calendar_events')}) src")], [Shape(objects("calendar_events", "$[*]"), None)])

rp = T("race_prediction")
add(
    "race_predictions",
    ["DELETE FROM race_prediction", insert(rp, "(SELECT p.fetched_at, p.body AS b FROM raw_payload p WHERE p.object_type = 'race_predictions') src")],
    [Shape(objects("race_predictions"), known(rp, extra=["userId", "fromCalendarDate", "toCalendarDate"]))],
)

SNAPSHOT_KINDS = [
    "activity_types", "activity_event_types", "cycling_ftp", "current_biometrics", "lifetime_totals", "user_statistics",
    "workout_count", "activity_count", "nutrition_status", "sync_timestamp", "primary_training_device", "jetlag_trips",
    "golf_summary", "active_training_plans", "gear_defaults",
]
snap = T("snapshot")
for _k in SNAPSHOT_KINDS:
    add(_k, [insert(snap, f"(SELECT r.object_type, r.fetched_at, r.body FROM {current(_k)}) src")], [Shape(objects(_k), None)])

PROJECTION_BY_TYPE = {p.type: p for p in PROJECTIONS}


def project(tx: Tx, type_: str, keys: Sequence[str] | None) -> None:
    """Rebuild the typed rows for `keys` of `type_` (all keys when None)."""
    projection = PROJECTION_BY_TYPE.get(type_)
    if projection is None:
        return
    tx.run("CREATE OR REPLACE TEMP TABLE _scope (object_key VARCHAR)")
    if keys is None:
        tx.run("INSERT INTO _scope SELECT object_key FROM raw_latest WHERE object_type = ?", [type_])
    elif len(keys) > 0:
        tx.run("INSERT INTO _scope SELECT unnest(?::VARCHAR[])", [list(keys)])
    for sql in projection.statements:
        tx.run(sql)


def rebuild_all(tx: Tx) -> None:
    for p in PROJECTIONS:
        project(tx, p.type, None)


def drift(tx: Tx) -> list[dict]:
    """Keys present in landed payloads that no projection maps: schema drift to look at."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for p in PROJECTIONS:
        for shape in p.shapes:
            if shape.known is None:
                continue
            rows = tx.all(
                f"SELECT k, count(*)::INTEGER AS n FROM (SELECT unnest(json_keys(o)) AS k FROM ({shape.objects}) WHERE o IS NOT NULL AND json_type(o) = 'OBJECT') GROUP BY k"
            )
            for r in rows:
                if r["k"] not in shape.known and (p.type, r["k"]) not in seen:
                    seen.add((p.type, r["k"]))
                    out.append({"type": p.type, "key": r["k"], "example_count": r["n"]})
    return out

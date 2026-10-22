"""The dataset registry: every Garmin Connect endpoint the mirror keeps, how it is
fetched, how its response is keyed, and how often it is refreshed.

Kinds:
  snapshot  - one payload per key (usually 'self'), refreshed every `refresh`.
  day       - one call per calendar day, keyed by the date. `intraday` datasets carry
              per-sample arrays that Garmin archives after ~6 months (an archive reload
              brings them back).
  range     - one call per date window of up to `chunk_days`; the response is split
              into per-day payloads (keyed by date) unless `whole` is set, in which
              case it is stored under 'start_end'.
  activity  - one call per activity id.
Paths use {dn} for the user's display name and {key}/{start}/{end} for the key.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

Kind = Literal["snapshot", "day", "range", "activity"]

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
WEEK = timedelta(days=7)


def _dumps(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)


@dataclass
class Dataset:
    name: str
    kind: Kind
    path: str
    params: dict[str, Any] = field(default_factory=dict)
    priority: int = 25
    refresh: timedelta | None = None
    # range datasets
    chunk_days: int = 28
    split: Callable[[Any], list[tuple[str, str]]] | None = None
    whole: bool = False
    # day datasets
    intraday: bool = False
    # element key for list splits (range datasets split by date)
    date_key: str = "calendarDate"
    doc: str = ""

    def resolve(self, dn: str, key: str = "self", start: str | None = None, end: str | None = None) -> tuple[str, dict[str, Any]]:
        fmt = {"dn": dn, "key": key, "start": start or key, "end": end or key}
        path = self.path.format(**fmt)
        params = {k: (v.format(**fmt) if isinstance(v, str) else v) for k, v in self.params.items()}
        return path, params


# ----------------------------------------------------------------- split helpers


def split_by_date(key: str = "calendarDate") -> Callable[[Any], list[tuple[str, str]]]:
    def fn(body: Any) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if isinstance(body, dict):
            for k in ("data", "hrvSummaries", "individualStats", "hillScoreDTOList", "values"):
                if isinstance(body.get(k), list):
                    body = body[k]
                    break
        if not isinstance(body, list):
            return out
        for el in body:
            if isinstance(el, dict) and el.get(key):
                out.append((str(el[key])[:10], _dumps(el)))
        return out

    return fn


def split_grouped_by_date(key: str = "calendarDate") -> Callable[[Any], list[tuple[str, str]]]:
    """Several elements per day (training readiness) -> one JSON array per day."""

    def fn(body: Any) -> list[tuple[str, str]]:
        groups: dict[str, list[Any]] = {}
        if isinstance(body, list):
            for el in body:
                if isinstance(el, dict) and el.get(key):
                    groups.setdefault(str(el[key])[:10], []).append(el)
        return [(d, _dumps(v)) for d, v in groups.items()]

    return fn


def split_max_metrics(body: Any) -> list[tuple[str, str]]:
    out = []
    if isinstance(body, list):
        for el in body:
            d = ((el or {}).get("generic") or {}).get("calendarDate") or ((el or {}).get("heatAltitudeAcclimation") or {}).get("calendarDate")
            if d:
                out.append((str(d)[:10], _dumps(el)))
    return out


def split_endurance(body: Any) -> list[tuple[str, str]]:
    gm = (body or {}).get("groupMap") if isinstance(body, dict) else None
    if not isinstance(gm, dict):
        return []
    return [(d, _dumps(v)) for d, v in gm.items() if isinstance(v, dict) and (v.get("groupAverage") is not None or v.get("groupMax") is not None)]


def split_body_battery(body: Any) -> list[tuple[str, str]]:
    return split_by_date("date")(body)


# ----------------------------------------------------------------- the registry

DATASETS: list[Dataset] = [
    # --- activities (list is handled by the engine; these are per activity)
    Dataset("activity", "activity", "/activity-service/activity/{key}", priority=20, doc="Activity detail (summaryDTO, metadataDTO)."),
    Dataset("activity_splits", "activity", "/activity-service/activity/{key}/splits", priority=21, doc="Laps and events."),
    Dataset("activity_typed_splits", "activity", "/activity-service/activity/{key}/typedsplits", priority=22, doc="Typed splits (intervals, climbs)."),
    Dataset("activity_split_summaries", "activity", "/activity-service/activity/{key}/split_summaries", priority=22, doc="Split summaries."),
    Dataset("activity_weather", "activity", "/activity-service/activity/{key}/weather", priority=23, doc="Weather at start."),
    Dataset("activity_hr_zones", "activity", "/activity-service/activity/{key}/hrTimeInZones", priority=23, doc="Time in HR zones."),
    Dataset("activity_power_zones", "activity", "/activity-service/activity/{key}/powerTimeInZones", priority=23, doc="Time in power zones."),
    Dataset("activity_gear", "activity", "/gear-service/gear/filterGear", {"activityId": "{key}"}, priority=24, doc="Gear linked to the activity."),
    Dataset("activity_exercise_sets", "activity", "/activity-service/activity/{key}/exerciseSets", priority=24, doc="Strength exercise sets."),
    Dataset("activity_polyline", "activity", "/activity-service/activity/polyline/{key}", priority=24, doc="Encoded GPS track."),
    Dataset("activity_details", "activity", "/activity-service/activity/{key}/details", {"maxChartSize": "2000", "maxPolylineSize": "4000"}, priority=45, doc="Chart series (large; the FIT file has the full-resolution data)."),
    # --- per day: intraday (archived after ~6 months)
    Dataset("user_summary", "day", "/usersummary-service/usersummary/daily/{dn}", {"calendarDate": "{key}"}, priority=20, doc="Daily summary."),
    Dataset("heart_rates", "day", "/wellness-service/wellness/dailyHeartRate/{dn}", {"date": "{key}"}, priority=21, intraday=True, doc="Heart rate every 2 minutes."),
    Dataset("stress", "day", "/wellness-service/wellness/dailyStress/{key}", priority=21, intraday=True, doc="Stress and Body Battery every 3 minutes."),
    Dataset("sleep", "day", "/wellness-service/wellness/dailySleepData/{dn}", {"date": "{key}", "nonSleepBufferMinutes": 60}, priority=21, intraday=True, doc="Sleep with all overnight series."),
    Dataset("respiration", "day", "/wellness-service/wellness/daily/respiration/{key}", priority=22, intraday=True, doc="Respiration every 2 minutes."),
    Dataset("spo2", "day", "/wellness-service/wellness/daily/spo2/{key}", priority=22, intraday=True, doc="Pulse ox."),
    Dataset("floors", "day", "/wellness-service/wellness/floorsChartData/daily/{key}", priority=23, intraday=True, doc="Floors per 15 minutes."),
    Dataset("body_battery_events", "day", "/wellness-service/wellness/bodyBattery/events/{key}", priority=23, intraday=True, doc="Body Battery events with traces."),
    Dataset("hrv", "day", "/hrv-service/hrv/{key}", priority=22, intraday=True, doc="HRV status with 5-minute readings."),
    Dataset("intensity_minutes", "day", "/wellness-service/wellness/daily/im/{key}", priority=24, intraday=True, doc="Intensity minutes."),
    Dataset("steps_chart", "day", "/wellness-service/wellness/dailySummaryChart/{dn}", {"date": "{key}"}, priority=24, doc="Steps per 15 minutes (not archived)."),
    # --- per day: summaries that stay available
    Dataset("training_status", "day", "/metrics-service/metrics/trainingstatus/aggregated/{key}", priority=24, doc="Training status, VO2 max, load balance."),
    Dataset("all_day_events", "day", "/wellness-service/wellness/dailyEvents", {"calendarDate": "{key}"}, priority=25, doc="Auto-detected activity events."),
    Dataset("hydration", "day", "/usersummary-service/usersummary/hydration/daily/{key}", priority=26, doc="Hydration log."),
    Dataset("naps", "day", "/sleep-service/sleep/naps/{key}", {"includeOverlaps": "true"}, priority=26, doc="Naps."),
    Dataset("lifestyle_logging", "day", "/lifestylelogging-service/dailyLog/{key}", priority=27, doc="Lifestyle logging."),
    Dataset("cycling_ability", "day", "/metrics-service/metrics/cycling-ability/latest", {"calendarDate": "{key}"}, priority=27, doc="Cycling ability."),
    Dataset("running_tolerance", "day", "/metrics-service/metrics/runningtolerance/stats", {"startDate": "{key}", "endDate": "{key}", "aggregation": "daily"}, priority=27, doc="Running tolerance."),
    # --- ranges split per day (fast coverage; up to chunk_days per call)
    Dataset("daily_summary_v2", "range", "/usersummary-service/v2/dailysummaries", {"startDate": "{start}", "endDate": "{end}"}, priority=15, chunk_days=28, split=split_by_date(), doc="Compact daily summaries."),
    Dataset("steps_daily", "range", "/usersummary-service/stats/steps/daily/{start}/{end}", priority=15, chunk_days=28, split=split_by_date(), doc="Steps per day."),
    Dataset("sleep_stats", "range", "/sleep-service/stats/sleep/daily/{start}/{end}", priority=16, chunk_days=28, split=split_by_date(), doc="Sleep statistics per day."),
    Dataset("hrv_summary", "range", "/hrv-service/hrv/daily/{start}/{end}", priority=16, chunk_days=28, split=split_by_date(), doc="HRV summary per day."),
    Dataset("training_readiness", "range", "/metrics-service/metrics/trainingreadiness/{start}/{end}", priority=16, chunk_days=28, split=split_grouped_by_date(), doc="Training readiness."),
    Dataset("max_metrics", "range", "/metrics-service/metrics/maxmet/daily/{start}/{end}", priority=16, chunk_days=28, split=split_max_metrics, doc="VO2 max and acclimation."),
    Dataset("body_battery", "range", "/wellness-service/wellness/bodyBattery/reports/daily", {"startDate": "{start}", "endDate": "{end}"}, priority=17, chunk_days=28, split=split_body_battery, doc="Body Battery per day."),
    Dataset("endurance_score", "range", "/metrics-service/metrics/endurancescore/stats", {"startDate": "{start}", "endDate": "{end}", "aggregation": "daily"}, priority=18, chunk_days=28, split=split_endurance, doc="Endurance score."),
    Dataset("hill_score", "range", "/metrics-service/metrics/hillscore/stats", {"startDate": "{start}", "endDate": "{end}", "aggregation": "daily"}, priority=18, chunk_days=28, split=split_by_date(), doc="Hill score."),
    Dataset("fitness_age", "range", "/fitnessage-service/stats/daily/{start}/{end}", priority=18, chunk_days=28, split=split_by_date(), doc="Fitness age."),
    # --- ranges stored whole per chunk
    Dataset("wellness_metrics", "range", "/userstats-service/wellness/daily/{dn}", {"fromDate": "{start}", "untilDate": "{end}", "metricId": [60, 22, 23]}, priority=18, chunk_days=28, whole=True, doc="Resting HR and calories per day."),
    Dataset("weigh_ins", "range", "/weight-service/weight/range/{start}/{end}", {"includeAll": "true"}, priority=19, chunk_days=28, whole=True, doc="Weigh-ins."),
    Dataset("blood_pressure", "range", "/bloodpressure-service/bloodpressure/range/{start}/{end}", {"includeAll": "true"}, priority=19, chunk_days=28, whole=True, doc="Blood pressure."),
    # --- snapshots (key self unless noted)
    Dataset("user_settings", "snapshot", "/userprofile-service/userprofile/user-settings", refresh=WEEK, priority=15),
    Dataset("userprofile_settings", "snapshot", "/userprofile-service/userprofile/settings", refresh=WEEK, priority=15),
    Dataset("user_profile_base", "snapshot", "/userprofile-service/userprofile/userProfileBase", refresh=WEEK, priority=15),
    Dataset("personal_information", "snapshot", "/userprofile-service/userprofile/personal-information/{dn}", {"includeBiometric": "true", "includeVO2Max": "true"}, refresh=WEEK, priority=15),
    Dataset("privacy_settings", "snapshot", "/privacysettings-service/privacy-settings", refresh=WEEK, priority=30),
    Dataset("devices", "snapshot", "/device-service/deviceregistration/devices", refresh=WEEK, priority=15),
    Dataset("historical_devices", "snapshot", "/device-service/deviceregistration/devices/historical", refresh=WEEK, priority=30),
    Dataset("device_settings", "snapshot", "/device-service/deviceservice/device-info/settings/{key}", refresh=WEEK, priority=30, doc="Keyed by device id."),
    Dataset("primary_training_device", "snapshot", "/web-gateway/device-info/primary-training-device", refresh=WEEK, priority=30),
    Dataset("sensors", "snapshot", "/device-service/sensors", refresh=WEEK, priority=30),
    Dataset("gear", "snapshot", "/gear-service/gear/filterGear", {"userProfilePk": "{key}"}, refresh=DAY, priority=15, doc="Keyed by user profile pk."),
    Dataset("gear_defaults", "snapshot", "/gear-service/gear/user/{key}/activityTypes", refresh=WEEK, priority=30, doc="Keyed by user profile pk."),
    Dataset("gear_stats", "snapshot", "/gear-service/gear/stats/{key}", refresh=DAY, priority=30, doc="Keyed by gear uuid."),
    Dataset("heart_rate_zones", "snapshot", "/biometric-service/heartRateZones", refresh=DAY, priority=15),
    Dataset("power_zones", "snapshot", "/biometric-service/powerZones/sports/all", refresh=DAY, priority=15),
    Dataset("personal_records", "snapshot", "/personalrecord-service/personalrecord/prs/{dn}", {"includeHistory": "true"}, refresh=DAY, priority=15),
    Dataset("current_biometrics", "snapshot", "/userprofile-service/biometric/current/{dn}", refresh=DAY, priority=30),
    Dataset("lifetime_totals", "snapshot", "/usersummary-service/stats/connectLifetimeTotals/{dn}", refresh=DAY, priority=30),
    Dataset("user_statistics", "snapshot", "/userstats-service/statistics/{dn}", refresh=DAY, priority=30),
    Dataset("race_predictions", "snapshot", "/metrics-service/metrics/racepredictions/latest/{dn}", refresh=DAY, priority=15),
    Dataset("cycling_ftp", "snapshot", "/biometric-service/biometric/latestFunctionalThresholdPower/CYCLING", refresh=DAY, priority=30),
    Dataset("earned_badges", "snapshot", "/badge-service/badge/earned", refresh=DAY, priority=30),
    Dataset("available_badges", "snapshot", "/badge-service/badge/available", {"showExclusiveBadge": "true"}, refresh=WEEK, priority=40),
    Dataset("badge_challenges", "snapshot", "/badgechallenge-service/badgeChallenge/completed", {"start": "1", "limit": "100"}, refresh=WEEK, priority=30),
    Dataset("available_badge_challenges", "snapshot", "/badgechallenge-service/badgeChallenge/available", {"start": "1", "limit": "100"}, refresh=WEEK, priority=40),
    Dataset("non_completed_badge_challenges", "snapshot", "/badgechallenge-service/badgeChallenge/non-completed", {"start": "1", "limit": "100"}, refresh=WEEK, priority=30),
    Dataset("inprogress_virtual_challenges", "snapshot", "/badgechallenge-service/virtualChallenge/inProgress", {"start": "1", "limit": "100"}, refresh=WEEK, priority=30),
    Dataset("adhoc_challenges", "snapshot", "/adhocchallenge-service/adHocChallenge/historical", {"start": "1", "limit": "100"}, refresh=WEEK, priority=30),
    Dataset("goals_active", "snapshot", "/goal-service/goal/goals", {"status": "active", "start": "1", "limit": "100", "sortOrder": "asc"}, refresh=DAY, priority=30),
    Dataset("goals_past", "snapshot", "/goal-service/goal/goals", {"status": "past", "start": "1", "limit": "100", "sortOrder": "asc"}, refresh=WEEK, priority=30),
    Dataset("user_goals", "snapshot", "/goal-service/goal/user", refresh=DAY, priority=30),
    Dataset("workouts", "snapshot", "/workout-service/workouts", {"start": 0, "limit": 500, "myWorkoutsOnly": "true", "sharedWorkoutsOnly": "false"}, refresh=DAY, priority=15),
    Dataset("workout", "snapshot", "/workout-service/workout/{key}", refresh=None, priority=30, doc="Keyed by workout id; refetched when the list's updateDate advances."),
    Dataset("workout_count", "snapshot", "/workout-service/workouts/count", {"includeAtp": "true"}, refresh=DAY, priority=30),
    Dataset("calendar_month", "snapshot", "/calendar-service/year/{start}/month/{end}", refresh=None, priority=30, doc="Keyed by YYYY-MM (Garmin months are 0-based in the path)."),
    Dataset("training_plans", "snapshot", "/trainingplan-service/trainingplan/plans", {"start": 0, "limit": 100}, refresh=DAY, priority=30),
    Dataset("active_training_plans", "snapshot", "/trainingplan-service/trainingplan/active-plans", {"startDate": "{key}"}, refresh=DAY, priority=30, doc="Keyed by today's date."),
    Dataset("courses", "snapshot", "/course-service/course/owner/{dn}", refresh=DAY, priority=15),
    Dataset("course", "snapshot", "/course-service/course/{key}", refresh=None, priority=35, doc="Keyed by course id; refetched when updatedDate advances."),
    Dataset("calendar_events", "snapshot", "/calendar-service/events", {"startDate": "{start}", "endDate": "{end}"}, refresh=DAY, priority=30, doc="Key start_end (past year to next year)."),
    Dataset("activity_types", "snapshot", "/activity-service/activity/activityTypes", refresh=WEEK, priority=40),
    Dataset("activity_event_types", "snapshot", "/activity-service/activity/eventTypes", refresh=WEEK, priority=40),
    Dataset("nutrition_status", "snapshot", "/nutrition-service/user/nutritionCurrentStatus", refresh=WEEK, priority=40),
    Dataset("jetlag_trips", "snapshot", "/jetlag-service/jetlag/trip/all", refresh=WEEK, priority=40),
    Dataset("golf_summary", "snapshot", "/gcs-golfcommunity/api/v2/scorecard/summary", {"per-page": "100", "start": "0"}, refresh=WEEK, priority=40),
    Dataset("activity_count", "snapshot", "/activitylist-service/activities/count", refresh=DAY, priority=30),
    Dataset("device_last_used", "snapshot", "/device-service/deviceservice/mylastused", refresh=timedelta(minutes=15), priority=10, doc="Polled to detect new syncs."),
    Dataset("sync_timestamp", "snapshot", "/wellness-service/wellness/syncTimestamp", refresh=timedelta(minutes=15), priority=10),
]

BY_NAME: dict[str, Dataset] = {d.name: d for d in DATASETS}
DAY_DATASETS = [d for d in DATASETS if d.kind == "day"]
INTRADAY_DATASETS = [d for d in DAY_DATASETS if d.intraday]
RANGE_DATASETS = [d for d in DATASETS if d.kind == "range"]
ACTIVITY_DATASETS = [d for d in DATASETS if d.kind == "activity"]
SNAPSHOT_DATASETS = [d for d in DATASETS if d.kind == "snapshot"]

ACTIVITY_LIST_PATH = "/activitylist-service/activities/search/activities"
ACTIVITY_FIT_PATH = "/download-service/files/activity/{key}"
WELLNESS_FIT_PATH = "/download-service/files/wellness/{key}"
RELOAD_PATH = "/wellness-service/wellness/epoch/request/{key}"

# Datasets a tool may ask for whose answer is a derived report: cached for a while, never backfilled.
REPORT_TTL = timedelta(hours=1)


def dataset(name: str) -> Dataset:
    return BY_NAME[name]


def days_between(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]

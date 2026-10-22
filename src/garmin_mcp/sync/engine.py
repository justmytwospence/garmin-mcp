"""The sync engine: a durable job queue in the mirror, a single worker that drains it
within Garmin's limits, a planner that turns gaps in the mirror into jobs, and a
scheduler for the periodic snapshots and the device-sync poll.

Priorities (lower runs first):
    5   on-demand refreshes queued by tools
   10   device poll, recent window after a device sync, new activities
   15-19 range datasets (fast coverage of daily summaries, newest first)
   20-29 per-activity and per-day datasets (newest first), FIT files
   30-40 snapshots with no urgency, details, reload requests
   45   activity chart details (large, redundant with FIT)
   50   archive reloads and their re-fetches
Below 10 is the interactive rate class, below 20 incremental, the rest backfill.

Job kinds and keys:
  poll:self, window:self, list_recent:self, list_full:self,
  snap:<dataset>:<key>, day:<dataset>:<date>, range:<dataset>:<start>_<end>,
  act:<dataset>:<activity id>, activity_fit:<id>, wellness_fit:<date>,
  reload:<date>, refetch_day:<date>
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..db.database import Database
from ..garmin.client import AuthError, GarminClient, Priority, RateLimited
from ..garmin.datasets import (
    ACTIVITY_DATASETS,
    DAY_DATASETS,
    INTRADAY_DATASETS,
    RANGE_DATASETS,
    SNAPSHOT_DATASETS,
    dataset,
)
from .fetchers import Fetchers

log = logging.getLogger("garmin_mcp.sync")

MINUTE = 60
HOUR = 3600
RELOADS_PER_DAY = 25
ARCHIVE_AGE_DAYS = 180
RECENT_WINDOW_DAYS = 3
REFETCH_DELAY = timedelta(hours=3)


def priority_class(priority: int) -> Priority:
    if priority < 10:
        return "interactive"
    if priority < 20:
        return "incremental"
    return "backfill"


class SyncEngine:
    def __init__(
        self,
        db: Database,
        client: GarminClient,
        data_dir: str,
        *,
        on_change: Callable[[], None] | None = None,
        history_start: str | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.db = db
        self.client = client
        self.fetch = Fetchers(db, client, data_dir)
        self.on_change = on_change
        self.history_start_env = history_start or os.environ.get("GARMIN_HISTORY_START")
        self._running = False
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self.current: dict[str, Any] | None = None
        self._sleep = sleep

    # ------------------------------------------------------------- time

    def time_zone(self) -> str:
        row = self.db.one("SELECT time_zone FROM user_profile LIMIT 1")
        return (row or {}).get("time_zone") or "UTC"

    def today(self) -> date:
        try:
            tz = ZoneInfo(self.time_zone())
        except Exception:
            tz = UTC
        return datetime.now(tz).date()

    def history_start(self) -> date | None:
        if self.history_start_env:
            return date.fromisoformat(self.history_start_env)
        row = self.db.one("SELECT value FROM sync_state WHERE name = 'history_start'")
        if row and row["value"]:
            return date.fromisoformat(row["value"])
        first_activity = self.db.scalar("SELECT min(start_time_local)::DATE FROM activity WHERE deleted_at IS NULL")
        created = self.db.scalar("SELECT create_date::DATE FROM user_profile LIMIT 1")
        candidates = [d for d in (first_activity, created) if d is not None]
        if not candidates:
            return None
        start = min(candidates)
        self.set_state("history_start", start.isoformat())
        return start

    # ------------------------------------------------------------- state

    def set_state(self, name: str, value: str | None) -> None:
        self.db.run("INSERT OR REPLACE INTO sync_state (name, value, updated_at) VALUES (?, ?, now())", [name, value])

    def get_state(self, name: str) -> str | None:
        row = self.db.one("SELECT value FROM sync_state WHERE name = ?", [name])
        return row["value"] if row else None

    def mark_ran(self, kind: str) -> None:
        self.set_state(f"last:{kind}", datetime.now(UTC).isoformat())

    # ------------------------------------------------------------- queue

    def enqueue(self, kind: str, key: str, priority: int, sort_key: int = 0, not_before: datetime | None = None) -> None:
        self.db.run(
            """INSERT INTO sync_job (kind, key, priority, sort_key, not_before) VALUES (?, ?, ?, ?, coalesce(?, now()))
               ON CONFLICT (kind, key) DO UPDATE SET priority = least(sync_job.priority, excluded.priority),
                 not_before = CASE WHEN excluded.priority < sync_job.priority THEN excluded.not_before ELSE sync_job.not_before END""",
            [kind, key, priority, sort_key, not_before],
        )
        self._wake.set()

    def _enqueue_many(self, rows: list[tuple[str, str, int, int]]) -> int:
        if not rows:
            return 0
        before = int(self.db.scalar("SELECT count(*) FROM sync_job"))
        with self.db.transaction() as tx:
            tx.run("CREATE OR REPLACE TEMP TABLE _jobs (kind VARCHAR, key VARCHAR, priority INTEGER, sort_key BIGINT)")
            for i in range(0, len(rows), 5000):
                chunk = rows[i : i + 5000]
                tx.run(
                    "INSERT INTO _jobs SELECT unnest(?::VARCHAR[]), unnest(?::VARCHAR[]), unnest(?::INTEGER[]), unnest(?::BIGINT[])",
                    [[r[0] for r in chunk], [r[1] for r in chunk], [r[2] for r in chunk], [r[3] for r in chunk]],
                )
            tx.run(
                """INSERT INTO sync_job (kind, key, priority, sort_key) SELECT kind, key, priority, sort_key FROM _jobs
                   ON CONFLICT (kind, key) DO UPDATE SET priority = least(sync_job.priority, excluded.priority)"""
            )
            tx.run("DROP TABLE _jobs")
        return int(self.db.scalar("SELECT count(*) FROM sync_job")) - before

    def next_job(self) -> dict[str, Any] | None:
        return self.db.one(
            "SELECT kind, key, priority, attempts FROM sync_job WHERE not_before <= now() ORDER BY priority, sort_key DESC, created_at LIMIT 1"
        )

    # ------------------------------------------------------------- planner

    @staticmethod
    def _age_bump(day: date, today: date) -> int:
        age = (today - day).days
        if age <= 30:
            return 0
        if age <= 400:
            return 10
        return 20

    def plan(self) -> int:
        """Turn every gap in the mirror into a job. Costs no API calls; idempotent."""
        today = self.today()
        start = self.history_start()
        rows: list[tuple[str, str, int, int]] = []
        have_profile = self.db.scalar("SELECT count(*) FROM user_profile") or 0
        if start is not None and have_profile:
            yesterday = today - timedelta(days=1)
            rows += self._plan_days(start, yesterday, today)
            rows += self._plan_ranges(start, today)
            rows += self._plan_wellness_files(start, yesterday, today)
            rows += self._plan_calendar(start, today)
        rows += self._plan_activities(today)
        rows += self._plan_snapshots(today)
        added = self._enqueue_many(rows)
        added += self._plan_reloads(today)
        return added

    def _missing_days(self, type_: str, start: date, end: date) -> list[date]:
        rows = self.db.all(
            """SELECT d::DATE AS d FROM range(?::DATE, ?::DATE + INTERVAL 1 DAY, INTERVAL 1 DAY) t(d)
               WHERE NOT EXISTS (SELECT 1 FROM raw_latest r WHERE r.object_type = ? AND r.object_key = strftime(d, '%Y-%m-%d'))
               ORDER BY d DESC""",
            [start, end, type_],
        )
        return [r["d"] for r in rows]

    def _plan_days(self, start: date, end: date, today: date) -> list[tuple[str, str, int, int]]:
        out = []
        for ds in DAY_DATASETS:
            for d in self._missing_days(ds.name, start, end):
                out.append((f"day:{ds.name}", d.isoformat(), ds.priority + self._age_bump(d, today), int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())))
        return out

    def _plan_ranges(self, start: date, today: date) -> list[tuple[str, str, int, int]]:
        out = []
        for ds in RANGE_DATASETS:
            if ds.whole:
                # chunks are keyed start_end on a fixed grid from the history start
                d = start
                while d <= today:
                    e = min(d + timedelta(days=ds.chunk_days - 1), today)
                    key = f"{d.isoformat()}_{e.isoformat()}"
                    row = self.db.one("SELECT checked_at FROM raw_latest WHERE object_type = ? AND object_key = ?", [ds.name, key])
                    stale = row is None or (e >= today - timedelta(days=1) and row["checked_at"] < datetime.now(UTC) - timedelta(days=1))
                    if stale:
                        out.append((f"range:{ds.name}", key, ds.priority + self._age_bump(e, today), int(datetime(e.year, e.month, e.day, tzinfo=UTC).timestamp())))
                    d = e + timedelta(days=1)
                continue
            missing = self._missing_days(ds.name, start, today - timedelta(days=1))
            # group missing days into windows of chunk_days
            missing_sorted = sorted(missing)
            i = 0
            while i < len(missing_sorted):
                s = missing_sorted[i]
                e_max = s + timedelta(days=ds.chunk_days - 1)
                j = i
                while j + 1 < len(missing_sorted) and missing_sorted[j + 1] <= e_max:
                    j += 1
                e = missing_sorted[j]
                out.append((f"range:{ds.name}", f"{s.isoformat()}_{e.isoformat()}", ds.priority + self._age_bump(e, today), int(datetime(e.year, e.month, e.day, tzinfo=UTC).timestamp())))
                i = j + 1
        return out

    def _plan_wellness_files(self, start: date, end: date, today: date) -> list[tuple[str, str, int, int]]:
        return [
            ("wellness_fit", d.isoformat(), 28 + self._age_bump(d, today), int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp()))
            for d in self._missing_days("wellness_fit", start, end)
        ]

    def _plan_calendar(self, start: date, today: date) -> list[tuple[str, str, int, int]]:
        out = []
        d = date(start.year, start.month, 1)
        last = date(today.year, today.month, 1) + timedelta(days=32)
        last = date(last.year, last.month, 1)
        while d <= last:
            key = f"{d.year}-{d.month:02d}"
            row = self.db.one("SELECT checked_at FROM raw_latest WHERE object_type = 'calendar_month' AND object_key = ?", [key])
            current = abs((d - date(today.year, today.month, 1)).days) <= 31
            stale = row is None or (current and row["checked_at"] < datetime.now(UTC) - timedelta(days=1))
            if stale:
                out.append(("snap:calendar_month", key, 30 if current else 40, int(datetime(d.year, d.month, 1, tzinfo=UTC).timestamp())))
            d = (d + timedelta(days=32)).replace(day=1)
        return out

    def _plan_activities(self, today: date) -> list[tuple[str, str, int, int]]:
        out = []
        for ds in ACTIVITY_DATASETS:
            rows = self.db.all(
                """SELECT a.activity_id::VARCHAR AS id, epoch(a.start_time_gmt)::BIGINT AS sk, a.start_time_local::DATE AS d, a.activity_type AS t
                   FROM activity a
                   LEFT JOIN raw_latest r ON r.object_type = ? AND r.object_key = a.activity_id::VARCHAR
                   WHERE a.deleted_at IS NULL AND (r.object_key IS NULL OR (? = 'activity' AND a.summary_fetched_at > r.checked_at))""",
                [ds.name, ds.name],
            )
            for r in rows:
                if ds.name == "activity_exercise_sets" and r["t"] not in ("strength_training", "indoor_cardio", "hiit", "yoga", "pilates", "breathwork", "fitness_equipment"):
                    continue
                if ds.name == "activity_power_zones" and r["t"] not in ("cycling", "road_biking", "gravel_cycling", "mountain_biking", "indoor_cycling", "virtual_ride", "e_bike_fitness", "running", "trail_running", "treadmill_running"):
                    continue
                d = r["d"] or today
                out.append((f"act:{ds.name}", r["id"], ds.priority + self._age_bump(d, today), int(r["sk"] or 0)))
        rows = self.db.all(
            """SELECT a.activity_id::VARCHAR AS id, epoch(a.start_time_gmt)::BIGINT AS sk, a.start_time_local::DATE AS d
               FROM activity a WHERE a.deleted_at IS NULL AND NOT coalesce(a.is_manual_activity, false)
                 AND NOT EXISTS (SELECT 1 FROM raw_latest r WHERE r.object_type = 'activity_fit' AND r.object_key = a.activity_id::VARCHAR)"""
        )
        for r in rows:
            out.append(("activity_fit", r["id"], 25 + self._age_bump(r["d"] or today, today), int(r["sk"] or 0)))
        return out

    def _plan_snapshots(self, today: date) -> list[tuple[str, str, int, int]]:
        out = []
        now = datetime.now(UTC)
        for ds in SNAPSHOT_DATASETS:
            if ds.name in ("device_settings", "gear_stats", "workout", "course", "calendar_month", "gear", "gear_defaults", "active_training_plans", "calendar_events"):
                continue
            if ds.refresh is None:
                continue
            row = self.db.one("SELECT checked_at FROM raw_latest WHERE object_type = ? AND object_key = 'self'", [ds.name])
            if row is None or row["checked_at"] < now - ds.refresh:
                out.append((f"snap:{ds.name}", "self", ds.priority, 0))
        # keyed snapshots
        for name, key in (("gear", "self"), ("gear_defaults", "self")):
            ds = dataset(name)
            row = self.db.one("SELECT checked_at FROM raw_latest WHERE object_type = ? LIMIT 1", [name])
            if row is None or row["checked_at"] < now - (ds.refresh or timedelta(days=1)):
                out.append((f"snap:{name}", key, ds.priority, 0))
        row = self.db.one("SELECT checked_at FROM raw_latest WHERE object_type = 'active_training_plans' AND object_key = ?", [today.isoformat()])
        if row is None:
            out.append(("snap:active_training_plans", today.isoformat(), 30, 0))
        ck = f"{(today - timedelta(days=365)).isoformat()}_{(today + timedelta(days=365)).isoformat()}"
        row = self.db.one("SELECT checked_at FROM raw_latest WHERE object_type = 'calendar_events' AND object_key = ?", [ck])
        if row is None or row["checked_at"] < now - timedelta(days=1):
            out.append(("snap:calendar_events", ck, 30, 0))
        for r in self.db.all(
            """SELECT d.device_id::VARCHAR AS id FROM device d LEFT JOIN raw_latest r ON r.object_type = 'device_settings' AND r.object_key = d.device_id::VARCHAR
               WHERE NOT d.historical AND (r.object_key IS NULL OR r.checked_at < now() - INTERVAL 7 DAY)"""
        ):
            out.append(("snap:device_settings", r["id"], 30, 0))
        for r in self.db.all(
            """SELECT g.uuid FROM gear g LEFT JOIN raw_latest r ON r.object_type = 'gear_stats' AND r.object_key = g.uuid
               WHERE r.object_key IS NULL OR r.checked_at < now() - INTERVAL 1 DAY"""
        ):
            out.append(("snap:gear_stats", r["uuid"], 30, 0))
        for r in self.db.all(
            """SELECT w.workout_id::VARCHAR AS id FROM workout w LEFT JOIN raw_latest r ON r.object_type = 'workout' AND r.object_key = w.workout_id::VARCHAR
               WHERE r.object_key IS NULL OR w.updated_date > r.checked_at"""
        ):
            out.append(("snap:workout", r["id"], 30, 0))
        for r in self.db.all(
            """SELECT c.course_id::VARCHAR AS id FROM course c LEFT JOIN raw_latest r ON r.object_type = 'course' AND r.object_key = c.course_id::VARCHAR
               WHERE r.object_key IS NULL OR c.updated_date > r.checked_at"""
        ):
            out.append(("snap:course", r["id"], 35, 0))
        return out

    def _plan_reloads(self, today: date) -> int:
        """Days older than ~6 months whose intraday data is archived get an archive reload request."""
        cutoff = today - timedelta(days=ARCHIVE_AGE_DAYS)
        rows = self.db.all(
            """SELECT h.calendar_date AS d FROM heart_rate_day h
               JOIN daily_summary s USING (calendar_date)
               WHERE h.calendar_date <= ? AND h.sample_count = 0 AND coalesce(s.includes_wellness_data, false)
                 AND coalesce(s.total_steps, 0) + coalesce(s.sleeping_seconds, 0) > 0
                 AND NOT EXISTS (SELECT 1 FROM reload_request q WHERE q.calendar_date = h.calendar_date)
               ORDER BY h.calendar_date DESC""",
            [cutoff],
        )
        return self._enqueue_many([("reload", r["d"].isoformat(), 50, int(datetime(r["d"].year, r["d"].month, r["d"].day, tzinfo=UTC).timestamp())) for r in rows])

    # ------------------------------------------------------------- scheduler

    def schedule(self) -> None:
        """Queue the periodic tasks that are due (poll, window, listings, snapshots)."""
        now = datetime.now(UTC)

        def due(name: str, every: timedelta) -> bool:
            last = self.get_state(f"last:{name}")
            return last is None or datetime.fromisoformat(last) < now - every

        have_activities = int(self.db.scalar("SELECT count(*) FROM activity") or 0)
        have_profile = int(self.db.scalar("SELECT count(*) FROM user_profile") or 0)
        if not have_profile:
            for name in ("userprofile_settings", "user_profile_base", "personal_information", "user_settings"):
                self.enqueue(f"snap:{name}", "self", 10)
        if due("poll", timedelta(minutes=15)):
            self.enqueue("poll", "self", 10)
        if due("list_recent", timedelta(minutes=15)):
            self.enqueue("list_recent", "self", 10 if have_activities else 10)
        if due("list_full", timedelta(days=7)):
            self.enqueue("list_full", "self", 25 if have_activities else 10)
        if due("window", timedelta(hours=6)):
            self.enqueue("window", "self", 12)
        if due("plan", timedelta(hours=1)):
            self.plan()
            self.mark_ran("plan")

    # ------------------------------------------------------------- jobs

    def run_job(self, kind: str, key: str, priority: int) -> bool:
        """Run one job. Returns whether it changed the mirror."""
        cls = priority_class(priority)
        f = self.fetch
        if kind == "poll":
            r = f.snapshot(dataset("device_last_used"), priority=cls)
            self.mark_ran("poll")
            row = self.db.one("SELECT max(last_used_device_upload_time) AS t FROM device_last_used")
            latest = row["t"].isoformat() if row and row["t"] else None
            seen = self.get_state("device_upload_time")
            if latest and latest != seen:
                self.set_state("device_upload_time", latest)
                self.enqueue("window", "self", 10)
                self.enqueue("list_recent", "self", 10)
            return r.changed
        if kind == "list_recent":
            r = f.activities(priority=cls)
            self.mark_ran("list_recent")
            if r["changed"]:
                self.plan()
            return bool(r["changed"])
        if kind == "list_full":
            r = f.activities(priority=cls, complete=True)
            self.mark_ran("list_full")
            self.mark_ran("list_recent")
            self.plan()
            return bool(r["changed"] or r["deleted"])
        if kind == "window":
            today = self.today()
            changed = False
            for i in range(RECENT_WINDOW_DAYS):
                d = (today - timedelta(days=i)).isoformat()
                for ds in DAY_DATASETS:
                    changed |= f.day(ds, d, priority=cls).changed
            for ds in RANGE_DATASETS:
                s = (today - timedelta(days=RECENT_WINDOW_DAYS - 1)).isoformat()
                changed |= bool(f.range(ds, s, today.isoformat(), priority=cls))
            self.mark_ran("window")
            return changed
        if kind.startswith("snap:"):
            return f.snapshot(dataset(kind[5:]), key, priority=cls).changed
        if kind.startswith("day:"):
            return f.day(dataset(kind[4:]), key, priority=cls).changed
        if kind.startswith("range:"):
            s, e = key.split("_")
            return bool(f.range(dataset(kind[6:]), s, e, priority=cls))
        if kind.startswith("act:"):
            return f.activity_sub(dataset(kind[4:]), key, priority=cls).changed
        if kind == "activity_fit":
            return f.activity_fit(key, priority=cls).changed
        if kind == "wellness_fit":
            return f.wellness_fit(key, priority=cls).changed
        if kind == "reload":
            if f.reloads_last_24h() >= RELOADS_PER_DAY:
                slot = f.next_reload_slot() or (datetime.now(UTC) + timedelta(hours=1))
                self.db.run("UPDATE sync_job SET not_before = ? WHERE kind = 'reload'", [slot])
                raise _Deferred(f"reload quota used; next slot {slot.isoformat()}")
            r = f.request_reload(key, priority=cls)
            status = (r.get("status") or "").upper()
            if status == "DENIED":
                self.db.run("UPDATE sync_job SET not_before = now() + INTERVAL 24 HOUR WHERE kind = 'reload'")
            self.enqueue("refetch_day", key, 50, 0, datetime.now(UTC) + REFETCH_DELAY)
            return True
        if kind == "refetch_day":
            changed = False
            for ds in INTRADAY_DATASETS:
                changed |= f.day(ds, key, priority=cls).changed
            changed |= f.wellness_fit(key, priority=cls).changed
            still_empty = self.db.scalar("SELECT coalesce(sample_count, 0) = 0 FROM heart_rate_day WHERE calendar_date = ?", [key])
            self.db.run(
                "UPDATE reload_request SET refetched_at = now(), status = CASE WHEN ? THEN 'empty_after_refetch' ELSE status END WHERE calendar_date = ? AND refetched_at IS NULL",
                [bool(still_empty), key],
            )
            return changed
        raise ValueError(f"Unknown job kind {kind}")

    def log(self, kind: str, key: str, outcome: str, detail: str | None = None) -> None:
        self.db.run("INSERT INTO sync_log (kind, key, outcome, detail) VALUES (?, ?, ?, ?)", [kind, key, outcome, detail])

    def step(self) -> str:
        """Run the next job. Returns worked, idle or waiting."""
        if self.client.state.auth_error:
            return "waiting"
        if self.client.backoff_remaining() > 0:
            return "waiting"
        job = self.next_job()
        if job is None:
            return "idle"
        kind, key = job["kind"], job["key"]
        self.current = {"kind": kind, "key": key, "started_at": datetime.now(UTC).isoformat()}
        try:
            changed = self.run_job(kind, key, int(job["priority"]))
            self.db.run("DELETE FROM sync_job WHERE kind = ? AND key = ?", [kind, key])
            self.log(kind, key, "changed" if changed else "ok")
            if changed and self.on_change:
                self.on_change()
        except _Deferred as err:
            self.log(kind, key, "deferred", str(err)[:500])
            return "waiting"
        except RateLimited as err:
            self.log(kind, key, "rate_limited", str(err)[:500])
            return "waiting"
        except AuthError as err:
            self.log(kind, key, "auth_error", str(err)[:500])
            return "waiting"
        except Exception as err:  # noqa: BLE001
            attempts = int(job["attempts"]) + 1
            backoff_min = min(2**attempts, 24 * 60)
            self.db.run(
                "UPDATE sync_job SET attempts = ?, last_error = ?, not_before = now() + to_minutes(?::BIGINT) WHERE kind = ? AND key = ?",
                [attempts, str(err)[:1000], backoff_min, kind, key],
            )
            self.log(kind, key, "error", str(err)[:1000])
            log.warning("sync: %s %s failed (attempt %d): %s", kind, key, attempts, err)
        finally:
            self.current = None
        return "worked"

    # ------------------------------------------------------------- worker

    def _wait(self, seconds: float) -> None:
        if self._sleep is not None:
            self._sleep(seconds)
            return
        self._wake.wait(seconds)
        self._wake.clear()

    def run_forever(self) -> None:
        last_schedule = 0.0
        last_prune = 0.0
        while self._running:
            try:
                if time.monotonic() - last_schedule > MINUTE:
                    self.schedule()
                    last_schedule = time.monotonic()
                if time.monotonic() - last_prune > 24 * HOUR:
                    self.db.run("DELETE FROM request_log WHERE observed_at < now() - INTERVAL 30 DAY")
                    self.db.run("DELETE FROM sync_log WHERE logged_at < now() - INTERVAL 30 DAY")
                    last_prune = time.monotonic()
                result = self.step()
                if result == "idle":
                    self._wait(MINUTE)
                elif result == "waiting":
                    self._wait(min(MINUTE, max(5.0, self.client.backoff_remaining())))
            except Exception:  # noqa: BLE001
                log.exception("sync: worker error")
                self._wait(MINUTE)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self.run_forever, name="garmin-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=30)

    # ------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        queue = self.db.all(
            """SELECT kind, count(*)::INTEGER AS pending, min(priority)::INTEGER AS top_priority,
                 count(*) FILTER (WHERE attempts > 0)::INTEGER AS retrying,
                 any_value(last_error) FILTER (WHERE last_error IS NOT NULL) AS example_error,
                 min(not_before)::VARCHAR AS next_not_before
               FROM sync_job GROUP BY kind ORDER BY min(priority), kind"""
        )
        last_runs = {r["name"].removeprefix("last:"): r["value"] for r in self.db.all("SELECT name, value FROM sync_state WHERE name LIKE 'last:%' ORDER BY name")}
        outcomes = self.db.all("SELECT outcome, count(*)::INTEGER AS n FROM sync_log WHERE logged_at > now() - INTERVAL 1 DAY GROUP BY outcome ORDER BY outcome")
        errors = self.db.all("SELECT logged_at::VARCHAR AS logged_at, kind, key, detail FROM sync_log WHERE outcome IN ('error', 'auth_error') ORDER BY logged_at DESC LIMIT 5")
        coverage = self.db.one(
            """SELECT
                 (SELECT count(*) FROM activity WHERE deleted_at IS NULL)::INTEGER AS activities,
                 (SELECT count(*) FROM activity WHERE detail_fetched_at IS NOT NULL)::INTEGER AS activities_with_detail,
                 (SELECT count(*) FROM raw_latest WHERE object_type = 'activity_fit' AND status = 'ok')::INTEGER AS activities_with_fit,
                 (SELECT min(start_time_local)::VARCHAR FROM activity) AS oldest_activity,
                 (SELECT max(start_time_local)::VARCHAR FROM activity) AS newest_activity,
                 (SELECT count(*) FROM daily_summary)::INTEGER AS days_with_summary,
                 (SELECT count(*) FROM heart_rate_day WHERE sample_count > 0)::INTEGER AS days_with_intraday_hr,
                 (SELECT count(*) FROM heart_rate_day WHERE sample_count = 0)::INTEGER AS days_intraday_archived,
                 (SELECT count(*) FROM sleep)::INTEGER AS nights_with_sleep,
                 (SELECT count(*) FROM raw_latest WHERE object_type = 'wellness_fit' AND status = 'ok')::INTEGER AS days_with_wellness_fit,
                 (SELECT min(calendar_date)::VARCHAR FROM daily_summary) AS oldest_day,
                 (SELECT max(calendar_date)::VARCHAR FROM daily_summary) AS newest_day"""
        )
        per_type = self.db.all(
            "SELECT object_type, count(*) FILTER (WHERE status = 'ok')::INTEGER AS ok, count(*) FILTER (WHERE status <> 'ok')::INTEGER AS empty, max(checked_at)::VARCHAR AS last_checked FROM raw_latest GROUP BY object_type ORDER BY object_type"
        )
        reloads = self.db.one(
            """SELECT count(*) FILTER (WHERE requested_at > now() - INTERVAL 24 HOUR)::INTEGER AS used_last_24h,
                      count(*)::INTEGER AS total, count(*) FILTER (WHERE refetched_at IS NOT NULL)::INTEGER AS refetched,
                      count(*) FILTER (WHERE status = 'empty_after_refetch')::INTEGER AS still_empty,
                      max(requested_at)::VARCHAR AS last_requested FROM reload_request"""
        )
        requests_24h = self.db.one("SELECT count(*)::INTEGER AS n, count(*) FILTER (WHERE status >= 400)::INTEGER AS failed FROM request_log WHERE observed_at > now() - INTERVAL 1 DAY")
        hs = self.history_start()
        return {
            "working_on": self.current,
            "history_start": hs.isoformat() if hs else None,
            "today": self.today().isoformat(),
            "time_zone": self.time_zone(),
            "coverage": coverage,
            "datasets": per_type,
            "queue": queue,
            "last_runs": last_runs,
            "outcomes_last_24h": outcomes,
            "recent_errors": errors,
            "reload_quota": {**(reloads or {}), "limit_per_24h": RELOADS_PER_DAY},
            "requests_last_24h": requests_24h,
            "pacing": self.client.status(),
        }


class _Deferred(Exception):
    pass

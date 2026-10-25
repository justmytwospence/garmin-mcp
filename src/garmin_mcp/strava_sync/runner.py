"""Runs the Strava sync: reads both sides, plans with reconcile.py, applies the plan
through the paced Garmin client, and keeps the ledger (strava_link) and an audit trail
(sync_log, kind strava_sync).

The engine runs it every STRAVA_SYNC_INTERVAL_MINUTES as the job strava_sync:self; the
tools run it on demand. A per-run cap bounds what one run can change; above the cap
for deletions nothing is deleted and the run says so, so a bad snapshot can never
empty the course library. strava_sync_apply lifts the caps (and dry-run) for one run.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ..garmin.client import NotFound, Priority
from ..garmin.datasets import dataset
from .config import StravaSyncConfig
from .reconcile import CoursePlan, GarminActivity, GarminCourse, TitleLink, TitlePlan, plan_courses, plan_titles
from .source import SnapshotUnavailable, StravaData, StravaSnapshot

if TYPE_CHECKING:
    from ..db.database import Database
    from ..sync.engine import SyncEngine

log = logging.getLogger("garmin_mcp.strava_sync")

SINCE_STATE = "strava_sync:titles_since"
LAST_STATE = "strava_sync:last_run"
ACTIVITY_PATH = "/activity-service/activity"
COURSE_PATH = "/course-service/course"


@dataclass
class Planned:
    data: StravaData | None
    titles: TitlePlan | None
    courses: CoursePlan | None
    problems: list[str]


class StravaSync:
    def __init__(self, db: Database, engine: SyncEngine, config: StravaSyncConfig, *, source: StravaSnapshot | None = None, now: Callable[[], datetime] | None = None) -> None:
        self.db = db
        self.engine = engine
        self.config = config
        self.source = source or StravaSnapshot(config)
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()

    # ------------------------------------------------------------- inputs

    def titles_since(self) -> date | None:
        if not self.config.titles:
            return None
        if self.config.titles_since:
            return self.config.titles_since
        stored = self.engine.get_state(SINCE_STATE)
        if stored:
            return date.fromisoformat(stored)
        today = self._now().date()
        self.engine.set_state(SINCE_STATE, today.isoformat())
        return today

    def _garmin_activities(self, since: datetime) -> list[GarminActivity]:
        rows = self.db.all(
            "SELECT activity_id, activity_name, start_time_gmt FROM activity WHERE deleted_at IS NULL AND start_time_gmt >= ? ORDER BY start_time_gmt",
            [since - timedelta(days=1)],
        )
        return [GarminActivity(int(r["activity_id"]), r["activity_name"], r["start_time_gmt"]) for r in rows if r["start_time_gmt"] is not None]

    def _garmin_courses(self) -> list[GarminCourse]:
        rows = self.db.all(
            "SELECT course_id, course_name, distance_in_meters, start_latitude, start_longitude, extra->>'consumer' AS consumer, created_date FROM course ORDER BY course_id"
        )
        return [
            GarminCourse(
                int(r["course_id"]),
                r["course_name"] or "",
                r["distance_in_meters"],
                (r["start_latitude"], r["start_longitude"]) if r["start_latitude"] is not None and r["start_longitude"] is not None else None,
                r["consumer"],
                r["created_date"],
            )
            for r in rows
        ]

    def _title_links(self) -> dict[int, TitleLink]:
        rows = self.db.all("SELECT strava_id, garmin_id, strava_name, garmin_name_set FROM strava_link WHERE kind = 'activity'")
        return {int(r["strava_id"]): TitleLink(int(r["strava_id"]), int(r["garmin_id"]), r["strava_name"], r["garmin_name_set"]) for r in rows}

    def _course_links(self) -> dict[int, int]:
        return {int(r["garmin_id"]): int(r["strava_id"]) for r in self.db.all("SELECT strava_id, garmin_id FROM strava_link WHERE kind = 'course'")}

    # ------------------------------------------------------------- planning

    def compute(self, priority: Priority, *, refresh: bool = True) -> Planned:
        problems: list[str] = []
        since_day = self.titles_since()
        since = datetime(since_day.year, since_day.month, since_day.day, tzinfo=UTC) if since_day else None
        try:
            data = self.source.read(since)
        except SnapshotUnavailable as err:
            return Planned(None, None, None, [str(err)])
        titles = None
        if self.config.titles and since is not None:
            titles = plan_titles(data.activities, self._garmin_activities(since), self._title_links())
        courses = None
        if self.config.courses:
            age = self._now() - data.routes_checked_at if data.routes_checked_at else None
            if age is None:
                problems.append("courses skipped: the Strava snapshot has no route list yet")
            elif age > self.config.max_age:
                problems.append(f"courses skipped: Strava's route list in the snapshot is {age.total_seconds() / 3600:.0f} h old (limit {self.config.max_age.total_seconds() / 3600:.0f} h)")
            else:
                if refresh:
                    self.engine.fetch.snapshot(dataset("courses"), priority=priority)
                courses = plan_courses(self._garmin_courses(), data.starred_routes, self.config.course_consumers, self._course_links())
                if courses.considered and not data.starred_routes:
                    problems.append("course deletions refused: the snapshot lists no starred routes at all, which would delete every course Strava made")
        return Planned(data, titles, courses, problems)

    def plan(self, *, priority: Priority = "interactive") -> dict[str, Any]:
        """What a run would do now, with no write."""
        with self._lock:
            p = self.compute(priority)
        return self._render(p)

    def _render(self, p: Planned) -> dict[str, Any]:
        out: dict[str, Any] = {"settings": self.config.describe(), "problems": p.problems}
        if p.data is not None:
            out["snapshot"] = {
                "route_list_checked_at": p.data.routes_checked_at.isoformat() if p.data.routes_checked_at else None,
                "activity_list_checked_at": p.data.activities_checked_at.isoformat() if p.data.activities_checked_at else None,
            }
        since = self.titles_since()
        if p.titles is not None:
            out["titles"] = {
                "since": since.isoformat() if since else None,
                "renames": [r.as_dict() for r in p.titles.renames],
                "already_in_sync": len(p.titles.adopt),
                "conflicts": [r.as_dict() for r in p.titles.conflicts],
                "skipped": p.titles.skipped,
            }
        if p.courses is not None:
            out["courses"] = {
                "mode": self.config.courses_mode,
                "strava_courses": p.courses.considered,
                "courses_from_other_sources": p.courses.other_sources,
                "starred_routes": p.courses.starred_routes,
                "starred_routes_without_a_course": p.courses.starred_routes_without_course,
                "keep": [d.as_dict() for d in p.courses.keep],
                "delete": [d.as_dict() for d in p.courses.delete],
            }
        return out

    # ------------------------------------------------------------- applying

    def run(self, *, priority: Priority = "incremental", manual: set[str] | None = None) -> dict[str, Any]:
        """One run. `manual` names the parts run on request: no per-run cap, and course deletions even in dry-run mode."""
        manual = manual or set()
        with self._lock:
            p = self.compute(priority)
            summary: dict[str, Any] = {"at": self._now().isoformat(), "problems": list(p.problems), "manual": sorted(manual)}
            if p.titles is not None and (not manual or "titles" in manual):
                summary["titles"] = self._apply_titles(p.titles, priority, capped="titles" not in manual)
            if p.courses is not None and (not manual or "courses" in manual):
                summary["courses"] = self._apply_courses(p.courses, priority, forced="courses" in manual, problems=p.problems)
            self.engine.set_state(LAST_STATE, json.dumps(summary, default=str))
        return summary

    def _log(self, key: str, outcome: str, detail: dict[str, Any]) -> None:
        self.engine.log("strava_sync", key, outcome, json.dumps(detail, ensure_ascii=False, default=str)[:1000])

    def _link(self, strava_id: int, garmin_id: int, strava_name: str | None, garmin_name_set: str | None, decided: str) -> None:
        with self.db.transaction() as tx:
            tx.run("DELETE FROM strava_link WHERE kind = 'activity' AND strava_id = ?", [strava_id])
            tx.run(
                "INSERT INTO strava_link (kind, strava_id, garmin_id, strava_name, garmin_name_set, decided, updated_at) VALUES ('activity', ?, ?, ?, ?, ?, now())",
                [strava_id, garmin_id, strava_name, garmin_name_set, decided],
            )

    def _current_name(self, garmin_id: int, priority: Priority) -> str | None:
        """The activity's name on Garmin right now (fetched, so a hand edit since the last mirror refresh is seen)."""
        self.engine.fetch.activity_sub(dataset("activity"), str(garmin_id), priority=priority)
        row = self.db.one("SELECT status, body->>'activityName' AS name FROM raw_current WHERE object_type = 'activity' AND object_key = ?", [str(garmin_id)])
        if row is None or row["status"] != "ok":
            raise NotFound(f"activity {garmin_id} not found", 404)
        return row["name"]

    def _apply_titles(self, plan: TitlePlan, priority: Priority, *, capped: bool) -> dict[str, Any]:
        links = self._title_links()
        renamed: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for r in plan.adopt:
            self._link(r.strava_id, r.garmin_id, r.strava_name, r.strava_name, "adopted")
        for r in plan.conflicts:
            self._link(r.strava_id, r.garmin_id, r.strava_name, links[r.strava_id].garmin_name_set if r.strava_id in links else None, "conflict")
            self._log(str(r.garmin_id), "conflict", {"strava_id": r.strava_id, "garmin_name": r.garmin_name, "strava_name": r.strava_name})
            conflicts.append(r.as_dict())
        todo = plan.renames[: self.config.max_renames] if capped else plan.renames
        for r in todo:
            try:
                current = self._current_name(r.garmin_id, priority)
            except NotFound:
                self._log(str(r.garmin_id), "skipped", {"strava_id": r.strava_id, "reason": "Garmin activity no longer exists"})
                continue
            link = links.get(r.strava_id)
            if (current or "").strip() == r.strava_name:
                self._link(r.strava_id, r.garmin_id, r.strava_name, r.strava_name, "adopted")
                continue
            if link is not None and link.garmin_id == r.garmin_id and link.garmin_name_set is not None and (current or "").strip() != link.garmin_name_set:
                self._link(r.strava_id, r.garmin_id, r.strava_name, link.garmin_name_set, "conflict")
                self._log(str(r.garmin_id), "conflict", {"strava_id": r.strava_id, "garmin_name": current, "strava_name": r.strava_name})
                conflicts.append({**r.as_dict(), "garmin_name": current})
                continue
            self.engine.client.send("PUT", f"{ACTIVITY_PATH}/{r.garmin_id}", json={"activityId": r.garmin_id, "activityName": r.strava_name}, priority=priority)
            self._link(r.strava_id, r.garmin_id, r.strava_name, r.strava_name, "renamed")
            self._log(str(r.garmin_id), "renamed", {"strava_id": r.strava_id, "from": current, "to": r.strava_name})
            self.engine.enqueue("act:activity", str(r.garmin_id), 12)
            renamed.append({**r.as_dict(), "garmin_name": current})
        if renamed:
            self.engine.enqueue("list_recent", "self", 12)
        return {"renamed": renamed, "conflicts": conflicts, "adopted": len(plan.adopt), "deferred": len(plan.renames) - len(todo), "skipped": plan.skipped}

    def _apply_courses(self, plan: CoursePlan, priority: Priority, *, forced: bool, problems: list[str]) -> dict[str, Any]:
        with self.db.transaction() as tx:
            tx.run("DELETE FROM strava_link WHERE kind = 'course'")
            for d in plan.keep:
                tx.run(
                    "INSERT INTO strava_link (kind, strava_id, garmin_id, strava_name, decided, updated_at) VALUES ('course', ?, ?, ?, 'kept', now())",
                    [d.route_id, d.course_id, d.route_name],
                )
        out: dict[str, Any] = {"mode": self.config.courses_mode, "kept": len(plan.keep), "orphaned": len(plan.delete), "deleted": []}
        if not plan.delete:
            return out
        if any(p.startswith("course deletions refused") for p in problems):
            out["held"] = "refused: see problems"
            return out
        if self.config.courses_mode == "dry-run" and not forced:
            out["held"] = "dry-run: nothing deleted (set STRAVA_SYNC_COURSES=on, or run strava_sync_apply)"
            return out
        if len(plan.delete) > self.config.max_deletes and not forced:
            out["held"] = f"over the cap: {len(plan.delete)} deletions exceed STRAVA_SYNC_MAX_DELETES={self.config.max_deletes}; nothing deleted (review strava_sync_plan, then run strava_sync_apply)"
            self._log("courses", "held", {"orphaned": len(plan.delete), "cap": self.config.max_deletes})
            return out
        for d in plan.delete:
            try:
                self.engine.client.send("DELETE", f"{COURSE_PATH}/{d.course_id}", priority=priority)
            except NotFound:
                pass
            self._log(str(d.course_id), "deleted_course", d.as_dict())
            out["deleted"].append(d.as_dict())
        self.engine.fetch.snapshot(dataset("courses"), priority=priority)
        return out

    # ------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        last = self.engine.get_state(LAST_STATE)
        counts = self.db.all("SELECT kind, decided, count(*)::INTEGER AS n FROM strava_link GROUP BY ALL ORDER BY ALL")
        recent = self.db.all(
            "SELECT logged_at::VARCHAR AS at, key, outcome, detail FROM sync_log WHERE kind = 'strava_sync' ORDER BY logged_at DESC LIMIT 20"
        )
        since = self.titles_since()
        return {
            "settings": self.config.describe(),
            "titles_since": since.isoformat() if since else None,
            "last_run": json.loads(last) if last else None,
            "ledger": counts,
            "recent_actions": recent,
        }

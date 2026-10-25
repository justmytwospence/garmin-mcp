"""The sync's decisions, as pure functions over plain data (no I/O).

Titles: a Strava activity is paired with the one Garmin activity that started within
MATCH_SECONDS of it (Strava's external_id is not the Garmin activity id). Strava's
name wins whenever it changes, except Strava's automatic names ("Morning Run"), and
except when the Garmin name was edited by hand after the sync last set it.

Courses: only courses Strava's own route sync created (recognised by Garmin's consumer
id) are considered. One is kept while it matches a starred Strava route (a link
recorded earlier, the exact name, or the same length and start); of several matching
the same route the newest is kept. Everything else is orphaned.
"""

from __future__ import annotations

import math
import re
from bisect import bisect_left
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .source import StravaActivity, StravaRoute

MATCH_SECONDS = 60
DISTANCE_TOLERANCE = 0.02
START_TOLERANCE_M = 250.0

# Strava's automatic activity names: a time of day and the sport ("Afternoon Trail Run").
DEFAULT_NAME = re.compile(r"^(Morning|Lunch|Afternoon|Evening|Night) \S.*$")


def is_default_name(name: str) -> bool:
    return bool(DEFAULT_NAME.match(name.strip()))


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


# ----------------------------------------------------------------- titles


@dataclass(frozen=True)
class GarminActivity:
    id: int
    name: str | None
    start: datetime


@dataclass(frozen=True)
class TitleLink:
    """What the ledger remembers for one Strava activity."""

    strava_id: int
    garmin_id: int
    strava_name: str | None
    garmin_name_set: str | None


@dataclass
class Rename:
    strava_id: int
    garmin_id: int
    garmin_name: str | None
    strava_name: str
    first: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TitlePlan:
    renames: list[Rename] = field(default_factory=list)
    # In sync already: recorded in the ledger without a write.
    adopt: list[Rename] = field(default_factory=list)
    # Garmin name edited by hand since the sync set it: left alone until Strava changes again.
    conflicts: list[Rename] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def pair_activities(strava: list[StravaActivity], garmin: list[GarminActivity], plan: TitlePlan) -> list[tuple[StravaActivity, GarminActivity]]:
    """Pairs of activities that started within MATCH_SECONDS of each other, one to one."""
    ordered = sorted(garmin, key=lambda g: g.start)
    starts = [g.start.timestamp() for g in ordered]
    candidates: dict[int, list[GarminActivity]] = {}
    claims: dict[int, int] = {}
    for s in strava:
        t = s.start.timestamp()
        i = bisect_left(starts, t - MATCH_SECONDS)
        found = []
        while i < len(ordered) and starts[i] <= t + MATCH_SECONDS:
            found.append(ordered[i])
            i += 1
        candidates[s.id] = found
        for g in found:
            claims[g.id] = claims.get(g.id, 0) + 1
    pairs = []
    for s in strava:
        found = candidates[s.id]
        if not found:
            plan.skip("no_garmin_activity")
        elif len(found) > 1 or claims[found[0].id] > 1:
            plan.skip("ambiguous_match")
        else:
            pairs.append((s, found[0]))
    return pairs


def plan_titles(strava: list[StravaActivity], garmin: list[GarminActivity], links: dict[int, TitleLink]) -> TitlePlan:
    plan = TitlePlan()
    for s, g in pair_activities(strava, garmin, plan):
        name = s.name.strip()
        link = links.get(s.id)
        if link is not None and link.garmin_id != g.id:
            link = None  # the pairing changed (an activity was deleted and re-uploaded): start over
        if not name:
            plan.skip("empty_strava_name")
            continue
        if is_default_name(name):
            plan.skip("strava_default_name")
            continue
        if link is not None and link.strava_name == name:
            plan.skip("unchanged")
            continue
        r = Rename(s.id, g.id, g.name, name, first=link is None)
        if (g.name or "").strip() == name:
            plan.adopt.append(r)
        elif link is not None and link.garmin_name_set is not None and (g.name or "").strip() != link.garmin_name_set:
            plan.conflicts.append(r)
        else:
            plan.renames.append(r)
    return plan


# ----------------------------------------------------------------- courses


@dataclass(frozen=True)
class GarminCourse:
    id: int
    name: str
    distance: float | None
    start: tuple[float, float] | None
    consumer: str | None
    created: datetime | None


@dataclass
class CourseDecision:
    course_id: int
    course_name: str
    distance_m: float | None
    created: str | None
    route_id: int | None = None
    route_name: str | None = None
    how: str | None = None  # link, name, geometry
    reason: str | None = None  # for deletions: orphan or duplicate

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class CoursePlan:
    keep: list[CourseDecision] = field(default_factory=list)
    delete: list[CourseDecision] = field(default_factory=list)
    considered: int = 0
    other_sources: int = 0
    starred_routes: int = 0
    starred_routes_without_course: int = 0


HOW_RANK = {"geometry": 0, "name": 1, "link": 1}


def _close(c: GarminCourse, r: StravaRoute) -> bool:
    if c.distance is None or r.distance is None or c.start is None or r.start is None or r.distance <= 0:
        return False
    return abs(c.distance - r.distance) / r.distance <= DISTANCE_TOLERANCE and haversine_m(c.start, r.start) <= START_TOLERANCE_M


def _match(c: GarminCourse, routes: list[StravaRoute], by_id: dict[int, StravaRoute], links: dict[int, int]) -> tuple[StravaRoute, str] | None:
    linked = links.get(c.id)
    if linked is not None and linked in by_id:
        return by_id[linked], "link"
    named = [r for r in routes if r.name.strip() == c.name.strip()]
    if named:
        if len(named) > 1 and c.distance is not None:
            named.sort(key=lambda r: abs((r.distance or 0) - c.distance))
        return named[0], "name"
    close = [r for r in routes if _close(c, r)]
    if close:
        close.sort(key=lambda r: haversine_m(c.start, r.start) if c.start and r.start else 0)  # type: ignore[arg-type]
        return close[0], "geometry"
    return None


def plan_courses(courses: list[GarminCourse], routes: list[StravaRoute], consumers: frozenset[str], links: dict[int, int] | None = None) -> CoursePlan:
    """links: course id -> Strava route id, as recorded by earlier runs."""
    links = links or {}
    plan = CoursePlan(starred_routes=len(routes))
    by_id = {r.id: r for r in routes}
    matched: dict[int, list[tuple[GarminCourse, StravaRoute, str]]] = {}
    for c in courses:
        if c.consumer not in consumers:
            plan.other_sources += 1
            continue
        plan.considered += 1
        m = _match(c, routes, by_id, links)
        if m is None:
            plan.delete.append(_decision(c, reason="orphan: no starred Strava route has this name, or this length and start"))
        else:
            matched.setdefault(m[0].id, []).append((c, m[0], m[1]))
    for group in matched.values():
        # A course matched by name or by an earlier link beats one matched by geometry; then the newest copy wins
        # (Strava makes a new course, with the same name, when a route is edited).
        group.sort(key=lambda x: (HOW_RANK[x[2]], x[0].created.timestamp() if x[0].created else 0.0, x[0].id), reverse=True)
        newest = group[0]
        plan.keep.append(_decision(newest[0], newest[1], newest[2]))
        for c, r, how in group[1:]:
            plan.delete.append(_decision(c, r, how, reason=f"duplicate: course {newest[0].id} is a newer copy of the same Strava route"))
    plan.starred_routes_without_course = len(routes) - len(matched)
    plan.keep.sort(key=lambda d: d.course_id)
    plan.delete.sort(key=lambda d: d.course_id)
    return plan


def _decision(c: GarminCourse, r: StravaRoute | None = None, how: str | None = None, reason: str | None = None) -> CourseDecision:
    return CourseDecision(
        c.id,
        c.name,
        c.distance,
        c.created.isoformat() if c.created else None,
        r.id if r else None,
        r.name if r else None,
        how,
        reason,
    )

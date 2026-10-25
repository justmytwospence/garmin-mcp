"""Settings of the optional Strava sync, read from the environment.

The feature is off unless STRAVA_SYNC names at least one part (titles, courses). It
reads Strava data from strava-mcp, never from Strava itself, so it needs no Strava
credentials of its own: from its live export (STRAVA_EXPORT_URL, GET
/export/garmin-sync, fresh to the minute) or from its snapshot (STRAVA_SNAPSHOT, a
file path or an http(s) URL, republished at most hourly).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

PARTS = ("titles", "courses")

# The consumer id Garmin records on courses that Strava's own route sync created.
STRAVA_COURSE_CONSUMERS = frozenset({"3527224f-080a-4b66-8b72-0a2c3e111af0"})

CoursesMode = Literal["dry-run", "on"]


@dataclass(frozen=True)
class StravaSyncConfig:
    titles: bool
    courses: bool
    snapshot: str | None
    token: str | None = None
    export_url: str | None = None
    courses_mode: CoursesMode = "dry-run"
    titles_since: date | None = None
    max_renames: int = 20
    max_deletes: int = 10
    max_age: timedelta = timedelta(hours=48)
    interval: timedelta = timedelta(minutes=2)
    course_consumers: frozenset[str] = field(default_factory=lambda: STRAVA_COURSE_CONSUMERS)

    @property
    def parts(self) -> list[str]:
        return [p for p in PARTS if getattr(self, p)]

    @property
    def is_url(self) -> bool:
        return bool(self.snapshot and self.snapshot.startswith(("http://", "https://")))

    @property
    def source(self) -> str:
        return self.export_url or self.snapshot or ""

    def describe(self) -> dict[str, object]:
        return {
            "parts": self.parts,
            "source": "export" if self.export_url else "snapshot",
            "location": self.source,
            "courses_mode": self.courses_mode,
            "titles_since": self.titles_since.isoformat() if self.titles_since else None,
            "max_renames_per_run": self.max_renames,
            "max_deletes_per_run": self.max_deletes,
            "max_route_list_age_hours": self.max_age.total_seconds() / 3600,
            "interval_minutes": self.interval.total_seconds() / 60,
        }

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> StravaSyncConfig | None:
        """None when STRAVA_SYNC is unset or off; ValueError on a setting that cannot work."""
        env = os.environ if env is None else env
        raw = (env.get("STRAVA_SYNC") or "").strip().lower()
        if raw in ("", "off", "0", "false", "no"):
            return None
        parts = {p.strip() for p in raw.split(",") if p.strip()}
        if parts in ({"on"}, {"all"}):
            parts = set(PARTS)
        unknown = parts - set(PARTS)
        if unknown:
            raise ValueError(f"STRAVA_SYNC: unknown part(s) {', '.join(sorted(unknown))}; use a comma list of {', '.join(PARTS)}")
        snapshot = (env.get("STRAVA_SNAPSHOT") or "").strip() or None
        export_url = (env.get("STRAVA_EXPORT_URL") or "").strip() or None
        if not snapshot and not export_url:
            raise ValueError("STRAVA_SYNC is set but neither STRAVA_EXPORT_URL (strava-mcp's /export/garmin-sync) nor STRAVA_SNAPSHOT (its snapshot) is")
        if snapshot and export_url:
            raise ValueError("set one of STRAVA_EXPORT_URL and STRAVA_SNAPSHOT, not both")
        if export_url and not export_url.startswith(("http://", "https://")):
            raise ValueError(f"STRAVA_EXPORT_URL must be an http(s) URL, got {export_url!r}")
        mode = (env.get("STRAVA_SYNC_COURSES") or "dry-run").strip().lower()
        if mode not in ("dry-run", "on"):
            raise ValueError(f'STRAVA_SYNC_COURSES must be "dry-run" or "on", got "{mode}"')
        since_raw = (env.get("STRAVA_SYNC_TITLES_SINCE") or "").strip()
        try:
            since = date.fromisoformat(since_raw) if since_raw else None
        except ValueError as err:
            raise ValueError(f"STRAVA_SYNC_TITLES_SINCE must be a date (YYYY-MM-DD), got {since_raw!r}") from err
        consumers = {c.strip() for c in (env.get("STRAVA_COURSE_CONSUMERS") or "").split(",") if c.strip()}
        return cls(
            titles="titles" in parts,
            courses="courses" in parts,
            snapshot=snapshot,
            export_url=export_url,
            token=(env.get("STRAVA_SNAPSHOT_TOKEN") or "").strip() or None,
            courses_mode=mode,  # type: ignore[arg-type]
            titles_since=since,
            max_renames=_int(env, "STRAVA_SYNC_MAX_RENAMES", 20),
            max_deletes=_int(env, "STRAVA_SYNC_MAX_DELETES", 10),
            max_age=timedelta(hours=_int(env, "STRAVA_SYNC_MAX_AGE_HOURS", 48)),
            interval=timedelta(minutes=max(1, _int(env, "STRAVA_SYNC_INTERVAL_MINUTES", 2))),
            course_consumers=frozenset(consumers) or STRAVA_COURSE_CONSUMERS,
        )


def _int(env: dict[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as err:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from err
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value

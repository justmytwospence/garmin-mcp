"""Strava data for the sync, read from a strava-mcp snapshot.

Each read opens its own in-memory DuckDB instance and attaches the snapshot read-only,
so the mirror's connection (and its memory limit and file-access lockdown) is never
involved, and garmin_query never sees Strava data. strava-mcp replaces the snapshot
by renaming a new file over it, so it is attached afresh on every read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import duckdb

from .config import StravaSyncConfig


class SnapshotUnavailable(Exception):
    """The snapshot cannot be read or is not a strava-mcp snapshot."""


@dataclass(frozen=True)
class StravaRoute:
    id: int
    name: str
    distance: float | None
    start: tuple[float, float] | None


@dataclass(frozen=True)
class StravaActivity:
    id: int
    name: str
    start: datetime
    elapsed: int | None


@dataclass(frozen=True)
class StravaData:
    routes_checked_at: datetime | None
    activities_checked_at: datetime | None
    starred_routes: list[StravaRoute]
    activities: list[StravaActivity]


def polyline_start(encoded: str | None) -> tuple[float, float] | None:
    """The first point of a Google-encoded polyline (only the first two values are decoded)."""
    if not encoded:
        return None
    values: list[int] = []
    i = 0
    try:
        while len(values) < 2:
            result = shift = 0
            while True:
                b = ord(encoded[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            values.append(~(result >> 1) if result & 1 else result >> 1)
    except IndexError:
        return None
    return values[0] / 1e5, values[1] / 1e5


class StravaSnapshot:
    def __init__(self, config: StravaSyncConfig) -> None:
        self.config = config

    def _connect(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(":memory:")
        con.execute("SET GLOBAL TimeZone = 'UTC'")
        con.execute("SET memory_limit = '256MB'")
        con.execute("SET threads = 1")
        src = self.config.snapshot
        try:
            if self.config.is_url:
                con.execute("INSTALL httpfs")
                con.execute("LOAD httpfs")
                if self.config.token:
                    header = f"Bearer {self.config.token}".replace("'", "''")
                    con.execute(f"CREATE SECRET strava_snapshot (TYPE http, EXTRA_HTTP_HEADERS MAP {{'Authorization': '{header}'}})")
            elif not Path(src).is_file():
                raise SnapshotUnavailable(f"no Strava snapshot at {src}")
            con.execute(f"ATTACH '{src.replace(chr(39), chr(39) * 2)}' AS strava (READ_ONLY)")
        except duckdb.Error as err:
            con.close()
            raise SnapshotUnavailable(f"cannot attach the Strava snapshot {src}: {err}") from err
        except SnapshotUnavailable:
            con.close()
            raise
        return con

    def read(self, since: datetime | None) -> StravaData:
        """Starred routes, and the activities that started at or after `since` (none when since is None)."""
        con = self._connect()
        try:
            try:
                checked = dict(
                    con.execute(
                        "SELECT object_type, checked_at FROM strava.raw_latest WHERE object_type IN ('route_list', 'activity_list') OR (object_type = 'athlete' AND object_key = 'self')"
                    ).fetchall()
                )
                last_list = con.execute("SELECT value FROM strava.sync_state WHERE name = 'last:list_recent'").fetchone()
                routes = [
                    StravaRoute(int(r[0]), r[1] or "", float(r[2]) if r[2] is not None else None, polyline_start(r[3]))
                    for r in con.execute("SELECT id, name, distance, summary_polyline FROM strava.route WHERE starred ORDER BY id").fetchall()
                ]
                activities: list[StravaActivity] = []
                if since is not None:
                    activities = [
                        StravaActivity(int(r[0]), r[1] or "", r[2], int(r[3]) if r[3] is not None else None)
                        for r in con.execute(
                            "SELECT id, name, start_date, elapsed_time FROM strava.activity WHERE start_date >= ? AND start_date IS NOT NULL ORDER BY start_date",
                            [since],
                        ).fetchall()
                    ]
            except duckdb.Error as err:
                raise SnapshotUnavailable(f"the Strava snapshot {self.config.snapshot} is not a readable strava-mcp snapshot: {err}") from err
        finally:
            con.close()
        activities_checked = None
        if last_list and last_list[0]:
            try:
                activities_checked = datetime.fromisoformat(str(last_list[0]).replace("Z", "+00:00"))
            except ValueError:
                activities_checked = None
        return StravaData(checked.get("route_list"), activities_checked, routes, activities)

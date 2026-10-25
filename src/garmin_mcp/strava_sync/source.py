"""Strava data for the sync, read from strava-mcp: its live export (GET /export/sync)
or its published snapshot.

The export is a few kilobytes of JSON computed live, so a rename reaches the sync
within a minute of strava-mcp hearing of it. The snapshot is republished at most
hourly. For the snapshot, each read opens its own in-memory DuckDB instance and attaches the snapshot read-only,
so the mirror's connection (and its memory limit and file-access lockdown) is never
involved, and garmin_query never sees Strava data. strava-mcp replaces the snapshot
by renaming a new file over it, so it is attached afresh on every read.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

import duckdb

from .config import StravaSyncConfig


class SourceUnavailable(Exception):
    """strava-mcp's export or snapshot cannot be read."""


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
        src = self.config.snapshot or ""
        try:
            if self.config.is_url:
                con.execute("INSTALL httpfs")
                con.execute("LOAD httpfs")
                if self.config.token:
                    header = f"Bearer {self.config.token}".replace("'", "''")
                    con.execute(f"CREATE SECRET strava_snapshot (TYPE http, EXTRA_HTTP_HEADERS MAP {{'Authorization': '{header}'}})")
            elif not Path(src).is_file():
                raise SourceUnavailable(f"no Strava snapshot at {src}")
            con.execute(f"ATTACH '{src.replace(chr(39), chr(39) * 2)}' AS strava (READ_ONLY)")
        except duckdb.Error as err:
            con.close()
            raise SourceUnavailable(f"cannot attach the Strava snapshot {src}: {err}") from err
        except SourceUnavailable:
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
                            "SELECT id, name, start_date, elapsed_time FROM strava.activity WHERE start_date >= ? AND start_date IS NOT NULL AND deleted_at IS NULL ORDER BY start_date",
                            [since],
                        ).fetchall()
                    ]
            except duckdb.Error as err:
                raise SourceUnavailable(f"the Strava snapshot {self.config.snapshot} is not a readable strava-mcp snapshot: {err}") from err
        finally:
            con.close()
        activities_checked = None
        if last_list and last_list[0]:
            try:
                activities_checked = datetime.fromisoformat(str(last_list[0]).replace("Z", "+00:00"))
            except ValueError:
                activities_checked = None
        return StravaData(checked.get("route_list"), activities_checked, routes, activities)


class StravaExport:
    """strava-mcp's GET /export/sync (older releases: /export/garmin-sync), with STRAVA_EXPORT_TOKEN as bearer."""

    def __init__(self, config: StravaSyncConfig, *, timeout: float = 30.0) -> None:
        self.config = config
        self.timeout = timeout

    def read(self, since: datetime | None) -> StravaData:
        url = self.config.export_url or ""
        if since is not None:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode({"since": since.isoformat()})
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        if self.config.token:
            req.add_header("Authorization", f"Bearer {self.config.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 (configured URL)
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            raise SourceUnavailable(f"strava-mcp's export {self.config.export_url} answered HTTP {err.code}") from err
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as err:
            raise SourceUnavailable(f"cannot read strava-mcp's export {self.config.export_url}: {err}") from err
        try:
            routes = [
                StravaRoute(int(r["id"]), r.get("name") or "", float(r["distance"]) if r.get("distance") is not None else None,
                            (float(r["start"][0]), float(r["start"][1])) if r.get("start") else None)
                for r in body["starred_routes"]
            ]
            activities = [
                StravaActivity(int(a["id"]), a.get("name") or "", _ts(a["start_date"]), int(a["elapsed_time"]) if a.get("elapsed_time") is not None else None)
                for a in body["activities"]
            ]
            return StravaData(_ts_or_none(body.get("route_list_checked_at")), _ts_or_none(body.get("activity_list_checked_at")), routes, activities)
        except (KeyError, TypeError, ValueError, IndexError) as err:
            raise SourceUnavailable(f"strava-mcp's export {self.config.export_url} is not in the expected shape: {err}") from err


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _ts_or_none(value: str | None) -> datetime | None:
    return _ts(value) if value else None


class Source(Protocol):
    def read(self, since: datetime | None) -> StravaData: ...


def make_source(config: StravaSyncConfig) -> Source:
    return StravaExport(config) if config.export_url else StravaSnapshot(config)

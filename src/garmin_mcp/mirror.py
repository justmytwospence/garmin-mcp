"""Cache-first reads for the tools.

Every read is answered from the DuckDB mirror when the stored copy is fresh enough for
that kind of data, and fetched (interactive priority, landed into the mirror) otherwise.
If Garmin cannot be asked right now (backoff after a 429, outage, lost authentication)
and a copy exists, the copy is served and marked stale. Each answer says where it came
from. Writes go to Garmin first; what Garmin returns is landed, never an optimistic guess.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .db.database import Database
from .db.raw import land
from .garmin.client import AuthError, GarminClient, GarminError, NotFound, RateLimited, Unavailable
from .garmin.datasets import DATASETS, REPORT_TTL, dataset, days_between
from .sync.engine import SyncEngine

log = logging.getLogger("garmin_mcp.mirror")

MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
FOREVER = timedelta(days=36500)


@dataclass
class Provenance:
    source: str  # mirror or garmin
    fetched_at: str | None
    stale: str | None = None
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        when = f" (fetched from Garmin {self.fetched_at})" if self.fetched_at else ""
        origin = f"[served from the local mirror{when}]" if self.source == "mirror" else "[fetched from Garmin Connect just now]"
        if self.stale:
            origin += f" [stale: Garmin could not be asked: {self.stale}]"
        for n in self.notes:
            origin += f" [{n}]"
        return origin


@dataclass
class Cached:
    data: Any
    meta: Provenance


class GarminNotFound(Exception):
    pass


def can_serve_stale(err: Exception) -> bool:
    return isinstance(err, (RateLimited, Unavailable, AuthError))


class Mirror:
    def __init__(self, db: Database, engine: SyncEngine, client: GarminClient) -> None:
        self.db = db
        self.engine = engine
        self.client = client
        self.fetch = engine.fetch

    # ------------------------------------------------------------- generic

    def today(self) -> date:
        return self.engine.today()

    def _stored(self, type_: str, key: str) -> dict | None:
        return self.db.one("SELECT status, checked_at, fetched_at, body FROM raw_current WHERE object_type = ? AND object_key = ?", [type_, key])

    def _to_cached(self, s: dict, source: str, stale: str | None = None) -> Cached:
        if s["status"] != "ok" or s["body"] is None:
            raise GarminNotFound(f"Nothing on Garmin Connect (as of {s['checked_at'].isoformat()})")
        meta = Provenance(source, s["checked_at"].isoformat(), stale)
        return Cached(json.loads(s["body"]), meta)

    def cached(self, type_: str, key: str, max_age: timedelta, fetcher: Callable[[], Any]) -> Cached:
        """Serve (type, key) from the mirror if younger than max_age, else fetch it first."""
        before = self._stored(type_, key)
        if before is not None and datetime.now(UTC) - before["checked_at"] <= max_age:
            return self._to_cached(before, "mirror")
        try:
            fetcher()
        except Exception as err:  # noqa: BLE001
            if before is not None and can_serve_stale(err):
                return self._to_cached(before, "mirror", str(err)[:200])
            raise
        after = self._stored(type_, key)
        if after is None:
            raise RuntimeError(f"Fetched {type_} {key} but nothing was stored")
        return self._to_cached(after, "garmin")

    def day_age(self, day: str) -> timedelta:
        """How stale a per-day payload may be: today/yesterday 15 minutes, last week 6 hours, older forever."""
        d = date.fromisoformat(day)
        age_days = (self.today() - d).days
        if age_days <= 1:
            return 15 * MINUTE
        if age_days <= 7:
            return 6 * HOUR
        return FOREVER

    # ------------------------------------------------------------- datasets

    def snapshot(self, name: str, key: str = "self", max_age: timedelta | None = None) -> Cached:
        ds = dataset(name)
        age = max_age if max_age is not None else (ds.refresh or DAY)
        if name in ("gear", "gear_defaults") and key == "self":
            key = self.fetch.user_profile_pk("interactive")
        return self.cached(name, key, age, lambda: self.fetch.snapshot(ds, key, priority="interactive"))

    def day(self, name: str, day: str) -> Cached:
        ds = dataset(name)
        if ds.kind == "range":
            return self.range_day(name, day)
        return self.cached(name, day, self.day_age(day), lambda: self.fetch.day(ds, day, priority="interactive"))

    def range_day(self, name: str, day: str) -> Cached:
        ds = dataset(name)
        return self.cached(name, day, self.day_age(day), lambda: self.fetch.range(ds, day, day, priority="interactive"))

    def range_days(self, name: str, start: str, end: str) -> Cached:
        """Per-day payloads of a range dataset for [start, end], as a list."""
        ds = dataset(name)
        s, e = date.fromisoformat(start), date.fromisoformat(end)
        if e < s:
            s, e = e, s
        days = [d.isoformat() for d in days_between(s, e)]
        rows = self.db.all(
            "SELECT object_key, status, checked_at, body FROM raw_current WHERE object_type = ? AND object_key IN (SELECT unnest(?::VARCHAR[]))",
            [name, days],
        )
        by_key = {r["object_key"]: r for r in rows}
        now = datetime.now(UTC)
        missing = [d for d in days if d not in by_key or now - by_key[d]["checked_at"] > self.day_age(d)]
        meta = Provenance("mirror", None)
        if missing:
            try:
                for chunk_start, chunk_end in self._chunks(missing, ds.chunk_days):
                    self.fetch.range(ds, chunk_start, chunk_end, priority="interactive")
                meta = Provenance("garmin", now.isoformat())
            except Exception as err:  # noqa: BLE001
                if not rows or not can_serve_stale(err):
                    raise
                meta = Provenance("mirror", None, str(err)[:200])
            rows = self.db.all(
                "SELECT object_key, status, checked_at, body FROM raw_current WHERE object_type = ? AND object_key IN (SELECT unnest(?::VARCHAR[]))",
                [name, days],
            )
        data = [json.loads(r["body"]) for r in sorted(rows, key=lambda r: r["object_key"]) if r["body"] is not None]
        if meta.source == "mirror" and rows:
            meta.fetched_at = max(r["checked_at"] for r in rows).isoformat()
        return Cached(data, meta)

    @staticmethod
    def _chunks(days: list[str], size: int) -> list[tuple[str, str]]:
        ds = sorted(date.fromisoformat(d) for d in days)
        out = []
        i = 0
        while i < len(ds):
            s = ds[i]
            j = i
            while j + 1 < len(ds) and (ds[j + 1] - s).days < size:
                j += 1
            out.append((s.isoformat(), ds[j].isoformat()))
            i = j + 1
        return out

    def days(self, name: str, start: str, end: str) -> Cached:
        """Per-day payloads of a day dataset for [start, end] (fetching the stale ones), as {date: payload}."""
        ds = dataset(name)
        if ds.kind == "range":
            return self.range_days(name, start, end)
        s, e = date.fromisoformat(start), date.fromisoformat(end)
        out: dict[str, Any] = {}
        meta = Provenance("mirror", None)
        stale_notes: list[str] = []
        for d in days_between(s, e):
            try:
                c = self.day(name, d.isoformat())
            except GarminNotFound:
                continue
            except Exception as err:  # noqa: BLE001
                if can_serve_stale(err):
                    stale_notes.append(str(err)[:120])
                    continue
                raise
            out[d.isoformat()] = c.data
            if c.meta.source == "garmin":
                meta = Provenance("garmin", datetime.now(UTC).isoformat())
        if stale_notes:
            meta.stale = stale_notes[0]
        return Cached(out, meta)

    def activity(self, name: str, activity_id: str) -> Cached:
        ds = dataset(name)
        row = self.db.one(
            """SELECT (epoch_ms(now()) - epoch_ms(a.start_time_gmt)) / 1000.0 AS age_s,
                      coalesce(a.summary_fetched_at > d.checked_at, false) AS summary_newer, a.deleted_at
               FROM activity a LEFT JOIN raw_latest d ON d.object_type = ? AND d.object_key = a.activity_id::VARCHAR
               WHERE a.activity_id = ?::BIGINT""",
            [name, activity_id],
        )
        if row is not None and row["deleted_at"] is not None:
            raise GarminNotFound(f"Activity {activity_id} was deleted on Garmin Connect ({row['deleted_at'].isoformat()})")
        max_age = FOREVER
        if row is None:
            max_age = DAY
        elif name == "activity" and row["summary_newer"]:
            max_age = timedelta(0)
        elif name == "activity" and row["age_s"] is not None and row["age_s"] < 2 * 86400:
            max_age = 15 * MINUTE
        return self.cached(name, activity_id, max_age, lambda: self.fetch.activity_sub(ds, activity_id, priority="interactive"))

    def fresh_activity_list(self, max_age: timedelta = 15 * MINUTE) -> Provenance:
        last = self.engine.get_state("last:list_recent")
        if last and datetime.now(UTC) - datetime.fromisoformat(last) <= max_age:
            return Provenance("mirror", last)
        try:
            self.engine.run_job("list_recent", "self", 5)
            return Provenance("garmin", datetime.now(UTC).isoformat())
        except Exception as err:  # noqa: BLE001
            if last and can_serve_stale(err):
                return Provenance("mirror", last, str(err)[:200])
            raise

    def activity_fit(self, activity_id: str) -> Cached:
        """Ensure the FIT file is stored and decoded; returns the raw_file row."""
        row = self.db.one("SELECT * FROM raw_file WHERE kind = 'activity_fit' AND key = ?", [activity_id])
        meta = Provenance("mirror", row["fetched_at"].isoformat() if row else None)
        if row is None:
            self.fetch.activity_fit(activity_id, priority="interactive")
            row = self.db.one("SELECT * FROM raw_file WHERE kind = 'activity_fit' AND key = ?", [activity_id])
            meta = Provenance("garmin", datetime.now(UTC).isoformat())
            if row is None:
                raise GarminNotFound(f"No original file for activity {activity_id}")
        return Cached(row, meta)

    # ------------------------------------------------------------- reports (cached pass-through)

    def report(self, name: str, path: str, params: dict[str, Any] | None, key: str, ttl: timedelta = REPORT_TTL) -> Cached:
        """A derived Garmin report (progress summary, power curve, ...): cached under (name, key) for ttl."""

        def fetcher() -> None:
            text = self.client.get_text(path, params, priority="interactive")
            with self.db.transaction() as tx:
                if text is None:
                    from .db.raw import mark_not_found

                    mark_not_found(tx, name, key)
                else:
                    land(tx, name, key, text)

        return self.cached(name, key, ttl, fetcher)

    # ------------------------------------------------------------- writes

    def write(self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: Any = None, refetch: Callable[[], Any] | None = None) -> Any:
        """Send a write to Garmin, then re-fetch what it affected so the mirror reflects Garmin, not our intent."""
        text = self.client.send(method, path, params=params, json=json_body, priority="interactive")
        result: Any = None
        if text:
            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                result = text
        if refetch is not None:
            try:
                refetch()
            except Exception as err:  # noqa: BLE001
                log.warning("refetch after write failed: %s", err)
        return result

    # ------------------------------------------------------------- status

    def dataset_names(self) -> list[str]:
        return [d.name for d in DATASETS]


__all__ = ["Mirror", "Cached", "Provenance", "GarminNotFound", "GarminError", "NotFound"]

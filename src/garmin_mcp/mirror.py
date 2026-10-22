"""Cache-first reads for the tools.

Every read is answered from the DuckDB mirror when the stored copy is fresh enough for
that kind of data, and fetched (interactive priority, landed into the mirror) otherwise.
If Garmin cannot be asked right now (backoff after a 429, outage, lost authentication)
and a copy exists, the copy is served and marked stale. Each answer says where it came
from. Writes go to Garmin first; what Garmin returns is landed, never an optimistic guess.
"""

from __future__ import annotations

import contextvars
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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


def merge_provenance(metas: list[Provenance]) -> Provenance:
    """One provenance line for a result assembled from several reads."""
    if not metas:
        return Provenance("mirror", None, notes=["computed from the local mirror"])
    if len(metas) == 1:
        return metas[0]
    fetched_now = [m for m in metas if m.source == "garmin"]
    mirrored = [m for m in metas if m.source == "mirror"]
    stale = next((m.stale for m in metas if m.stale), None)
    notes = [n for m in metas for n in m.notes]
    if fetched_now and not mirrored:
        return Provenance("garmin", None, stale, notes)
    oldest = min((m.fetched_at for m in mirrored if m.fetched_at), default=None)
    if fetched_now:
        notes = [f"{len(fetched_now)} of {len(metas)} parts fetched from Garmin just now; the rest from the mirror" + (f" (oldest {oldest})" if oldest else ""), *notes]
        return Provenance("garmin", None, stale, notes)
    return Provenance("mirror", oldest, stale, notes)


# Every Cached the mirror hands out during a tool call is noted here, so the tool's
# provenance line can be assembled without each tool threading metadata around.
_collector: contextvars.ContextVar[list[Provenance] | None] = contextvars.ContextVar("garmin_mcp_provenance", default=None)


def note_provenance(meta: Provenance) -> None:
    bucket = _collector.get()
    if bucket is not None:
        bucket.append(meta)


@contextmanager
def collect_provenance() -> Iterator[list[Provenance]]:
    bucket: list[Provenance] = []
    token = _collector.set(bucket)
    try:
        yield bucket
    finally:
        _collector.reset(token)


@dataclass
class Cached:
    data: Any
    meta: Provenance

    def __post_init__(self) -> None:
        note_provenance(self.meta)


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
            note_provenance(Provenance(source, s["checked_at"].isoformat(), stale, notes=["Garmin had nothing for this object"]))
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

    def optional(self, name: str, key: str = "self", max_age: timedelta | None = None, default: Any = None) -> Any:
        """A snapshot whose absence is a normal answer (an empty gear list, no race prediction): the data or `default`."""
        try:
            return self.snapshot(name, key, max_age).data
        except GarminNotFound:
            return default

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

    def chunk_grid(self, name: str, start: date, end: date) -> list[tuple[str, str]]:
        """The fixed chunk windows (from the history start) of a whole-range dataset covering [start, end]."""
        ds = dataset(name)
        origin = self.engine.history_start() or start
        if origin > start:
            origin = start
        today = self.today()
        out = []
        d = origin
        while d <= min(end, today):
            e = min(d + timedelta(days=ds.chunk_days - 1), today)
            if e >= start:
                out.append((d.isoformat(), e.isoformat()))
            d = e + timedelta(days=1)
        return out

    def chunks(self, name: str, start: str, end: str) -> Cached:
        """Payloads of a whole-range dataset (weigh-ins, blood pressure, wellness metrics) covering [start, end]."""
        ds = dataset(name)
        s, e = date.fromisoformat(start), date.fromisoformat(end)
        out: list[Any] = []
        metas: list[Provenance] = []
        for cs, ce in self.chunk_grid(name, s, e):
            try:
                c = self.cached(name, f"{cs}_{ce}", self.day_age(ce), lambda cs=cs, ce=ce: self.fetch.range(ds, cs, ce, priority="interactive"))
            except GarminNotFound:
                continue
            out.append(c.data)
            metas.append(c.meta)
        return Cached(out, merge_provenance(metas))

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

    def report(self, name: str, path: str, params: dict[str, Any] | None, key: str, ttl: timedelta = REPORT_TTL, *, method: str = "GET", json_body: Any = None) -> Cached:
        """A derived Garmin report (progress summary, power curve, a GraphQL query, ...): cached under (name, key) for ttl."""

        def fetcher() -> None:
            if method == "GET":
                text = self.client.get_text(path, params, priority="interactive")
            else:
                try:
                    text = self.client.send(method, path, params=params, json=json_body, priority="interactive")
                except NotFound:
                    text = None
            with self.db.transaction() as tx:
                if text is None:
                    from .db.raw import mark_not_found

                    mark_not_found(tx, name, key)
                else:
                    land(tx, name, key, text)

        return self.cached(name, key, ttl, fetcher)

    # ------------------------------------------------------------- writes

    def write(self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: Any = None, files: Any = None, refetch: Callable[[], Any] | None = None) -> Any:
        """Send a write to Garmin, then re-fetch what it affected so the mirror reflects Garmin, not our intent."""
        text = self.client.send(method, path, params=params, json=json_body, files=files, priority="interactive")
        result: Any = None
        if text:
            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                result = text
        self._after_write(f"{method} {path}", refetch)
        return result

    def call(self, method_name: str, *args: Any, refetch: Callable[[], Any] | None = None, **kwargs: Any) -> Any:
        """A write through a python-garminconnect method (it builds the payload), then the same re-fetch."""
        result = self.client.call(method_name, *args, priority="interactive", **kwargs)
        self._after_write(method_name, refetch)
        return result

    def _after_write(self, what: str, refetch: Callable[[], Any] | None) -> None:
        note = f"write {what} accepted by Garmin Connect"
        if refetch is not None:
            try:
                refetch()
                note += "; the affected objects were re-fetched into the mirror"
            except Exception as err:  # noqa: BLE001
                log.warning("refetch after write failed: %s", err)
                note += f"; re-fetching the affected objects failed ({str(err)[:120]}), a refresh job was queued"
        note_provenance(Provenance("garmin", None, notes=[note]))

    def refresh_activity(self, activity_id: str) -> None:
        """Re-fetch an activity's summary (via the recent listing) and detail after a write."""
        try:
            self.engine.run_job("list_recent", "self", 5)
        except Exception as err:  # noqa: BLE001
            log.warning("list_recent after write failed: %s", err)
            self.engine.enqueue("list_recent", "self", 5)
        self.fetch.activity_sub(dataset("activity"), activity_id, priority="interactive")

    def refresh_snapshot(self, name: str, key: str = "self") -> None:
        ds = dataset(name)
        if name in ("gear", "gear_defaults") and key == "self":
            key = self.fetch.user_profile_pk("interactive")
        self.fetch.snapshot(ds, key, priority="interactive")

    def refresh_day(self, name: str, day: str) -> None:
        ds = dataset(name)
        if ds.kind == "range":
            self.fetch.range(ds, day, day, priority="interactive")
        else:
            self.fetch.day(ds, day, priority="interactive")

    def refresh_range_chunk(self, name: str, day: str) -> None:
        """Re-fetch the whole-chunk range dataset covering `day` (weigh-ins, blood pressure)."""
        ds = dataset(name)
        d = date.fromisoformat(day)
        start = self.engine.history_start() or d
        while start + timedelta(days=ds.chunk_days) <= d:
            start += timedelta(days=ds.chunk_days)
        end = min(start + timedelta(days=ds.chunk_days - 1), self.today())
        self.fetch.range(ds, start.isoformat(), end.isoformat(), priority="interactive")

    def enqueue_refresh(self, kind: str, key: str) -> None:
        self.engine.enqueue(kind, key, 5)

    # ------------------------------------------------------------- typed tables

    def rows(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """Rows from the typed tables, JSON-ready (dates and timestamps as ISO strings)."""
        return [{k: jsonable(v) for k, v in r.items()} for r in self.db.all(sql, params)]

    def row(self, sql: str, params: list[Any] | None = None) -> dict[str, Any] | None:
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------- status

    def dataset_names(self) -> list[str]:
        return [d.name for d in DATASETS]


def jsonable(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, str):
        return v
    if hasattr(v, "__float__") and not isinstance(v, (int, float, bool)):
        return float(v)
    return v


__all__ = ["Mirror", "Cached", "Provenance", "GarminNotFound", "GarminError", "NotFound", "merge_provenance", "collect_provenance", "note_provenance", "jsonable"]

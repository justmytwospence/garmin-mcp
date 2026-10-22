"""Fetchers: one Garmin request (or one paged listing) each, landed into the mirror.

The sync worker and the cache-first tools call the same functions; only the priority
differs. A 404/204/empty answer is recorded as not_found rather than thrown, so it is
never retried needlessly.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..db.database import Database
from ..db.raw import land, land_many, mark_not_found
from ..fit.decode import PARSER_VERSION, land_fit
from ..garmin.client import GarminClient, NotFound, Priority
from ..garmin.datasets import (
    ACTIVITY_FIT_PATH,
    ACTIVITY_LIST_PATH,
    RELOAD_PATH,
    WELLNESS_FIT_PATH,
    Dataset,
    dataset,
    days_between,
)

log = logging.getLogger("garmin_mcp.sync")

PAGE = 100


@dataclass
class FetchResult:
    changed: bool
    not_found: bool = False


def _dumps(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)


def _empty(text: str | None) -> bool:
    if text is None:
        return True
    t = text.strip()
    return t in ("", "[]", "{}", "null")


class Fetchers:
    def __init__(self, db: Database, client: GarminClient, data_dir: str | Path) -> None:
        self.db = db
        self.client = client
        self.data_dir = Path(data_dir)

    # ------------------------------------------------------------- helpers

    @property
    def display_name(self) -> str:
        return self.client.display_name

    def user_profile_pk(self, priority: Priority = "incremental") -> str:
        row = self.db.one("SELECT id FROM user_settings LIMIT 1")
        if row is None:
            self.snapshot(dataset("user_settings"), priority=priority)
            row = self.db.one("SELECT id FROM user_settings LIMIT 1")
        if row is None:
            raise RuntimeError("User settings unavailable")
        return str(row["id"])

    def _land(self, type_: str, key: str, text: str | None) -> FetchResult:
        if _empty(text):
            with self.db.transaction() as tx:
                mark_not_found(tx, type_, key)
            return FetchResult(changed=True, not_found=True)
        assert text is not None
        with self.db.transaction() as tx:
            changed = land(tx, type_, key, text)
        return FetchResult(changed=changed)

    def _resolve_key(self, ds: Dataset, key: str, priority: Priority) -> str:
        if ds.name in ("gear", "gear_defaults") and key == "self":
            return self.user_profile_pk(priority)
        return key

    # ------------------------------------------------------------- datasets

    def snapshot(self, ds: Dataset, key: str = "self", *, priority: Priority = "backfill") -> FetchResult:
        key = self._resolve_key(ds, key, priority)
        start = end = None
        if ds.name == "calendar_month":
            y, m = key.split("-")
            start, end = y, str(int(m) - 1)
        elif ds.name == "calendar_events":
            start, end = key.split("_")
        path, params = ds.resolve(self.display_name, key, start, end)
        text = self.client.get_text(path, params or None, priority=priority, headers=ds.headers or None)
        return self._land(ds.name, key, text)

    def day(self, ds: Dataset, day: str, *, priority: Priority = "backfill") -> FetchResult:
        path, params = ds.resolve(self.display_name, day)
        text = self.client.get_text(path, params or None, priority=priority)
        return self._land(ds.name, day, text)

    def range(self, ds: Dataset, start: str, end: str, *, priority: Priority = "backfill") -> list[str]:
        """Fetch a window; returns the keys that changed. Days without data are marked not_found."""
        path, params = ds.resolve(self.display_name, f"{start}_{end}", start, end)
        text = self.client.get_text(path, params or None, priority=priority)
        if ds.whole:
            r = self._land(ds.name, f"{start}_{end}", text)
            return [f"{start}_{end}"] if r.changed else []
        items: list[tuple[str, str]] = []
        if not _empty(text):
            assert text is not None and ds.split is not None
            try:
                items = ds.split(json.loads(text))
            except json.JSONDecodeError:
                items = []
        days = {d.isoformat() for d in days_between(date.fromisoformat(start), date.fromisoformat(end))}
        with self.db.transaction() as tx:
            changed = land_many(tx, ds.name, [(k, b) for k, b in items if k in days])
            seen = {k for k, _ in items}
            for d in sorted(days - seen):
                mark_not_found(tx, ds.name, d)
        return changed

    def activity_sub(self, ds: Dataset, activity_id: str, *, priority: Priority = "backfill") -> FetchResult:
        path, params = ds.resolve(self.display_name, activity_id)
        text = self.client.get_text(path, params or None, priority=priority)
        return self._land(ds.name, activity_id, text)

    # ------------------------------------------------------------- activity list

    def activities(self, *, priority: Priority = "backfill", complete: bool = False, stop_when_known: bool = True, max_pages: int = 10000) -> dict[str, list[str]]:
        """List activities newest first, landing each summary. With `complete`, activities missing from the listing are marked deleted."""
        seen: list[str] = []
        changed: list[str] = []
        start = 0
        pages = 0
        while pages < max_pages:
            text = self.client.get_text(ACTIVITY_LIST_PATH, {"start": str(start), "limit": str(PAGE)}, priority=priority)
            pages += 1
            items = json.loads(text) if not _empty(text) else []
            if not items:
                break
            keyed = [(str(a["activityId"]), _dumps(a)) for a in items if isinstance(a, dict) and a.get("activityId")]
            known = set()
            if stop_when_known and not complete:
                ids = [k for k, _ in keyed]
                rows = self.db.all("SELECT object_key FROM raw_latest WHERE object_type = 'activity_summary' AND object_key IN (SELECT unnest(?::VARCHAR[]))", [ids])
                known = {r["object_key"] for r in rows}
            with self.db.transaction() as tx:
                changed.extend(land_many(tx, "activity_summary", keyed))
            seen.extend(k for k, _ in keyed)
            if len(items) < PAGE:
                break
            if stop_when_known and not complete and known:
                break
            start += PAGE
        deleted: list[str] = []
        if complete:
            with self.db.transaction() as tx:
                gone = tx.all(
                    "SELECT object_key AS k FROM raw_latest WHERE object_type = 'activity_summary' AND status = 'ok' AND object_key NOT IN (SELECT unnest(?::VARCHAR[]))",
                    [seen],
                )
                for g in gone:
                    mark_not_found(tx, "activity_summary", g["k"])
                    deleted.append(g["k"])
        return {"seen": seen, "changed": changed, "deleted": deleted}

    # ------------------------------------------------------------- files

    def _store_file(self, kind: str, key: str, data: bytes | None, at: datetime) -> FetchResult:
        sub = "activities" if kind == "activity_fit" else "wellness"
        if data is None:
            with self.db.transaction() as tx:
                mark_not_found(tx, kind, key, at)
            return FetchResult(changed=True, not_found=True)
        sha = hashlib.sha256(data).hexdigest()
        prev = self.db.one("SELECT sha256, parser_version FROM raw_file WHERE kind = ? AND key = ?", [kind, key])
        folder = self.data_dir / "fit" / sub
        folder.mkdir(parents=True, exist_ok=True)
        ext = ".zip" if data[:2] == b"PK" else ".fit"
        path = folder / f"{key}{ext}"
        unchanged = prev is not None and prev["sha256"] == sha and prev["parser_version"] == PARSER_VERSION and path.exists()
        if not unchanged:
            path.write_bytes(data)
        with self.db.transaction() as tx:
            tx.run(
                """INSERT OR REPLACE INTO raw_latest (object_type, object_key, status, content_hash, fetched_at, checked_at)
                   VALUES (?, ?, 'ok', ?, ?, ?)""",
                [kind, key, sha, at, at],
            )
            if unchanged:
                return FetchResult(changed=False)
            summary = land_fit(tx, kind, key, data, at)
            tx.run(
                """INSERT OR REPLACE INTO raw_file (kind, key, path, sha256, size, fetched_at, parser_version, parsed_at, parse_error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [kind, key, str(path), sha, len(data), at, PARSER_VERSION, at, "; ".join(summary["errors"])[:1000] or None],
            )
        return FetchResult(changed=True)

    def activity_fit(self, activity_id: str, *, priority: Priority = "backfill") -> FetchResult:
        data = self.client.get_bytes(ACTIVITY_FIT_PATH.format(key=activity_id), priority=priority)
        return self._store_file("activity_fit", activity_id, data, datetime.now(UTC))

    def wellness_fit(self, day: str, *, priority: Priority = "backfill") -> FetchResult:
        data = self.client.get_bytes(WELLNESS_FIT_PATH.format(key=day), priority=priority)
        if data is not None and data[:2] != b"PK" and data[:12].find(b".FIT") < 0:
            data = None  # an HTML or JSON error page, not a file
        return self._store_file("wellness_fit", day, data, datetime.now(UTC))

    def import_file(self, kind: str, key: str, data: bytes) -> FetchResult:
        """Land a file obtained elsewhere (account export); no Garmin call."""
        return self._store_file(kind, key, data, datetime.now(UTC))

    def reparse_file(self, kind: str, key: str) -> FetchResult:
        row = self.db.one("SELECT path FROM raw_file WHERE kind = ? AND key = ?", [kind, key])
        if row is None:
            return FetchResult(changed=False)
        return self._store_file(kind, key, Path(row["path"]).read_bytes(), datetime.now(UTC))

    # ------------------------------------------------------------- archive reload

    def request_reload(self, day: str, *, priority: Priority = "backfill") -> dict[str, Any]:
        at = datetime.now(UTC)
        try:
            text = self.client.send("POST", RELOAD_PATH.format(key=day), priority=priority)
        except NotFound:
            text = None
        status = None
        body: Any = None
        if text:
            try:
                body = json.loads(text)
                status = body.get("status") if isinstance(body, dict) else None
            except json.JSONDecodeError:
                body = {"raw": text[:500]}
        with self.db.transaction() as tx:
            tx.run(
                "INSERT INTO reload_request (calendar_date, requested_at, status, response) VALUES (?, ?, ?, ?::JSON)",
                [day, at, status or "unknown", _dumps(body)],
            )
        return {"status": status, "response": body}

    def reloads_last_24h(self) -> int:
        return int(self.db.scalar("SELECT count(*) FROM reload_request WHERE requested_at > now() - INTERVAL 24 HOUR") or 0)

    def next_reload_slot(self) -> datetime | None:
        row = self.db.one("SELECT min(requested_at) AS t FROM (SELECT requested_at FROM reload_request WHERE requested_at > now() - INTERVAL 24 HOUR ORDER BY requested_at LIMIT 1)")
        if row is None or row["t"] is None:
            return None
        return row["t"] + timedelta(hours=24, minutes=5)

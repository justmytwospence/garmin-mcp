"""Landing raw payloads. The body is stored as the JSON text Garmin sent (DuckDB parses
it itself, so 64-bit ids keep their precision); a new raw_payload row is written only
when the content changed, and raw_latest always records the check.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from .database import Tx
from .projections import project


def hash_body(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _now(at: datetime | None) -> datetime:
    return at or datetime.now(UTC)


def store(tx: Tx, type_: str, key: str, body: str, at: datetime | None = None) -> bool:
    """Store one payload without projecting. Returns whether it changed."""
    h = hash_body(body)
    prev = tx.one("SELECT content_hash, status FROM raw_latest WHERE object_type = ? AND object_key = ?", [type_, key])
    changed = prev is None or prev["content_hash"] != h or prev["status"] != "ok"
    when = _now(at)
    if prev is None or prev["content_hash"] != h:
        tx.run(
            """INSERT INTO raw_payload (object_type, object_key, content_hash, fetched_at, body)
               VALUES (?, ?, ?, ?, ?::JSON) ON CONFLICT DO NOTHING""",
            [type_, key, h, when, body],
        )
    # When this version was first stored (a payload can return to an earlier version). By
    # the hash alone, which the content_hash index answers without scanning raw_payload.
    first = [r["fetched_at"] for r in tx.all("SELECT object_type, object_key, fetched_at FROM raw_payload WHERE content_hash = ?", [h]) if r["object_type"] == type_ and r["object_key"] == key]
    tx.run(
        """INSERT OR REPLACE INTO raw_latest (object_type, object_key, status, content_hash, fetched_at, checked_at)
           VALUES (?, ?, 'ok', ?, ?, ?)""",
        [type_, key, h, first[0] if first else when, when],
    )
    return changed


def land(tx: Tx, type_: str, key: str, body: str, at: datetime | None = None) -> bool:
    """Store one payload and, when it changed, rebuild the typed rows derived from it."""
    changed = store(tx, type_, key, body, at)
    if changed:
        project(tx, type_, [key])
    return changed


def land_many(tx: Tx, type_: str, items: Iterable[tuple[str, str]], at: datetime | None = None) -> list[str]:
    """Store many payloads of one type, then project the changed ones in one pass. Returns the changed keys."""
    changed = [key for key, body in items if store(tx, type_, key, body, at)]
    if changed:
        project(tx, type_, changed)
    return changed


def mark_status(tx: Tx, type_: str, key: str, status: str, at: datetime | None = None) -> None:
    """Record that Garmin had nothing for (type, key): not_found or archived. The last good payload stays as history."""
    when = _now(at)
    tx.run(
        """INSERT INTO raw_latest (object_type, object_key, status, content_hash, fetched_at, checked_at)
           VALUES (?, ?, ?, NULL, NULL, ?)
           ON CONFLICT (object_type, object_key) DO UPDATE SET status = excluded.status, content_hash = NULL, fetched_at = NULL, checked_at = excluded.checked_at""",
        [type_, key, status, when],
    )
    project(tx, type_, [key])


def mark_not_found(tx: Tx, type_: str, key: str, at: datetime | None = None) -> None:
    mark_status(tx, type_, key, "not_found", at)


def touch(tx: Tx, type_: str, key: str, at: datetime | None = None) -> None:
    """Record a check that found the payload unchanged without re-sending it."""
    tx.run("UPDATE raw_latest SET checked_at = ? WHERE object_type = ? AND object_key = ?", [_now(at), type_, key])


class _Reader(Protocol):
    def all(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]: ...
    def scalar(self, sql: str, params: Sequence[Any] | None = None) -> Any: ...


def read_current(q: _Reader, type_: str, keys: Sequence[str]) -> list[dict[str, Any]]:
    """raw_current's object_key, status, checked_at, fetched_at and body for some keys of one type.

    Not a query on the raw_current view: filtering raw_payload by type and key still reads
    the body column a whole vector at a time, megabyte-sized activity details included
    (see projections.stage). The body is fetched by its hash instead, a point read on the
    content_hash index.
    """
    rows = q.all(
        "SELECT object_key, status, checked_at, fetched_at, content_hash FROM raw_latest WHERE object_type = ? AND object_key IN (SELECT unnest(?::VARCHAR[]))",
        [type_, list(keys)],
    )
    for r in rows:
        h = r.pop("content_hash")
        r["body"] = q.scalar("SELECT body FROM raw_payload WHERE content_hash = ? LIMIT 1", [h]) if h is not None else None
        if r["body"] is None:
            r["fetched_at"] = None
    return rows


def current(q: _Reader, type_: str, key: str) -> dict[str, Any] | None:
    rows = read_current(q, type_, [key])
    return rows[0] if rows else None

"""Landing raw payloads. The body is stored as the JSON text Garmin sent (DuckDB parses
it itself, so 64-bit ids keep their precision); a new raw_payload row is written only
when the content changed, and raw_latest always records the check.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime

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
    tx.run(
        """INSERT OR REPLACE INTO raw_latest (object_type, object_key, status, content_hash, fetched_at, checked_at)
           VALUES (?, ?, 'ok', ?, (SELECT fetched_at FROM raw_payload WHERE object_type = ? AND object_key = ? AND content_hash = ?), ?)""",
        [type_, key, h, type_, key, h, when],
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


def current(tx: Tx, type_: str, key: str) -> dict | None:
    return tx.one(
        "SELECT status, checked_at, fetched_at, body FROM raw_current WHERE object_type = ? AND object_key = ?",
        [type_, key],
    )

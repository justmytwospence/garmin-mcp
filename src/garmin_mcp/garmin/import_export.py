"""Ingest Garmin's account data export (the zip from garmin.com/account/datamanagement)
into the FIT tables without a single API call.

The export nests zips (DI_CONNECT/DI-Connect-Uploaded-Files/UploadedFiles_*.zip holds
the original activity files, named <activityId>_ACTIVITY.fit). Every .fit found is
decoded: activity files are keyed by the activity id from the filename, or matched to a
mirrored activity by start time; monitoring (wellness) files by their first local
timestamp. Files already in the mirror with the same content are skipped.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from ..fit.decode import decode_messages
from ..sync.engine import SyncEngine

_ACTIVITY_NAME = re.compile(r"(\d{6,})_ACTIVITY\.fit$", re.IGNORECASE)


def iter_fit_files(path: Path) -> Iterator[tuple[str, bytes]]:
    """Every .fit inside the zip, descending into nested zips."""

    def walk(name: str, data: bytes) -> Iterator[tuple[str, bytes]]:
        if name.lower().endswith(".fit"):
            yield name, data
        elif name.lower().endswith(".zip") or data[:2] == b"PK":
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for info in z.infolist():
                        if info.is_dir():
                            continue
                        yield from walk(f"{name}/{info.filename}", z.read(info))
            except zipfile.BadZipFile:
                return

    if path.suffix.lower() == ".fit":
        yield path.name, path.read_bytes()
        return
    yield from walk(path.name, path.read_bytes())


def classify(name: str, data: bytes) -> tuple[str | None, str | None, str]:
    """(kind, key, note) for one FIT file: activity_fit/<id>, wellness_fit/<date>, or None."""
    try:
        messages, _ = decode_messages(data)
    except Exception as err:  # noqa: BLE001
        return None, None, f"undecodable: {err}"
    file_id = (messages.get("file_id_mesgs") or [{}])[0]
    ftype = str(file_id.get("type") or "")
    if ftype == "activity":
        m = _ACTIVITY_NAME.search(name)
        if m:
            return "activity_fit", m.group(1), "activity id from filename"
        sess = (messages.get("session_mesgs") or [{}])[0]
        start = sess.get("start_time") or file_id.get("time_created")
        return "activity_fit", None, f"activity without id in filename; start {start}"
    if ftype.startswith("monitoring"):
        rows = messages.get("monitoring_mesgs") or []
        info = (messages.get("monitoring_info_mesgs") or [{}])[0]
        ts = info.get("local_timestamp") or next((r.get("timestamp") for r in rows if r.get("timestamp")), None) or file_id.get("time_created")
        if ts:
            return "wellness_fit", str(ts)[:10], "wellness day from monitoring timestamps"
        return "wellness_fit", None, "monitoring file without timestamps"
    return None, None, f"file type {ftype or 'unknown'} not mirrored"


def import_zip(engine: SyncEngine, path: Path, limit: int | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {"activity_files": 0, "wellness_files": 0, "skipped": 0, "errors": 0, "notes": []}
    seen = 0
    for name, data in iter_fit_files(path):
        if limit is not None and seen >= limit:
            break
        seen += 1
        kind, key, note = classify(name, data)
        if kind is None:
            summary["skipped"] += 1
            continue
        if key is None and kind == "activity_fit":
            key = match_activity(engine, data)
            if key is None:
                summary["skipped"] += 1
                summary["notes"].append(f"{name}: {note}; no mirrored activity with that start time")
                continue
        try:
            r = engine.fetch.import_file(kind, key, data)
        except Exception as err:  # noqa: BLE001
            summary["errors"] += 1
            summary["notes"].append(f"{name}: {err}")
            continue
        if not r.changed:
            summary["skipped"] += 1
            continue
        summary["activity_files" if kind == "activity_fit" else "wellness_files"] += 1
    if summary["activity_files"] or summary["wellness_files"]:
        engine.plan()
    return summary


def match_activity(engine: SyncEngine, data: bytes) -> str | None:
    try:
        messages, _ = decode_messages(data)
    except Exception:  # noqa: BLE001
        return None
    sess = (messages.get("session_mesgs") or [{}])[0]
    start = sess.get("start_time")
    if not start:
        return None
    try:
        ts = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    except ValueError:
        return None
    row = engine.db.one("SELECT activity_id FROM activity WHERE abs(epoch(start_time_gmt) - epoch(?::TIMESTAMPTZ)) <= 120 ORDER BY abs(epoch(start_time_gmt) - epoch(?::TIMESTAMPTZ)) LIMIT 1", [ts, ts])
    return str(row["activity_id"]) if row else None

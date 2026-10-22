"""Decode FIT files (activity originals and daily wellness bundles) into the fit_*
tables. Every message is landed as JSON into a temp table and projected with the
declarative expressions of fit/tables.py; messages without a typed table go to
fit_message whole, and unmapped fields of typed messages go to their extra column.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from garmin_fit_sdk import Decoder, Stream

from ..db.database import Tx
from ..db.tabledef import insert_sql
from .tables import FIT_MESSAGE_TABLES, FIT_TABLES, fit_file_table, fit_hrv_table, fit_message_table

PARSER_VERSION = 1
FIT_EPOCH = datetime(1989, 12, 31, tzinfo=UTC)


def _jsonable(v: Any) -> Any:
    if isinstance(v, datetime):
        if v.tzinfo is None:
            return v.isoformat()
        return v.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return None
    return v


def unpack(data: bytes) -> list[tuple[str, bytes]]:
    """The FIT files inside a Garmin download (a zip, or a bare .fit)."""
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return [(n, z.read(n)) for n in z.namelist() if n.lower().endswith(".fit")]
    return [("file.fit", data)]


def decode_messages(data: bytes) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    decoder = Decoder(Stream.from_byte_array(data))
    messages, errors = decoder.read(
        convert_datetimes_to_dates=True,
        apply_scale_and_offset=True,
        merge_heart_rates=True,
        expand_sub_fields=True,
        expand_components=True,
        convert_types_to_strings=True,
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for name, rows in messages.items():
        out[str(name)] = [_jsonable(r) for r in rows]
    _resolve_timestamp16(out)
    return out, [str(e) for e in errors]


def _resolve_timestamp16(messages: dict[str, list[dict[str, Any]]]) -> None:
    """Monitoring heart-rate rows carry only timestamp_16; fill timestamp from the last full one."""
    rows = messages.get("monitoring_mesgs")
    if not rows:
        return
    last: datetime | None = None
    for r in rows:
        ts = r.get("timestamp")
        if ts:
            try:
                last = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                last = None
            continue
        t16 = r.get("timestamp_16")
        if last is not None and isinstance(t16, int):
            base = int((last - FIT_EPOCH).total_seconds())
            delta = (t16 - (base & 0xFFFF)) & 0xFFFF
            resolved = FIT_EPOCH + timedelta(seconds=base + delta)
            r["timestamp"] = resolved.isoformat().replace("+00:00", "Z")
            last = resolved


def land_fit(tx: Tx, kind: str, key: str, data: bytes, at: datetime | None = None) -> dict[str, Any]:
    """Decode every FIT file in `data` and (re)build its rows. Returns a summary."""
    at = at or datetime.now(UTC)
    prefix = f"{kind}/{key}/"
    activity_id = int(key) if kind == "activity_fit" else None
    calendar_date = key if kind == "wellness_fit" else None
    for t in FIT_TABLES:
        tx.run(f"DELETE FROM {t.name} WHERE fit_file_id LIKE ?", [prefix.replace("_", "\\_") + "%"])
    tx.run("CREATE OR REPLACE TEMP TABLE _fit_in (fit_file_id VARCHAR, mesg VARCHAR, idx INTEGER, b JSON)")
    summary: dict[str, Any] = {"files": 0, "messages": 0, "errors": []}
    for name, blob in unpack(data):
        fit_file_id = prefix + name
        try:
            messages, errors = decode_messages(blob)
        except Exception as err:  # noqa: BLE001
            summary["errors"].append(f"{name}: {err}")
            continue
        summary["files"] += 1
        summary["errors"].extend(f"{name}: {e}" for e in errors)
        counts = Counter({m: len(rows) for m, rows in messages.items()})
        tx.run("DELETE FROM _fit_in")
        for mesg, rows in messages.items():
            if not rows:
                continue
            tx.run(
                "INSERT INTO _fit_in SELECT ?, ?, unnest(range(?::BIGINT))::INTEGER, unnest(CAST(?::JSON AS JSON[]))",
                [fit_file_id, mesg, len(rows), json.dumps(rows, separators=(",", ":"))],
            )
            summary["messages"] += len(rows)
        ctx = f"'{fit_file_id}' AS fit_file_id, {activity_id if activity_id is not None else 'NULL'}::BIGINT AS activity_id, {('DATE ' + repr(calendar_date)) if calendar_date else 'NULL::DATE'} AS calendar_date"
        file_id = (messages.get("file_id_mesgs") or [{}])[0]
        tx.run(
            insert_sql(
                fit_file_table,
                f"(SELECT '{fit_file_id}' AS fit_file_id, '{kind}' AS kind, ? AS key, ? AS file_name, ?::JSON AS b, ?::JSON AS message_counts, {PARSER_VERSION} AS parser_version, ?::TIMESTAMPTZ AS parsed_at) src",
            ),
            [key, name, json.dumps(file_id), json.dumps(dict(counts)), at],
        )
        for mesg in messages:
            t = FIT_MESSAGE_TABLES.get(mesg)
            if t is not None:
                tx.run(insert_sql(t, f"(SELECT {ctx}, idx, b FROM _fit_in WHERE mesg = '{mesg}') src", conflict="OR IGNORE"))
            elif mesg == "hrv_mesgs":
                tx.run(
                    insert_sql(
                        fit_hrv_table,
                        f"(SELECT {ctx}, idx, pos, rr FROM (SELECT idx, unnest(range(len(rr)))::INTEGER AS pos, unnest(rr) AS rr FROM (SELECT idx, TRY_CAST(b->'time' AS DOUBLE[]) AS rr FROM _fit_in WHERE mesg = 'hrv_mesgs')) WHERE rr IS NOT NULL) src",
                    )
                )
            else:
                tx.run(insert_sql(fit_message_table, f"(SELECT {ctx}, mesg, idx, b FROM _fit_in WHERE mesg = '{mesg}') src"))
            if t is not None or mesg == "hrv_mesgs":
                # Nothing is dropped: the typed table keeps unmapped fields in extra; hrv has only 'time'.
                pass
    tx.run("DROP TABLE IF EXISTS _fit_in")
    return summary

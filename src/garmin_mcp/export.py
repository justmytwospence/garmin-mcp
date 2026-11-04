"""The canonical activity export: every activity's recorded samples in the curves app's canonical format
(schema v1), written from each published snapshot and served at /export/canonical/v1/.

Layout of the export directory (GARMIN_EXPORT_DIR, default <data dir>/export, plus canonical-v1/):

    manifest.json                     schema_version, importer, generated_at and one entry per activity
    activities/garmin-<id>.parquet    one row per recorded second: t, lat, lng, alt, dist, hr, power, cadence

Turned on with GARMIN_EXPORT=canonical; served with bearer EXPORT_TOKEN. The exporter reads the snapshot over
its own read-only DuckDB connection, never the live mirror, and rewrites only activities whose FIT data changed
(a fingerprint of the record count and FIT file ids), so a routine publish writes almost nothing.

Values follow the Data cubes notebook's loader exactly: one row per whole-second timestamp (the row with the
greatest distance wins a tie), t in seconds from the activity's first record, missing values as nulls.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
from starlette.requests import Request
from starlette.responses import FileResponse, Response

log = logging.getLogger("garmin_mcp.export")

SPORTS: dict[str, str] = {
    "running": "run",
    "track_running": "run",
    "trail_running": "trail_run",
    "treadmill_running": "treadmill_run",
    "walking": "walk",
    "hiking": "hike",
    "mountaineering": "hike",
    "snow_shoe_ws": "hike",
    "cycling": "ride",
    "road_biking": "ride",
    "gravel_cycling": "gravel_ride",
    "mountain_biking": "mountain_bike",
    "indoor_cycling": "indoor_ride",
    "virtual_ride": "virtual_ride",
    "backcountry_skiing": "backcountry_ski",
    "resort_skiing": "resort_ski",
    "resort_skiing_snowboarding_ws": "resort_ski",
    "skate_skiing_ws": "skate_ski",
    "cross_country_skiing_ws": "classic_ski",
    "lap_swimming": "swim",
    "open_water_swimming": "swim",
    "rowing": "row",
    "indoor_rowing": "row",
    "kayaking_v2": "paddle",
    "stand_up_paddleboarding_v2": "paddle",
    "paddling": "paddle",
}
FOOT = {"run", "trail_run", "treadmill_run", "walk", "hike"}
WHEEL = {"ride", "gravel_ride", "mountain_bike", "indoor_ride", "virtual_ride"}
INDOOR_TYPES = {"indoor_cycling", "virtual_ride", "treadmill_running", "indoor_rowing"}

_SERVABLE = re.compile(r"manifest\.json|activities/garmin-\d+\.parquet")


def cadence_unit(sport: str) -> str | None:
    """How the FIT file counts cadence: one foot's cycles on foot, pedal revolutions on a bike."""
    if sport in FOOT:
        return "cycles"
    if sport in WHEEL:
        return "rpm"
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CanonicalExporter:
    """Writes the canonical export from a snapshot file. `run` is idempotent and cheap when nothing changed."""

    def __init__(self, out_dir: Path, *, memory_limit: str = "256MB", batch: int = 200, importer: str) -> None:
        self.out_dir = Path(out_dir)
        self.memory_limit = memory_limit
        self.batch = batch
        self.importer = importer

    @property
    def manifest_path(self) -> Path:
        return self.out_dir / "manifest.json"

    def run(self, snapshot: Path) -> dict[str, Any]:
        t_start = time.time()
        activities_dir = self.out_dir / "activities"
        activities_dir.mkdir(parents=True, exist_ok=True)
        stage = self.out_dir.parent / "stage"
        tmp = self.out_dir.parent / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect()
        try:
            con.execute(f"SET memory_limit = '{self.memory_limit}'")
            con.execute("SET threads = 1")
            con.execute(f"SET temp_directory = '{tmp}'")
            con.execute("SET preserve_insertion_order = true")
            con.execute(f"ATTACH '{snapshot}' AS g (READ_ONLY)")
            acts = con.execute(
                """
                WITH s AS (
                    SELECT r.activity_id, count(*) AS n,
                           string_agg(DISTINCT r.fit_file_id, ',' ORDER BY r.fit_file_id) AS files,
                           min(r.timestamp) AS start
                    FROM g.fit_record r
                    WHERE r.timestamp IS NOT NULL AND r.activity_id IS NOT NULL
                    GROUP BY 1
                )
                SELECT s.activity_id, s.n, s.files, s.start, a.activity_type, a.activity_name, a.manufacturer,
                       a.manual_activity,
                       coalesce(round((epoch(a.start_time_local::TIMESTAMP) - epoch(a.start_time_gmt)) / 60)::INTEGER, 0)
                           AS utc_offset_min
                FROM s JOIN g.activity a USING (activity_id)
                WHERE a.deleted_at IS NULL
                ORDER BY s.start, s.activity_id
                """
            ).fetchall()
            previous: dict[str, dict[str, Any]] = {}
            if self.manifest_path.exists():
                previous = {e["id"]: e for e in json.loads(self.manifest_path.read_text()).get("activities", [])}

            need: list[int] = []
            for aid, n, files, *_ in acts:
                old = previous.get(f"garmin:{aid}")
                f = activities_dir / f"garmin-{aid}.parquet"
                if old is None or old.get("fingerprint") != f"{n}|{files}" or not f.exists():
                    need.append(int(aid))

            hashes: dict[int, str] = {}
            for i in range(0, len(need), self.batch):
                ids = need[i : i + self.batch]
                shutil.rmtree(stage, ignore_errors=True)
                con.execute(
                    f"""
                    COPY (
                      WITH r AS (
                        SELECT activity_id, epoch(timestamp)::BIGINT AS ts, position_lat, position_long, altitude, distance,
                               heart_rate, power, cadence, fractional_cadence
                        FROM g.fit_record
                        WHERE activity_id IN ({", ".join(map(str, ids))}) AND timestamp IS NOT NULL
                        QUALIFY row_number() OVER (PARTITION BY activity_id, epoch(timestamp)::BIGINT
                                                   ORDER BY distance DESC NULLS LAST) = 1)
                      SELECT activity_id,
                             (ts - min(ts) OVER (PARTITION BY activity_id))::INTEGER AS t,
                             round(position_lat * 1e7)::INTEGER AS lat, round(position_long * 1e7)::INTEGER AS lng,
                             altitude::DOUBLE AS alt, distance::DOUBLE AS dist, heart_rate::FLOAT AS hr,
                             power::FLOAT AS power, (cadence + coalesce(fractional_cadence, 0))::FLOAT AS cadence
                      FROM r ORDER BY activity_id, t)
                    TO '{stage}' (FORMAT parquet, COMPRESSION snappy, PARTITION_BY (activity_id), OVERWRITE_OR_IGNORE)
                    """
                )
                for aid in ids:
                    parts = sorted((stage / f"activity_id={aid}").glob("*.parquet"))
                    if len(parts) != 1:
                        raise RuntimeError(f"export: expected one parquet file for activity {aid}, found {len(parts)}")
                    dest = activities_dir / f"garmin-{aid}.parquet"
                    os.replace(parts[0], dest)
                    hashes[aid] = _sha256(dest)
        finally:
            con.close()
            shutil.rmtree(stage, ignore_errors=True)

        now = _iso(datetime.now(UTC))
        entries = []
        for aid, n, files, start, atype, name, manufacturer, manual, offset in acts:
            aid = int(aid)
            key = f"garmin:{aid}"
            old = previous.get(key, {})
            sport = SPORTS.get(atype or "", "other")
            entries.append({
                "id": key,
                "sources": [{"kind": "garmin", "id": str(aid)}],
                "start_time": _iso(start),
                "utc_offset_min": int(offset or 0),
                "sport": sport,
                "source_sport": atype,
                "name": name,
                "device": manufacturer,
                "indoor": (atype or "") in INDOOR_TYPES,
                "manual": bool(manual),
                "cadence_unit": cadence_unit(sport),
                "n": int(n),
                "sha256": hashes.get(aid, old.get("sha256")),
                "fingerprint": f"{n}|{files}",
                "updated_at": now if aid in hashes else old.get("updated_at", now),
                "file": f"activities/garmin-{aid}.parquet",
            })
        manifest = {"schema_version": 1, "importer": self.importer, "generated_at": now, "activities": entries}
        tmp_manifest = self.manifest_path.with_suffix(".json.tmp")
        tmp_manifest.write_text(json.dumps(manifest, indent=1))
        os.replace(tmp_manifest, self.manifest_path)

        keep = {Path(e["file"]).name for e in entries}
        removed = 0
        for f in activities_dir.glob("*.parquet"):
            if f.name not in keep:
                f.unlink()
                removed += 1
        stats = {"activities": len(entries), "written": len(hashes), "removed": removed, "seconds": round(time.time() - t_start, 1)}
        log.info("export: %d activities, %d written, %d removed in %.1f s", stats["activities"], stats["written"], stats["removed"], stats["seconds"])
        return stats

    def serve(self, rel: str, request: Request) -> Response:
        """The manifest or one activity's file; 404 for anything else."""
        if not _SERVABLE.fullmatch(rel):
            return Response("Not found", status_code=404, media_type="text/plain")
        path = self.out_dir / rel
        if not path.is_file():
            return Response("Not found", status_code=404, media_type="text/plain")
        media = "application/json" if rel == "manifest.json" else "application/octet-stream"
        return FileResponse(str(path), media_type=media)

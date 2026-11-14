"""The canonical activity export: every activity's recorded samples in the curves app's canonical format
(schema v1), written from each published snapshot and served at /export/canonical/v1/.

Layout of the export directory (GARMIN_EXPORT_DIR, default <data dir>/export, plus canonical-v1/):

    manifest.json                     schema_version, importer, generated_at and one entry per activity
    segments.json                     the segments the entries' segment_efforts reference (device-recorded laps)
    activities/garmin-<id>.parquet    one row per recorded second: t, lat, lng, alt, dist, hr, power, cadence

and, beside the export directory (never served), weather-v1.json: the modelled weather found for each activity
(see weather.py), which the manifest entries carry as `weather`.

Turned on with GARMIN_EXPORT=canonical; served with bearer EXPORT_TOKEN. The exporter reads the snapshot over
its own read-only DuckDB connection, never the live mirror, and rewrites only activities whose FIT data changed
(a fingerprint of the record count and FIT file ids; `n` is the file's row count, one per second), so a routine publish writes almost nothing.

Values follow the Data cubes notebook's loader exactly: one row per whole-second timestamp (the row with the
greatest distance wins, then the earliest record, so the choice is deterministic), t in seconds from the activity's first record, missing values as nulls.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
from starlette.requests import Request
from starlette.responses import FileResponse, Response

from .weather import WeatherCache, fill_weather

log = logging.getLogger("garmin_mcp.export")

SPORTS: dict[str, str] = {
    "running": "run",
    "track_running": "run",
    "street_running": "run",
    "ultra_run": "run",
    "indoor_running": "treadmill_run",
    "virtual_run": "treadmill_run",
    "trail_running": "trail_run",
    "treadmill_running": "treadmill_run",
    "walking": "walk",
    "casual_walking": "walk",
    "speed_walking": "walk",
    "hiking": "hike",
    "mountaineering": "hike",
    "rucking": "hike",
    "snow_shoe_ws": "hike",
    "cycling": "ride",
    "road_biking": "ride",
    "track_cycling": "ride",
    "recumbent_cycling": "ride",
    "cyclocross": "gravel_ride",
    "enduro_mtb": "mountain_bike",
    "gravel_cycling": "gravel_ride",
    "mountain_biking": "mountain_bike",
    "indoor_cycling": "indoor_ride",
    "virtual_ride": "virtual_ride",
    "backcountry_skiing": "backcountry_ski",
    "backcountry_skiing_snowboarding_ws": "backcountry_ski",
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
INDOOR_TYPES = {"indoor_cycling", "virtual_ride", "treadmill_running", "indoor_running", "virtual_run", "indoor_rowing"}
# FIT rider_position_change values from power pedals: standing (1) or seated (0) from that second on
RIDER_POSITIONS = {"standing": 1, "transition_to_standing": 1, "seated": 0, "transition_to_seated": 0}

_SERVABLE = re.compile(r"manifest\.json|segments\.json|activities/garmin-\d+\.parquet")


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

    def __init__(
        self, out_dir: Path, *, memory_limit: str = "256MB", batch: int = 200, importer: str, weather: bool = False, weather_session: Any = None,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.memory_limit = memory_limit
        self.batch = batch
        self.importer = importer
        self.weather = weather
        self.weather_session = weather_session

    def _weather(self, acts: list[tuple[Any, ...]], activities_dir: Path) -> dict[int, dict[str, Any] | None]:
        """Fill in the modelled weather of the activities that lack it (bounded, and never a reason to fail the export);
        each activity's weather as cached for its current fingerprint."""
        cache = WeatherCache(self.out_dir.parent / "weather-v1.json")
        try:
            session = self.weather_session
            if session is None:
                import requests

                session = requests.Session()
                session.headers["User-Agent"] = f"{self.importer} (+https://github.com/justmytwospence/garmin-mcp)"
            cands = [
                {"id": f"garmin:{int(a[0])}", "fingerprint": a[2], "start_time": _iso(a[3]), "indoor": (a[4] or "") in INDOOR_TYPES, "manual": bool(a[7])}
                for a in acts
            ]
            cache.forget({c["id"] for c in cands})
            con = duckdb.connect()
            try:

                def read(aid: str) -> tuple[list[int], list[int | None], list[int | None], list[float | None]]:
                    path = activities_dir / f"{aid.replace(':', '-')}.parquet"
                    rows = con.execute("SELECT t, lat, lng, alt FROM read_parquet(?) ORDER BY t", [str(path)]).fetchall()
                    return [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows]

                stats = fill_weather(cache, cands, read, session)
            finally:
                con.close()
            if any(stats.values()):
                log.info("export weather: %s", stats)
        except Exception:  # noqa: BLE001
            log.exception("export weather failed")
        return {int(a[0]): cache.weather(f"garmin:{int(a[0])}", a[2]) for a in acts}

    @property
    def manifest_path(self) -> Path:
        return self.out_dir / "manifest.json"

    @property
    def segments_path(self) -> Path:
        return self.out_dir / "segments.json"

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
                    SELECT r.activity_id, count(*) AS records, count(DISTINCT epoch(r.timestamp)::BIGINT) AS n,
                           string_agg(DISTINCT r.fit_file_id, ',' ORDER BY r.fit_file_id) AS files,
                           min(r.timestamp) AS start
                    FROM g.fit_record r
                    WHERE r.timestamp IS NOT NULL AND r.activity_id IS NOT NULL
                    GROUP BY 1
                )
                SELECT s.activity_id, s.n, s.records || '|' || s.files AS fingerprint, s.start, a.activity_type, a.activity_name, a.manufacturer,
                       a.manual_activity, coalesce(h.hr_sensor, false) AS hr_sensor, a.event_type,
                       coalesce(round((epoch(a.start_time_local::TIMESTAMP) - epoch(a.start_time_gmt)) / 60)::INTEGER, 0)
                           AS utc_offset_min
                FROM s JOIN g.activity a USING (activity_id)
                LEFT JOIN (
                    -- a heart-rate strap or armband was connected (the Data cubes notebook's rule)
                    SELECT activity_id, bool_or(antplus_device_type = 'heart_rate'
                                                OR (source_type = 'bluetooth_low_energy' AND device_type = '1')
                                                OR garmin_product ILIKE 'hrm%') AS hr_sensor
                    FROM g.fit_device_info WHERE activity_id IS NOT NULL GROUP BY 1
                ) h USING (activity_id)
                WHERE a.deleted_at IS NULL
                ORDER BY s.start, s.activity_id
                """
            ).fetchall()
            laps = con.execute(
                """
                WITH l AS (SELECT activity_id, fields FROM g.fit_message WHERE mesg = 'segment_lap_mesgs')
                SELECT activity_id, json_extract_string(fields, '$.uuid') AS uuid, json_extract_string(fields, '$.manufacturer') AS mfr,
                       json_extract_string(fields, '$.name') AS name,
                       epoch(json_extract_string(fields, '$.start_time')::TIMESTAMPTZ) AS st,
                       epoch(json_extract_string(fields, '$.timestamp')::TIMESTAMPTZ) AS en,
                       try_cast(json_extract_string(fields, '$.total_distance') AS DOUBLE) AS dist,
                       try_cast(json_extract_string(fields, '$.start_position_lat') AS BIGINT) AS slat,
                       try_cast(json_extract_string(fields, '$.start_position_long') AS BIGINT) AS slng,
                       try_cast(json_extract_string(fields, '$.end_position_lat') AS BIGINT) AS elat,
                       try_cast(json_extract_string(fields, '$.end_position_long') AS BIGINT) AS elng
                FROM l
                WHERE json_extract_string(fields, '$.status') = 'end' AND json_extract_string(fields, '$.uuid') IS NOT NULL
                """
            ).fetchall()
            positions = con.execute(
                """
                SELECT activity_id, epoch(timestamp) AS ts, rider_position FROM g.fit_event
                WHERE event = 'rider_position_change' AND rider_position IS NOT NULL AND activity_id IS NOT NULL AND timestamp IS NOT NULL
                ORDER BY activity_id, ts, idx
                """
            ).fetchall()
            previous: dict[str, dict[str, Any]] = {}
            if self.manifest_path.exists():
                previous = {e["id"]: e for e in json.loads(self.manifest_path.read_text()).get("activities", [])}

            need: list[int] = []
            for aid, _n, fingerprint, *_ in acts:
                old = previous.get(f"garmin:{aid}")
                f = activities_dir / f"garmin-{aid}.parquet"
                if old is None or old.get("fingerprint") != fingerprint or not f.exists():
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
                                                   ORDER BY distance DESC NULLS LAST, fit_file_id, idx) = 1)
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

        weathers = self._weather(acts, activities_dir) if self.weather else {}
        now = _iso(datetime.now(UTC))
        starts = {int(a[0]): a[3].timestamp() for a in acts}
        rider: dict[int, dict[str, list[int]]] = {}
        for aid, ts, position in positions:
            aid = int(aid)
            state = RIDER_POSITIONS.get(position)
            if aid not in starts or state is None:
                continue
            r = rider.setdefault(aid, {"t": [], "standing": []})
            t = max(0, round(ts - starts[aid]))
            if r["t"] and r["t"][-1] == t:  # the later of two events in a second wins
                r["standing"][-1] = state
            else:
                r["t"].append(t)
                r["standing"].append(state)
        for aid in list(rider):
            # collapse changes to the state already held (seated before the first event)
            kept: dict[str, list[int]] = {"t": [], "standing": []}
            held = 0
            for t, st in zip(rider[aid]["t"], rider[aid]["standing"], strict=True):
                if st != held:
                    kept["t"].append(t)
                    kept["standing"].append(st)
                    held = st
            if kept["t"]:
                rider[aid] = kept
            else:
                del rider[aid]
        efforts: dict[int, list[dict[str, Any]]] = {}
        seg_laps: dict[str, list[tuple[Any, ...]]] = {}
        seg_first: dict[str, int] = {}  # the activity of a segment's first effort (acts are sorted by start)
        order = {int(a[0]): i for i, a in enumerate(acts)}
        for aid, uuid, mfr, lname, st, en, dist, slat, slng, elat, elng in sorted(laps, key=lambda r: (r[4] is None, r[4], r[1])):
            aid = int(aid)
            if aid not in starts or st is None or en is None:
                continue
            t0, t1 = round(st - starts[aid]), round(en - starts[aid])
            if not 0 <= t0 < t1:
                continue
            seg = f"{'strava' if mfr == 'strava' else 'garmin'}:{uuid}"
            efforts.setdefault(aid, []).append({"segment": seg, "t0": t0, "t1": t1, "from": "device", "src": f"garmin:{aid}"})
            seg_laps.setdefault(seg, []).append((st, lname, dist, slat, slng, elat, elng))
            if seg not in seg_first or order[aid] < order[seg_first[seg]]:
                seg_first[seg] = aid
        for lst in efforts.values():
            lst.sort(key=lambda e: (e["t0"], e["segment"]))
        def degrees(v: int | None) -> float | None:
            return None if v is None else round(v * 180 / 2**31, 6)

        def point(lat: int | None, lng: int | None) -> list[float] | None:
            return None if lat is None or lng is None else [degrees(lat), degrees(lng)]  # type: ignore[list-item]

        seg_acts = {int(a[0]): a[4] for a in acts}
        segments = []
        for seg in sorted(seg_laps):
            ls = sorted(seg_laps[seg], key=lambda r: r[0], reverse=True)
            newest = ls[0]
            dists = [r[2] for r in ls if r[2] is not None]
            segments.append({
                "id": seg,
                "name": newest[1],
                "sport": SPORTS.get(seg_acts[seg_first[seg]] or "", "other"),
                "distance": statistics.median(dists) if dists else None,
                "start": point(newest[3], newest[4]),
                "end": point(newest[5], newest[6]),
                "starred": False,
                "from": "device",
                "created_from": None,
            })
        entries = []
        for aid, n, fingerprint, start, atype, name, manufacturer, manual, hr_sensor, event_type, offset in acts:
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
                "hr_sensor": bool(hr_sensor),
                "tags": ["race"] if event_type == "race" else [],
                "weather": weathers.get(aid),
                "segment_efforts": efforts.get(aid, []),
                "rider_position": rider.get(aid),
                "n": int(n),
                "sha256": hashes.get(aid, old.get("sha256")),
                "fingerprint": fingerprint,
                "updated_at": now if aid in hashes else old.get("updated_at", now),
                "file": f"activities/garmin-{aid}.parquet",
            })
        manifest = {"schema_version": 1, "importer": self.importer, "generated_at": now, "activities": entries}
        tmp_manifest = self.manifest_path.with_suffix(".json.tmp")
        tmp_manifest.write_text(json.dumps(manifest, indent=1))
        os.replace(tmp_manifest, self.manifest_path)
        seg_doc = {"schema_version": 1, "importer": self.importer, "generated_at": now, "segments": segments}
        tmp_segments = self.segments_path.with_suffix(".json.tmp")
        tmp_segments.write_text(json.dumps(seg_doc, indent=1))
        os.replace(tmp_segments, self.segments_path)

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
        media = "application/json" if rel in ("manifest.json", "segments.json") else "application/octet-stream"
        return FileResponse(str(path), media_type=media)

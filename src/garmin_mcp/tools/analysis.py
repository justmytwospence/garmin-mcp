"""Activity analysis and courses (upstream activity_analysis, courses). FIT analyses
are computed from the mirror's decoded FIT tables (fit_session, fit_lap, fit_record,
fit_event, fit_hrv); the original file is kept on disk, so downloads cost no Garmin
request once the mirror holds it."""

from __future__ import annotations

import json
import os
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from ..fit.decode import decode_messages, unpack
from ..mirror import GarminNotFound, Provenance, note_provenance
from .base import DESTRUCTIVE, READ, WRITE, NoData, Registry, ToolFailure, WriteResult, activity_key, drop_none
from .fit_analysis import (
    _PDC_LABELS,
    _compute_hr_drift,
    _compute_hrv_metrics,
    _compute_power_duration_curve,
    _compute_shift_summary,
    _compute_temperature_stats,
    _decode_gear_change,
    _decode_left_right_balance,
    _derive_grades,
    _detect_climbs,
    _fill_shift_grades,
    _grade_analysis,
    _primary_session_index,
    _records_in_session,
)
from .payloads import _build_course_payload, _gpx_decimal

DEFAULT_FIT_CONFIG = "~/.garminconnect_fit_config.json"
COURSE_ACTIVITY_TYPE_IDS = {"running": 1, "cycling": 2, "hiking": 3, "walking": 9, "trail_running": 6, "mountain_biking": 5, "road_biking": 10, "gravel_cycling": 143}
MAX_MESSAGE_LIMIT = 5000


def fit_config_path() -> str:
    return os.getenv("GARMIN_FIT_CONFIG") or DEFAULT_FIT_CONFIG


def read_fit_config() -> dict[str, Any]:
    try:
        data = json.loads(Path(fit_config_path()).expanduser().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def resolve_download_dir(output_dir: str | None) -> str | None:
    if output_dir:
        return os.path.abspath(os.path.expanduser(output_dir))
    env = os.getenv("GARMIN_FIT_DOWNLOAD_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    cfg = read_fit_config().get("download_dir")
    return os.path.abspath(os.path.expanduser(cfg)) if cfg else None


def resolve_gpx_output_path(course_id: int, output_path: str | None) -> str:
    if output_path:
        p = os.path.abspath(os.path.expanduser(output_path))
        if os.path.isdir(p) or output_path.endswith(("/", "\\")):
            return os.path.join(p, f"{course_id}.gpx")
        if not p.lower().endswith(".gpx"):
            raise ToolFailure(f"output_path must be a directory or a .gpx file, got: {output_path}")
        return p
    return os.path.join(resolve_download_dir(None) or os.path.abspath("./courses"), f"{course_id}.gpx")


def _ts(v: Any) -> str:
    return v if isinstance(v, str) else (v.isoformat() if v is not None else "")


def _session_from_row(r: dict[str, Any]) -> dict[str, Any]:
    s = {
        "sport": r.get("sport"), "sub_sport": r.get("sub_sport"), "start_time": _ts(r.get("start_time")), "total_elapsed_time_s": r.get("total_elapsed_time"), "total_timer_time_s": r.get("total_timer_time"),
        "total_distance_m": r.get("total_distance"), "total_calories": r.get("total_calories"), "avg_speed_mps": r.get("enhanced_avg_speed") if r.get("enhanced_avg_speed") is not None else r.get("avg_speed"),
        "max_speed_mps": r.get("enhanced_max_speed") if r.get("enhanced_max_speed") is not None else r.get("max_speed"), "avg_power_w": r.get("avg_power"), "max_power_w": r.get("max_power"), "normalized_power_w": r.get("normalized_power"),
        "avg_cadence_rpm": r.get("avg_cadence"), "max_cadence_rpm": r.get("max_cadence"), "avg_heart_rate_bpm": r.get("avg_heart_rate"), "max_heart_rate_bpm": r.get("max_heart_rate"), "total_ascent_m": r.get("total_ascent"), "total_descent_m": r.get("total_descent"),
        "total_training_effect": r.get("total_training_effect"), "avg_left_pco_mm": r.get("avg_left_pco"), "avg_right_pco_mm": r.get("avg_right_pco"), "avg_left_torque_effectiveness_pct": r.get("avg_left_torque_effectiveness"),
        "avg_right_torque_effectiveness_pct": r.get("avg_right_torque_effectiveness"), "avg_left_pedal_smoothness_pct": r.get("avg_left_pedal_smoothness"), "avg_right_pedal_smoothness_pct": r.get("avg_right_pedal_smoothness"),
    }
    for side in ("left", "right"):
        phase = r.get(f"avg_{side}_power_phase")
        if isinstance(phase, (list, tuple)) and phase:
            s[f"avg_{side}_power_phase_start_deg"] = phase[0]
            if len(phase) > 1:
                s[f"avg_{side}_power_phase_end_deg"] = phase[1]
    left = _decode_left_right_balance(r.get("left_right_balance"))
    if left is not None:
        s["avg_left_power_pct"], s["avg_right_power_pct"] = left, round(100.0 - left, 1)
    s = drop_none(s)
    if s.get("normalized_power_w") and s.get("avg_power_w"):
        s["variability_index"] = round(s["normalized_power_w"] / s["avg_power_w"], 3)
    return s


def _lap_from_row(i: int, r: dict[str, Any]) -> dict[str, Any]:
    lap = {
        "lap_number": i, "start_time": _ts(r.get("start_time")), "total_elapsed_time_s": r.get("total_elapsed_time"), "total_distance_m": r.get("total_distance"), "avg_speed_mps": r.get("enhanced_avg_speed") if r.get("enhanced_avg_speed") is not None else r.get("avg_speed"),
        "avg_power_w": r.get("avg_power"), "max_power_w": r.get("max_power"), "normalized_power_w": r.get("normalized_power"), "avg_cadence_rpm": r.get("avg_cadence"), "avg_heart_rate_bpm": r.get("avg_heart_rate"), "avg_left_pco_mm": r.get("avg_left_pco"), "avg_right_pco_mm": r.get("avg_right_pco"),
        "avg_left_torque_effectiveness_pct": r.get("avg_left_torque_effectiveness"), "avg_right_torque_effectiveness_pct": r.get("avg_right_torque_effectiveness"), "avg_left_pedal_smoothness_pct": r.get("avg_left_pedal_smoothness"), "avg_right_pedal_smoothness_pct": r.get("avg_right_pedal_smoothness"),
        "total_ascent_m": r.get("total_ascent"), "total_descent_m": r.get("total_descent"),
    }
    if lap["total_distance_m"] is not None and lap["total_distance_m"] < 0:
        lap["total_distance_m"] = None
        lap["distance_invalid"] = True
    left = _decode_left_right_balance(r.get("left_right_balance"))
    if left is not None:
        lap["avg_left_power_pct"], lap["avg_right_power_pct"] = left, round(100.0 - left, 1)
    lap = drop_none(lap)
    if lap.get("normalized_power_w") and lap.get("avg_power_w"):
        lap["variability_index"] = round(lap["normalized_power_w"] / lap["avg_power_w"], 3)
    return lap


def _record_from_row(r: dict[str, Any]) -> dict[str, Any]:
    rec = {
        "timestamp": _ts(r.get("timestamp")), "power_w": r.get("power"), "cadence_rpm": r.get("cadence"), "heart_rate_bpm": r.get("heart_rate"), "speed_mps": r.get("speed"), "altitude_m": r.get("altitude"), "distance_m": r.get("distance"),
        "grade_pct": r.get("grade"), "temperature_c": r.get("temperature"), "lat_deg": r.get("position_lat"), "lon_deg": r.get("position_long"), "left_pco_mm": r.get("left_pco"), "right_pco_mm": r.get("right_pco"),
        "left_torque_effectiveness_pct": r.get("left_torque_effectiveness"), "right_torque_effectiveness_pct": r.get("right_torque_effectiveness"), "left_pedal_smoothness_pct": r.get("left_pedal_smoothness"), "right_pedal_smoothness_pct": r.get("right_pedal_smoothness"),
    }
    bal = _decode_left_right_balance(r.get("left_right_balance"), per_record=True)
    if bal is not None:
        rec["left_power_pct"] = bal
    for side in ("left", "right"):
        phase = r.get(f"{side}_power_phase")
        if isinstance(phase, (list, tuple)) and phase:
            rec[f"{side}_power_phase_start_deg"] = phase[0]
            if len(phase) > 1:
                rec[f"{side}_power_phase_end_deg"] = phase[1]
    return drop_none(rec)


def register(reg: Registry) -> None:
    m = reg.mirror

    def ensure_fit(key: str) -> dict[str, Any]:
        try:
            return m.activity_fit(key).data
        except GarminNotFound as err:
            raise NoData(f"No original FIT file for activity {key}: {err}") from err

    def rider_weight(activity_date: str) -> tuple[float, str] | None:
        since = (date.fromisoformat(activity_date) - timedelta(days=90)).isoformat()
        row = m.row("SELECT weight, calendar_date FROM weigh_in WHERE calendar_date BETWEEN ?::DATE AND ?::DATE AND weight IS NOT NULL ORDER BY calendar_date DESC, timestamp_gmt DESC LIMIT 1", [since, activity_date])
        if row:
            return round(row["weight"] / 1000, 2), row["calendar_date"]
        return None

    @reg.tool(annotations=READ)
    def get_activity_fit_messages(activity_id: int | str, message_types: list[str] | None = None, include_records: bool = False, message_offset: int = 0, message_limit: int = 1000) -> Any:
        """Retrieve and generically parse an activity's original FIT file (the source of truth for device data): every message and
        field the FIT decoder emits, in file order, without sport-specific curation. Service-side Garmin Connect edits are not merged.

        High-frequency record messages are counted but omitted by default; other message types occurring more than 100 times are
        inventoried but omitted from an unfiltered response. Request types explicitly with message_types (snake_case FIT names, e.g.
        ["session", "lap", "set", "exercise_title"] for strength). Results are bounded by message_offset/message_limit (max 5000);
        follow pagination.next_offset until absent.

        Args:
            activity_id: Garmin activity ID.
            message_types: Optional list of FIT message types to return. Omit to return low-frequency non-record message types.
            include_records: Include the high-frequency record stream (default false).
            message_offset: Zero-based offset within the selected message stream.
            message_limit: Maximum messages returned per call, 1-5000 (default 1000).
        """
        key = activity_key(activity_id)
        row = ensure_fit(key)
        message_limit = max(1, min(int(message_limit), MAX_MESSAGE_LIMIT))
        message_offset = max(0, int(message_offset))
        data = Path(row["path"]).read_bytes()
        files = unpack(data)
        if not files:
            raise ToolFailure(f"The stored download for activity {key} contains no FIT file.")
        messages, errors = decode_messages(files[0][1])
        by_type = {name.removesuffix("_mesgs"): rows for name, rows in messages.items()}
        counts = {k: len(v) for k, v in by_type.items()}
        wanted = {t.strip().lower() for t in (message_types or [])}
        if wanted:
            selected = [t for t in by_type if t in wanted]
        else:
            selected = [t for t, n in counts.items() if t != "record" and n <= 100]
            if include_records:
                selected.append("record")
        stream = [{"type": t, "index": i, "fields": msg} for t in selected for i, msg in enumerate(by_type.get(t, []))]
        page = stream[message_offset : message_offset + message_limit]
        out: dict[str, Any] = {"activity_id": int(key), "file": files[0][0], "message_counts": counts, "selected_types": selected, "returned": len(page), "messages": page, "decoder_errors": errors[:10]}
        if message_offset + message_limit < len(stream):
            out["pagination"] = {"next_offset": message_offset + message_limit, "total_selected": len(stream)}
        unknown = sorted(wanted - set(by_type))
        if unknown:
            out["unknown_message_types"] = unknown
        return out

    def load_records(key: str, fit_file_id: str | None = None) -> list[dict[str, Any]]:
        where = "activity_id = ?::BIGINT" + (" AND fit_file_id = ?" if fit_file_id else "")
        params: list[Any] = [key] + ([fit_file_id] if fit_file_id else [])
        rows = m.rows(f"SELECT timestamp, power, cadence, heart_rate, speed, altitude, distance, grade, temperature, position_lat, position_long, left_right_balance, left_pco, right_pco, left_torque_effectiveness, right_torque_effectiveness, left_pedal_smoothness, right_pedal_smoothness, left_power_phase, right_power_phase FROM fit_record WHERE {where} ORDER BY idx", params)
        return [_record_from_row(r) for r in rows]

    @reg.tool(annotations=READ)
    def get_activity_fit_data(activity_id: int | str, include_records: bool = False) -> Any:
        """Analyse an activity's original FIT file (from the mirror's decoded tables) for data the REST API lacks: Di2/eTap shift
        events with cadence, grade and gear at each shift and shift quality (proactive 70-100 rpm, reactive <70, coasting 0,
        spun_out >100); cycling dynamics per session and lap (PCO, left/right balance, torque effectiveness, pedal smoothness);
        variability index; climb detection with VAM and W/kg (latest weigh-in up to 90 days before the ride); grade-correlated
        power/cadence/HR (grade from the device when present, else derived from altitude and distance; grade_source says which);
        HR drift for rides of 60 min or more; temperature correlation; the power duration curve (5s to 60min); HRV from RR intervals;
        optional full per-second records. Multisport files report every leg in sessions and analyse the cycling or longest leg.

        Args:
            activity_id: Garmin activity ID
            include_records: Include full per-second time series (default False). Adds significant volume for long rides.
        """
        key = activity_key(activity_id)
        ensure_fit(key)
        sess_rows = m.rows("SELECT * FROM fit_session WHERE activity_id = ?::BIGINT ORDER BY fit_file_id, message_index", [key])
        sessions = [_session_from_row(r) for r in sess_rows]
        laps = [_lap_from_row(i + 1, r) for i, r in enumerate(m.rows("SELECT * FROM fit_lap WHERE activity_id = ?::BIGINT ORDER BY fit_file_id, message_index", [key]))]
        records = load_records(key)
        # Shift events with the cadence and grade of the preceding record.
        events = m.rows("SELECT timestamp, event, data, extra FROM fit_event WHERE activity_id = ?::BIGINT AND event IN ('rear_gear_change', 'front_gear_change', 'gear_change') ORDER BY idx", [key])
        shifts: list[dict[str, Any]] = []
        shift_counts: list[tuple[dict[str, Any], int]] = []
        ri = 0
        last_cad = None
        last_grade = None
        for ev in events:
            ts = ev["timestamp"] or ""
            while ri < len(records) and records[ri].get("timestamp", "") <= ts:
                if records[ri].get("cadence_rpm") is not None:
                    last_cad = records[ri]["cadence_rpm"]
                if records[ri].get("grade_pct") is not None:
                    last_grade = records[ri]["grade_pct"]
                ri += 1
            entry: dict[str, Any] = {"timestamp": ts, "event": ev["event"], "cadence_at_shift_rpm": last_cad}
            if last_grade is not None:
                entry["grade_at_shift_pct"] = round(last_grade, 1)
            raw = ev.get("data")
            if raw is None and isinstance(ev.get("extra"), str):
                raw = json.loads(ev["extra"]).get("gear_change_data")
            if raw is not None:
                try:
                    decoded = _decode_gear_change(int(raw))
                    entry.update(decoded)
                    if decoded.get("front_teeth") and decoded.get("rear_teeth"):
                        entry["gear_combo"] = f"{decoded['front_teeth']}/{decoded['rear_teeth']}t"
                except (TypeError, ValueError):
                    entry["gear_change_data_raw"] = str(raw)
            entry["quality"] = "unknown" if last_cad is None else "coasting" if last_cad == 0 else "reactive" if last_cad < 70 else "spun_out" if last_cad > 100 else "proactive"
            entry = drop_none(entry)
            shifts.append(entry)
            shift_counts.append((entry, ri))
        multisport = len(sessions) > 1
        analysis_records = records
        note = None
        analyzed = 0
        if multisport:
            analyzed = _primary_session_index(sessions)
            session = dict(sessions[analyzed])
            sub = _records_in_session(records, session)
            if sub is None:
                note = "Multisport file: session summarises one leg, but record timestamps or the session window are missing, so record-based analyses cover all legs."
            else:
                analysis_records = sub
                note = f"Multisport file: session and record-based analyses cover leg {analyzed} ({session.get('sport', 'unknown')}) only. See sessions for every leg."
        else:
            session = sessions[0] if sessions else {}
        grade_source = _derive_grades(records) if records else None
        if grade_source == "derived":
            _fill_shift_grades(shift_counts, records)
        if analysis_records:
            for k, fn in (("grade_analysis", _grade_analysis), ("hr_drift", _compute_hr_drift), ("temperature_stats", _compute_temperature_stats)):
                v = fn(analysis_records)
                if v:
                    session[k] = v
        result: dict[str, Any] = {"activity_id": int(key), "session": session, "laps": laps, "shift_summary": _compute_shift_summary(shifts), "shifts": shifts}
        if multisport:
            result.update({"multisport": True, "sessions": sessions, "analyzed_session_index": analyzed, "analysis_note": note})
        climbs = _detect_climbs(analysis_records) if analysis_records else []
        if climbs:
            result["climbs"] = climbs
        pdc = _compute_power_duration_curve(analysis_records) if analysis_records else None
        if pdc:
            result["power_duration_curve"] = pdc
        if grade_source:
            result["grade_source"] = grade_source
        rr = [r["rr_interval"] for r in m.rows("SELECT rr_interval FROM fit_hrv WHERE activity_id = ?::BIGINT ORDER BY idx, pos", [key]) if r["rr_interval"] is not None and 0.2 < r["rr_interval"] < 3.0]
        if rr:
            hrv = _compute_hrv_metrics(rr)
            if hrv:
                result["hrv"] = hrv
        start = session.get("start_time", "")[:10]
        if start:
            w = rider_weight(start)
            if w:
                result["rider_weight_kg"], result["rider_weight_date"] = w
                for k, dst in (("avg_power_w", "avg_w_per_kg"), ("normalized_power_w", "normalized_w_per_kg")):
                    if session.get(k):
                        session[dst] = round(session[k] / w[0], 2)
                for c in result.get("climbs") or []:
                    if c.get("avg_power_w"):
                        c["avg_w_per_kg"] = round(c["avg_power_w"] / w[0], 2)
        if include_records:
            result["records"] = records
        else:
            result["record_count"] = len(records)
        note_provenance(Provenance("mirror", None, notes=["analysis computed from the mirror's decoded FIT tables"]))
        return result

    @reg.tool(annotations=READ)
    def get_power_duration_curve(num_activities: int = 20, activity_type: str = "cycling") -> Any:
        """Get the season-best Power Duration Curve across recent activities of a type: best mean maximal power at 5s, 30s, 1min, 5min,
        10min, 20min and 60min with the activity and date each best came from, computed from the mirror's FIT records (activities
        whose file is not mirrored yet are fetched first). Use the 20-minute best x 0.95 as an FTP estimate.

        Args:
            num_activities: Number of recent activities matching activity_type to request (default 20, max 50)
            activity_type: Activity type to filter (default "cycling")
        """
        n = max(1, min(int(num_activities), 50))
        t = activity_type.lower()
        where = "(lower(activity_type) LIKE ? OR (? = 'cycling' AND (parent_type_id = 2 OR activity_type LIKE '%biking%' OR activity_type LIKE '%cycling%')))"
        rows = m.rows(f"SELECT activity_id, activity_name, start_time_local, activity_type, avg_power FROM activity WHERE deleted_at IS NULL AND NOT coalesce(is_manual_activity, false) AND {where} ORDER BY start_time_gmt DESC LIMIT {n}", [f"%{t}%", t])
        if not rows:
            return "No activities found."
        bests: dict[str, dict[str, Any]] = {}
        processed = skipped = 0
        for r in rows:
            key = str(r["activity_id"])
            try:
                ensure_fit(key)
            except ToolFailure:
                skipped += 1
                continue
            curve = _compute_power_duration_curve(load_records(key))
            if not curve:
                skipped += 1
                continue
            processed += 1
            for label, watts in curve.items():
                if label not in bests or watts > bests[label]["watts"]:
                    bests[label] = {"watts": watts, "activity_id": r["activity_id"], "activity_name": r["activity_name"], "date": (r["start_time_local"] or "")[:10]}
        if not bests:
            return "No power data found in the selected activities."
        ordered = {k: bests[k] for k in _PDC_LABELS if k in bests}
        ftp = round(ordered["20min"]["watts"] * 0.95) if "20min" in ordered else None
        note_provenance(Provenance("mirror", None, notes=["power curve computed from the mirror's FIT records"]))
        return {"activity_type": activity_type, "activities_analyzed": processed, "activities_skipped": skipped, "ftp_estimate_w": ftp, "ftp_note": "Estimated as 95% of 20-minute best power", "season_bests": ordered}

    @reg.tool(annotations=READ)
    def download_activity_file(activity_id: int | str, format: str = "fit", output_dir: str | None = None) -> Any:  # noqa: A002
        """Download an activity and save it to disk: the original .fit (copied from the mirror, no Garmin request), or gpx, tcx, csv
        exports fetched from Garmin. Directory: output_dir, else GARMIN_FIT_DOWNLOAD_DIR, else the persisted set_fit_download_dir;
        with none configured returns status "needs_setup". Files are named {activity_id}.{ext} and overwrite existing ones.

        Args:
            activity_id: Garmin activity ID
            format: One of fit, gpx, tcx, csv (default fit)
            output_dir: Optional one-off directory override (not persisted)
        """
        key = activity_key(activity_id)
        fmt = format.lower().strip()
        if fmt not in ("fit", "gpx", "tcx", "csv"):
            raise ToolFailure(f"Invalid format '{format}'. Valid formats: fit, gpx, tcx, csv")
        target_dir = resolve_download_dir(output_dir)
        if not target_dir:
            return {"status": "needs_setup", "suggested_default": os.getcwd(), "config_path": os.path.expanduser(fit_config_path()), "message": "No download directory configured. Ask the user where to save, call set_fit_download_dir(path), then call this tool again."}
        os.makedirs(target_dir, exist_ok=True)
        if fmt == "fit":
            row = ensure_fit(key)
            files = unpack(Path(row["path"]).read_bytes())
            if not files:
                raise ToolFailure("The stored download contains no FIT file.")
            payload = files[0][1]
        else:
            payload = m.client.get_bytes(f"/download-service/export/{fmt}/activity/{key}", priority="interactive")
            note_provenance(Provenance("garmin", None))
            if not payload:
                raise ToolFailure(f"Garmin returned no {fmt} export for activity {key}.")
        path = os.path.join(target_dir, f"{key}.{fmt}")
        Path(path).write_bytes(payload)
        return {"status": "success", "activity_id": int(key), "format": fmt, "file_path": os.path.abspath(path), "size_bytes": len(payload), "message": "Activity file saved."}

    @reg.tool(annotations=WRITE)
    def set_fit_download_dir(path: str) -> Any:
        """Set and persist the default directory for downloaded activity files (in ~/.garminconnect_fit_config.json, overridable via GARMIN_FIT_CONFIG).

        Args:
            path: Directory where activity files (.fit/.gpx/.tcx/.csv) are saved.
        """
        abspath = os.path.abspath(os.path.expanduser(path))
        os.makedirs(abspath, exist_ok=True)
        cfg_path = Path(fit_config_path()).expanduser()
        cfg = read_fit_config()
        cfg["download_dir"] = abspath
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        note_provenance(Provenance("mirror", None, notes=["local configuration only; no Garmin request"]))
        return {"status": "success", "download_dir": abspath, "config_path": str(cfg_path), "message": "Default FIT download directory configured."}

    # ----------------------------------------------------------------- courses

    def course_detail(course_id: int) -> dict[str, Any]:
        try:
            data = m.snapshot("course", str(int(course_id))).data
        except GarminNotFound as err:
            raise ToolFailure(f"Course {course_id} not found ({err})") from err
        if not isinstance(data, dict):
            raise ToolFailure(f"Course {course_id} not found")
        return data

    @reg.tool(annotations=READ)
    def get_courses() -> Any:
        """List all courses saved on Garmin Connect (id, name, distance, activity type, creation date)."""
        data = m.optional("courses", default=[]) or []
        items = data if isinstance(data, list) else data.get("courses") or []
        out = [{"course_id": c.get("courseId"), "name": c.get("courseName"), "distance_m": c.get("distanceInMeters"), "elevation_gain_m": c.get("elevationGainInMeters"), "elevation_loss_m": c.get("elevationLossInMeters"),
                "activity": (c.get("activityType") or {}).get("typeKey"), "has_pace_band": c.get("hasPaceBand"), "created": c.get("createdDateFormatted")} for c in items if isinstance(c, dict)]
        return {"count": len(out), "courses": out}

    @reg.tool(annotations=READ)
    def get_course_details(course_id: int) -> Any:
        """Get full details of a Garmin Connect course: metadata, elevation gain/loss, distance and all custom course waypoints.

        Args:
            course_id: ID of the course (from get_courses).
        """
        data = course_detail(course_id)
        points = [{"name": cp.get("name"), "type": cp.get("pointType"), "lat": cp.get("lat"), "lon": cp.get("lon"), "distance_m": cp.get("distance")} for cp in data.get("coursePoints") or []]
        return {"course_id": data.get("courseId"), "name": data.get("courseName"), "distance_m": data.get("distanceInMeters") or data.get("distanceMeter"), "elevation_gain_m": data.get("elevationGainInMeters") or data.get("elevationGainMeter"),
                "elevation_loss_m": data.get("elevationLossInMeters") or data.get("elevationLossMeter"), "activity": (data.get("activityType") or {}).get("typeKey"), "waypoints_count": len(points), "waypoints": points,
                "geo_points_count": len(data.get("geoPoints") or []), "url": f"https://connect.garmin.com/modern/course/{course_id}"}

    @reg.tool(annotations=READ)
    def download_course_gpx(course_id: int, output_path: str | None = None) -> Any:
        """Download a Garmin Connect course as a GPX file rebuilt from its track points and course points (not the original upload).

        Args:
            course_id: ID of the course to download.
            output_path: Optional destination directory (saved as {course_id}.gpx) or file path ending in .gpx. Defaults to the
                download_activity_file directory, else ./courses/{course_id}.gpx.
        """
        target = resolve_gpx_output_path(int(course_id), output_path)
        data = course_detail(course_id)
        geo = data.get("geoPoints") or []
        if not geo:
            raise ToolFailure(f"Course {course_id} has no geoPoints.")
        name = data.get("courseName") or f"Garmin Course {course_id}"
        cps = data.get("coursePoints") or []
        lines = ["<?xml version='1.0' encoding='UTF-8'?>", "<gpx version='1.1' creator='GarminConnectMCP' xmlns='http://www.topografix.com/GPX/1/1'>", "  <metadata>", f"    <name>{escape(name)}</name>", "  </metadata>"]
        for cp in cps:
            if cp.get("lat") is not None and cp.get("lon") is not None:
                lines += [f"  <wpt lat='{_gpx_decimal(cp['lat'])}' lon='{_gpx_decimal(cp['lon'])}'>", f"    <name>{escape(cp.get('name') or cp.get('pointType') or 'Waypoint')}</name>", f"    <type>{escape(cp.get('pointType') or 'GENERIC')}</type>", "  </wpt>"]
        lines += ["  <trk>", f"    <name>{escape(name)}</name>", "    <trkseg>"]
        for p in geo:
            if p.get("latitude") is not None and p.get("longitude") is not None:
                ele = f"<ele>{_gpx_decimal(p['elevation'])}</ele>" if p.get("elevation") is not None else ""
                lines.append(f"      <trkpt lat='{_gpx_decimal(p['latitude'])}' lon='{_gpx_decimal(p['longitude'])}'>{ele}</trkpt>")
        lines += ["    </trkseg>", "  </trk>", "</gpx>"]
        os.makedirs(os.path.dirname(target), exist_ok=True)
        Path(target).write_text("\n".join(lines), encoding="utf-8")
        return {"status": "success", "course_id": course_id, "name": name, "gpx_path": target, "waypoints_count": len(cps), "track_points_count": len(geo)}

    @reg.tool(annotations=WRITE)
    def upload_course(gpx_path: str, course_name: str | None = None, activity_type: str = "running", description: str | None = None) -> Any:
        """Upload a GPX file as a Garmin Connect Course (loadable onto the watch for navigation or a PacePro strategy).

        Args:
            gpx_path: Absolute path to the .gpx file on disk.
            course_name: Override the course name. Defaults to the name parsed from the GPX file. Max 128 characters.
            activity_type: One of running, cycling, hiking, walking, trail_running, mountain_biking, road_biking, gravel_cycling.
            description: Optional description shown on the course detail page. Max 512 characters.
        """
        p = Path(gpx_path).expanduser()
        if p.suffix.lower() != ".gpx":
            raise ToolFailure(f"only .gpx files are allowed, got: {p.suffix or '(no extension)'}")
        if not p.is_file():
            raise ToolFailure(f"GPX file not found: {p}")
        type_id = COURSE_ACTIVITY_TYPE_IDS.get(activity_type.lower().strip())
        if type_id is None:
            raise ToolFailure(f"Unsupported activity_type '{activity_type}'. Supported: {', '.join(sorted(COURSE_ACTIVITY_TYPE_IDS))}.")
        parsed = m.write("POST", "/course-service/course/import", files={"file": (p.name, p.read_bytes(), "application/gpx+xml")})
        if not isinstance(parsed, dict):
            raise ToolFailure("Garmin did not return a parsed course for the GPX upload.")
        name = course_name or parsed.get("courseName") or p.stem
        payload = _build_course_payload(parsed, course_name=name, activity_type_id=type_id, description=description)
        saved = m.write("POST", "/course-service/course", json_body=payload, refetch=lambda: m.refresh_snapshot("courses"))
        saved = saved if isinstance(saved, dict) else {}
        return WriteResult({"status": "success", "course_id": saved.get("courseId"), "name": saved.get("courseName"), "distance_m": saved.get("distanceMeter"), "elevation_gain_m": saved.get("elevationGainMeter"), "elevation_loss_m": saved.get("elevationLossMeter"),
                            "activity_type_id": saved.get("activityTypePk"), "url": f"https://connect.garmin.com/modern/course/{saved.get('courseId')}"})

    @reg.tool(annotations=DESTRUCTIVE)
    def delete_course(course_id: int, confirm: bool = False) -> Any:
        """Delete a course from Garmin Connect. This cannot be undone. Call once with confirm=false (the default) to preview, then with confirm=true.

        Args:
            course_id: ID of the course to delete (get IDs from get_courses).
            confirm: Must be true to actually delete. Default false returns a preview.
        """
        preview: dict[str, Any] = {"course_id": course_id}
        try:
            c = course_detail(course_id)
            preview.update(drop_none({"name": c.get("courseName"), "activity": (c.get("activityType") or {}).get("typeKey"), "distance_m": c.get("distanceInMeters") or c.get("distanceMeter"), "created": c.get("createdDateFormatted")}))
        except ToolFailure:
            pass
        if not confirm:
            return {"status": "needs_confirmation", "course": preview, "message": "This permanently deletes the course from Garmin Connect and cannot be undone. Call again with confirm=true to proceed."}
        m.write("DELETE", f"/course-service/course/{int(course_id)}", refetch=lambda: m.refresh_snapshot("courses"))
        return WriteResult({"status": "success", "course_id": course_id, "course": preview, "message": f"Course {course_id} deleted"})


__all__ = ["register", "shutil", "datetime"]

"""Activity tools (upstream activity_management): the list and every per-activity
dataset come from the mirror; edits go to Garmin and the activity is re-fetched."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..mirror import GarminNotFound, Provenance, note_provenance
from .base import DESTRUCTIVE, IDEMPOTENT_WRITE, READ, WRITE, Registry, ToolFailure, WriteResult, activity_key, drop_none, parse_date, parse_range

ACTIVITY_URL = "/activity-service/activity"


def _build_exercise_sets(sets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Friendly set dicts -> Garmin's exerciseSets wire format (from upstream, MIT)."""
    out: list[dict[str, Any]] = []
    for i, s in enumerate(sets, start=1):
        category = str(s.get("category") or "").strip().upper()
        if not category:
            raise ValueError(f"set {i}: category is required")
        name = str(s.get("exercise_name") or "").strip().upper() or None
        if s.get("weight_kg") is not None and s.get("weight_lb") is not None:
            raise ValueError(f"set {i}: give weight_kg or weight_lb, not both")
        if s.get("weight_kg") is not None:
            kg: float | None = float(s["weight_kg"])
        elif s.get("weight_lb") is not None:
            kg = float(s["weight_lb"]) * 0.45359237
        else:
            kg = None
        if kg is not None and kg < 0:
            raise ValueError(f"set {i}: weight cannot be negative")
        reps = s.get("reps")
        if reps is not None and (int(reps) != reps or reps < 0):
            raise ValueError(f"set {i}: reps must be a non-negative whole number")
        out.append({
            "setType": "ACTIVE",
            "exercises": [{"category": category, "name": None if name == category else name, "probability": 100}],
            "repetitionCount": None if reps is None else int(reps),
            "weight": -1 if kg is None else round(kg * 1000),
            "duration": s.get("duration_seconds"),
        })
        if s.get("rest_seconds"):
            out.append({"setType": "REST", "exercises": [], "repetitionCount": None, "weight": -1, "duration": s["rest_seconds"]})
    return out


def _list_item(a: dict[str, Any]) -> dict[str, Any]:
    return drop_none({
        "id": a.get("activityId"),
        "name": a.get("activityName"),
        "type": (a.get("activityType") or {}).get("typeKey"),
        "event_type": (a.get("eventType") or {}).get("typeKey"),
        "start_time": a.get("startTimeLocal"),
        "distance_meters": a.get("distance"),
        "duration_seconds": a.get("duration"),
        "calories": a.get("calories"),
        "avg_hr_bpm": a.get("averageHR"),
        "max_hr_bpm": a.get("maxHR"),
        "steps": a.get("steps"),
        "elevation_gain_meters": a.get("elevationGain"),
        "elevation_loss_meters": a.get("elevationLoss"),
    })


def register(reg: Registry) -> None:
    m = reg.mirror

    def listing_note() -> None:
        if m.engine.get_state("last:list_full") is None:
            note_provenance(Provenance("mirror", None, notes=["the first complete activity listing has not finished yet; older activities may be missing"]))

    def summaries(where: str, params: list[Any], limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
        sql = f"""SELECT r.body FROM activity a JOIN raw_current r ON r.object_type = 'activity_summary' AND r.object_key = a.activity_id::VARCHAR
                  WHERE a.deleted_at IS NULL AND r.body IS NOT NULL AND {where} ORDER BY a.start_time_gmt DESC"""
        if limit is not None:
            sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
        import json

        return [json.loads(r["body"]) for r in m.db.all(sql, params)]

    @reg.tool(annotations=READ)
    def get_activities_by_date(start_date: str, end_date: str, activity_type: str = "", page: int = 0, page_size: int = 100) -> Any:
        """Get activities between specified dates with pagination support.

        Activities are ordered newest-first. When has_more is true the response includes next_page; pass it as page on the next call.
        Each activity includes an event_type field ("race", "training", "uncategorized" when no event type was set; filter for races
        with event_type == "race" rather than excluding "training").

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
            activity_type: Optional activity type filter (e.g., cycling, running, swimming)
            page: Zero-based page number (default 0)
            page_size: Number of activities per page, max 200 (default 100)
        """
        s, e = parse_range(start_date, end_date)
        page_size = max(1, min(int(page_size), 200))
        page = max(0, int(page))
        m.fresh_activity_list()
        listing_note()
        where = "a.start_time_local::DATE BETWEEN ?::DATE AND ?::DATE"
        params: list[Any] = [s, e]
        if activity_type:
            where += " AND a.activity_type = ?"
            params += [activity_type]
        rows = summaries(where, params, page_size, page * page_size)
        if not rows:
            return {"count": 0, "page": page, "page_size": page_size, "has_more": False, "date_range": {"start": start_date, "end": end_date}, "activities": [], "message": f"No activities found between {start_date} and {end_date}"}
        has_more = len(rows) == page_size
        out: dict[str, Any] = {"count": len(rows), "page": page, "page_size": page_size, "has_more": has_more, "date_range": {"start": start_date, "end": end_date}, "activities": [_list_item(a) for a in rows]}
        if has_more:
            out["next_page"] = page + 1
        return out

    @reg.tool(annotations=READ)
    def get_activities_fordate(date: str) -> Any:
        """Get activities for a specific date

        Args:
            date: Date in YYYY-MM-DD format
        """
        d = parse_date(date)
        m.fresh_activity_list()
        rows = summaries("a.start_time_local::DATE = ?::DATE", [d])
        if not rows:
            return f"No activities found for {date}"
        acts = []
        for a in rows:
            acts.append(drop_none({
                "id": a.get("activityId"), "name": a.get("activityName"), "type": (a.get("activityType") or {}).get("typeKey"),
                "event_type": (a.get("eventType") or {}).get("typeKey"), "start_time": a.get("startTimeLocal"), "distance_meters": a.get("distance"),
                "duration_seconds": a.get("duration"), "calories": a.get("calories"), "avg_hr_bpm": a.get("averageHR"), "steps": a.get("steps"),
                "lap_count": a.get("lapCount"), "moderate_intensity_minutes": a.get("moderateIntensityMinutes"), "vigorous_intensity_minutes": a.get("vigorousIntensityMinutes"),
            }))
        return {"date": date, "count": len(acts), "activities": acts}

    @reg.tool(annotations=READ)
    def get_activity(activity_id: int | str) -> Any:
        """Get detailed information for a single activity.

        Returns a comprehensive summary including timing, distance, heart rate, elevation, training effect, and an event_type field
        ("race", "training", "uncategorized" when no event type was set; omitted when Garmin returns no eventTypeDTO).

        Args:
            activity_id: ID of the activity to retrieve
        """
        key = activity_key(activity_id)
        activity = m.activity("activity", key).data
        summary = activity.get("summaryDTO") or {}
        activity_type = activity.get("activityTypeDTO") or {}
        metadata = activity.get("metadataDTO") or {}
        return drop_none({
            "id": activity.get("activityId"), "name": activity.get("activityName"), "description": activity.get("description"),
            "type": activity_type.get("typeKey"), "event_type": (activity.get("eventTypeDTO") or {}).get("typeKey"), "parent_type": activity_type.get("parentTypeId"),
            "start_time_local": summary.get("startTimeLocal"), "start_time_gmt": summary.get("startTimeGMT"),
            "duration_seconds": summary.get("duration"), "moving_duration_seconds": summary.get("movingDuration"), "elapsed_duration_seconds": summary.get("elapsedDuration"),
            "distance_meters": summary.get("distance"), "avg_speed_mps": summary.get("averageSpeed"), "max_speed_mps": summary.get("maxSpeed"),
            "avg_hr_bpm": summary.get("averageHR"), "max_hr_bpm": summary.get("maxHR"), "min_hr_bpm": summary.get("minHR"),
            "calories": summary.get("calories"), "bmr_calories": summary.get("bmrCalories"),
            "avg_cadence": summary.get("averageRunCadence"), "max_cadence": summary.get("maxRunCadence"),
            "avg_stride_length_cm": summary.get("strideLength"), "avg_ground_contact_time_ms": summary.get("groundContactTime"), "avg_vertical_oscillation_cm": summary.get("verticalOscillation"),
            "steps": summary.get("steps"), "avg_power_watts": summary.get("averagePower"), "max_power_watts": summary.get("maxPower"), "normalized_power_watts": summary.get("normalizedPower"),
            "training_effect": summary.get("trainingEffect"), "anaerobic_training_effect": summary.get("anaerobicTrainingEffect"), "training_effect_label": summary.get("trainingEffectLabel"),
            "training_load": summary.get("activityTrainingLoad"), "moderate_intensity_minutes": summary.get("moderateIntensityMinutes"), "vigorous_intensity_minutes": summary.get("vigorousIntensityMinutes"),
            "elevation_gain_meters": summary.get("elevationGain"), "elevation_loss_meters": summary.get("elevationLoss"), "max_elevation_meters": summary.get("maxElevation"), "min_elevation_meters": summary.get("minElevation"),
            "recovery_hr_bpm": summary.get("recoveryHeartRate"), "body_battery_impact": summary.get("differenceBodyBattery"),
            "workout_feel": summary.get("directWorkoutFeel"), "workout_rpe": summary.get("directWorkoutRpe"),
            "lap_count": metadata.get("lapCount"), "has_splits": metadata.get("hasSplits"), "device_manufacturer": metadata.get("manufacturer"),
        })

    def put_activity(key: str, payload: dict[str, Any]) -> Any:
        return m.write("PUT", f"{ACTIVITY_URL}/{key}", json_body={"activityId": int(key), **payload}, refetch=lambda: m.refresh_activity(key))

    @reg.tool(annotations=IDEMPOTENT_WRITE)
    def set_activity_name(activity_id: int | str, activity_name: str) -> Any:
        """Set or update the name of an activity.

        Args:
            activity_id: ID of the activity to update
            activity_name: New activity name. Max 200 characters; Garmin rejects longer.
        """
        key = activity_key(activity_id)
        activity_name = activity_name.strip()
        if not activity_name:
            raise ToolFailure("Activity name cannot be empty")
        put_activity(key, {"activityName": activity_name})
        return WriteResult({"success": True, "activity_id": int(key), "activity_name": activity_name, "message": "Activity name successfully updated"})

    @reg.tool(annotations=IDEMPOTENT_WRITE)
    def set_activity_type(activity_id: int | str, type_key: str) -> Any:
        """Change the activity type (sport) of an activity.

        Useful for reclassifying a mislabelled activity, e.g. flipping a run logged as 'trail_running' to 'running'.
        Call get_activity_types to see all valid type keys.

        Args:
            activity_id: ID of the activity to update
            type_key: Target activity type key (e.g. 'running', 'trail_running', 'treadmill_running', 'cycling', 'lap_swimming')
        """
        key = activity_key(activity_id)
        type_key = type_key.strip()
        types = m.optional("activity_types", default=[]) or []
        match = next((t for t in types if t.get("typeKey") == type_key), None)
        if not match:
            valid = ", ".join(sorted(t.get("typeKey") for t in types if t.get("typeKey")))
            raise ToolFailure(f"Unknown activity type '{type_key}'. Valid type keys: {valid}")
        put_activity(key, {"activityTypeDTO": {"typeId": match["typeId"], "typeKey": match["typeKey"], "parentTypeId": match.get("parentTypeId")}})
        return WriteResult({"success": True, "activity_id": int(key), "type_key": match["typeKey"], "type_id": match["typeId"], "message": "Activity type successfully updated"})

    @reg.tool(annotations=IDEMPOTENT_WRITE)
    def set_activity_description(activity_id: int | str, description: str) -> Any:
        """Set or update the free-text description (notes) of an activity. Pass an empty string to clear it.

        Args:
            activity_id: ID of the activity to update
            description: New description text (empty string clears it). Max 2000 characters; Garmin rejects longer.
        """
        key = activity_key(activity_id)
        put_activity(key, {"description": description})
        return WriteResult({"success": True, "activity_id": int(key), "description": description, "message": "Activity description successfully updated"})

    @reg.tool(annotations=IDEMPOTENT_WRITE)
    def set_activity_event_type(activity_id: int | str, event_type: str) -> Any:
        """Set the event type of an activity.

        Event type categorises the activity's purpose. Valid keys: race, recreation, specialEvent, training, transportation, touring,
        geocaching, fitness, uncategorized.

        Args:
            activity_id: ID of the activity to update
            event_type: Target event type key (e.g. 'race', 'training')
        """
        key = activity_key(activity_id)
        event_type = event_type.strip()
        types = m.optional("activity_event_types", default=[]) or []
        match = next((t for t in types if t.get("typeKey") == event_type), None)
        if not match:
            valid = ", ".join(sorted(t.get("typeKey") for t in types if t.get("typeKey")))
            raise ToolFailure(f"Unknown event type '{event_type}'. Valid event types: {valid}")
        put_activity(key, {"eventTypeDTO": {"typeId": match["typeId"], "typeKey": match["typeKey"]}})
        return WriteResult({"success": True, "activity_id": int(key), "event_type": match["typeKey"], "event_type_id": match["typeId"], "message": "Activity event type successfully updated"})

    @reg.tool(annotations=IDEMPOTENT_WRITE)
    def set_perceived_effort(activity_id: int | str, rpe: float) -> Any:
        """Set the perceived effort (RPE) for an activity, on Garmin Connect's 0-10 scale (0 clears the rating; Garmin stores it times 10).

        Args:
            activity_id: ID of the activity to update
            rpe: Perceived effort from 0 to 10 (0 clears the rating)
        """
        key = activity_key(activity_id)
        if not 0 <= rpe <= 10:
            raise ToolFailure("rpe must be between 0 and 10")
        stored = None if rpe == 0 else round(rpe * 10)
        put_activity(key, {"summaryDTO": {"directWorkoutRpe": stored}})
        return WriteResult({"success": True, "activity_id": int(key), "rpe": rpe, "message": "Perceived effort successfully updated" if stored else "Perceived effort cleared"})

    @reg.tool(annotations=IDEMPOTENT_WRITE)
    def set_activity_feel(activity_id: int | str, feel: int) -> Any:
        """Set how an activity felt ('How did you feel?'): one of 0 (very tired), 25, 50 (normal), 75, 100 (strong).

        Args:
            activity_id: ID of the activity to update
            feel: One of 0, 25, 50, 75, 100
        """
        key = activity_key(activity_id)
        if feel not in (0, 25, 50, 75, 100):
            raise ToolFailure("feel must be one of 0, 25, 50, 75, 100")
        put_activity(key, {"summaryDTO": {"directWorkoutFeel": feel}})
        return WriteResult({"success": True, "activity_id": int(key), "feel": feel, "message": "Activity feel successfully updated"})

    @reg.tool(annotations=READ)
    def get_activity_splits(activity_id: int | str) -> Any:
        """Get splits for an activity. Lap and nested length start_time_gmt timestamps are GMT (UTC).

        Args:
            activity_id: ID of the activity to retrieve splits for
        """
        key = activity_key(activity_id)
        splits = m.activity("activity_splits", key).data
        laps = splits.get("lapDTOs") or []
        out: dict[str, Any] = {"activity_id": splits.get("activityId"), "lap_count": len(laps), "laps": []}
        for lap in laps:
            lap_data = {
                "lap_number": lap.get("lapIndex"), "start_time_gmt": lap.get("startTimeGMT"), "distance_meters": lap.get("distance"),
                "duration_seconds": lap.get("duration"), "moving_duration_seconds": lap.get("movingDuration"), "elapsed_duration_seconds": lap.get("elapsedDuration"),
                "avg_speed_mps": lap.get("averageSpeed"), "avg_moving_speed_mps": lap.get("averageMovingSpeed"), "max_speed_mps": lap.get("maxSpeed"),
                "avg_hr_bpm": lap.get("averageHR"), "max_hr_bpm": lap.get("maxHR"), "calories": lap.get("calories"), "bmr_calories": lap.get("bmrCalories"),
                "avg_cadence": lap.get("averageRunCadence"), "avg_power_watts": lap.get("averagePower"), "avg_swim_cadence": lap.get("averageSwimCadence"),
                "active_length_count": lap.get("numberOfActiveLengths"), "total_strokes": lap.get("totalNumberOfStrokes"), "avg_strokes": lap.get("averageStrokes"),
                "avg_swolf": lap.get("averageSWOLF"), "avg_stroke_distance": lap.get("averageStrokeDistance"), "intensity_type": lap.get("intensityType"),
                "elevation_gain_meters": lap.get("elevationGain"), "elevation_loss_meters": lap.get("elevationLoss"), "workout_step_index": lap.get("wktStepIndex"),
            }
            dist = lap.get("distance")
            if dist is not None and dist < 0:
                lap_data["distance_meters"] = None
                lap_data["distance_invalid"] = True
            if lap.get("lengthDTOs"):
                lap_data["lengths"] = [
                    drop_none({
                        "length_number": ln.get("lengthIndex"), "start_time_gmt": ln.get("startTimeGMT"), "distance_meters": ln.get("distance"), "duration_seconds": ln.get("duration"),
                        "avg_speed_mps": ln.get("averageSpeed"), "max_speed_mps": ln.get("maxSpeed"), "calories": ln.get("calories"), "avg_hr_bpm": ln.get("averageHR"), "max_hr_bpm": ln.get("maxHR"),
                        "total_strokes": ln.get("totalNumberOfStrokes"), "avg_swolf": ln.get("averageSWOLF"), "stroke": ln.get("swimStroke"),
                    })
                    for ln in lap["lengthDTOs"]
                ]
            out["laps"].append(drop_none(lap_data))
        return out

    @reg.tool(annotations=READ)
    def get_activity_typed_splits(activity_id: int | str) -> Any:
        """Get typed splits for an activity

        Args:
            activity_id: ID of the activity to retrieve typed splits for
        """
        return m.activity("activity_typed_splits", activity_key(activity_id)).data

    @reg.tool(annotations=READ)
    def get_activity_split_summaries(activity_id: int | str) -> Any:
        """Get split summaries for an activity

        Args:
            activity_id: ID of the activity to retrieve split summaries for
        """
        return m.activity("activity_split_summaries", activity_key(activity_id)).data

    @reg.tool(annotations=READ)
    def get_activity_weather(activity_id: int | str) -> Any:
        """Get weather data for an activity.

        Garmin's weather endpoint returns temperatures in Fahrenheit with no unit indicator; this tool converts them to the account's
        display unit (metric accounts get Celsius, statute_us accounts keep Fahrenheit; temperature_unit says which). Wind speed is
        already in the account's display unit (km/h or mph) and is labeled via wind_speed_unit.

        Args:
            activity_id: ID of the activity to retrieve weather data for
        """
        key = activity_key(activity_id)
        weather = m.activity("activity_weather", key).data
        unit_system = _unit_system(m)

        def f_to_c(v: Any) -> Any:
            return round((v - 32) * 5 / 9, 1) if v is not None else None

        if unit_system is not None and unit_system != "statute_us":
            temp_unit = "C"
            temp, apparent, dew = f_to_c(weather.get("temp")), f_to_c(weather.get("apparentTemp")), f_to_c(weather.get("dewPoint"))
        else:
            temp_unit = "F" if unit_system == "statute_us" else None
            temp, apparent, dew = weather.get("temp"), weather.get("apparentTemp"), weather.get("dewPoint")
        wind_unit = "mph" if unit_system == "statute_us" else ("km/h" if unit_system is not None else None)
        wt = weather.get("weatherTypeDTO") or {}
        st = weather.get("weatherStationDTO") or {}
        return drop_none({
            "activity_id": int(key), "temperature": temp, "temperature_unit": temp_unit, "apparent_temperature": apparent, "dew_point": dew,
            "humidity_percent": weather.get("relativeHumidity"), "wind_speed": weather.get("windSpeed"), "wind_speed_unit": wind_unit,
            "wind_direction_degrees": weather.get("windDirection"), "wind_direction_compass": weather.get("windDirectionCompassPoint"), "wind_gust": weather.get("windGust"),
            "weather_type": wt.get("weatherTypeName"), "weather_description": wt.get("desc") or wt.get("weatherTypeDesc"), "location": weather.get("issueLocation"),
            "station_id": st.get("id"), "station_name": st.get("name"), "issue_time": weather.get("issueDate"),
        })

    @reg.tool(annotations=READ)
    def get_activity_hr_in_timezones(activity_id: int | str) -> Any:
        """Get heart rate data in different time zones for an activity

        Args:
            activity_id: ID of the activity to retrieve heart rate time zone data for
        """
        return m.activity("activity_hr_zones", activity_key(activity_id)).data

    @reg.tool(annotations=READ)
    def get_activity_power_in_timezones(activity_id: int | str) -> Any:
        """Get power distribution across training zones for an activity (time in each power zone with watt thresholds). Requires a power meter.

        Args:
            activity_id: ID of the activity to retrieve power zone data for
        """
        return m.activity("activity_power_zones", activity_key(activity_id)).data

    @reg.tool(annotations=READ)
    def get_activity_gear(activity_id: int | str) -> Any:
        """Get gear data used for an activity

        Args:
            activity_id: ID of the activity to retrieve gear data for
        """
        return m.activity("activity_gear", activity_key(activity_id)).data

    @reg.tool(annotations=READ)
    def get_activity_exercise_sets(activity_id: int | str) -> Any:
        """Get exercise sets for strength training activities

        Args:
            activity_id: ID of the activity to retrieve exercise sets for
        """
        return m.activity("activity_exercise_sets", activity_key(activity_id)).data

    @reg.tool(annotations=IDEMPOTENT_WRITE)
    def set_activity_exercise_sets(activity_id: int | str, sets: list[dict[str, Any]], keep_timing: bool = False) -> Any:
        """Replace the exercise sets on a strength (or other rep-counting) activity.

        Writes Garmin's structured per-set data (the grid the Garmin Connect "Edit sets" screen saves). This OVERWRITES every existing
        set, including watch-detected sets and their timing; to keep some, read them with get_activity_exercise_sets and include them.
        With keep_timing=True the existing sets keep their start times, durations and rest periods and only exercise, reps and weight
        are replaced, in order (the number of sets must match the recorded active sets).
        Exercises use Garmin's own keys: category (e.g. DEADLIFT, BENCH_PRESS) and exercise_name within it (e.g. BARBELL_DEADLIFT);
        see https://connect.garmin.com/web-data/exercises/Exercises.json. Garmin rejects unknown keys and leaves the activity unchanged.

        Args:
            activity_id: ID of the activity to update
            sets: Active sets in order. Each: category (required), exercise_name, reps, weight_kg or weight_lb, duration_seconds, rest_seconds
            keep_timing: Keep the recorded set timing and rest periods, replacing only exercise, reps and weight (default False)
        """
        key = activity_key(activity_id)
        if not sets:
            raise ToolFailure("sets must contain at least one set")
        new_sets = _build_exercise_sets(sets)
        if keep_timing:
            current = m.activity("activity_exercise_sets", key).data or {}
            existing = current.get("exerciseSets") or []
            active = [s for s in existing if s.get("setType") == "ACTIVE"]
            new_active = [s for s in new_sets if s["setType"] == "ACTIVE"]
            if len(active) != len(new_active):
                raise ToolFailure(f"keep_timing needs exactly {len(active)} active sets (the activity's recorded sets), got {len(new_active)}")
            it = iter(new_active)
            merged = []
            for s in existing:
                if s.get("setType") == "ACTIVE":
                    n = next(it)
                    merged.append({**s, "exercises": n["exercises"], "repetitionCount": n["repetitionCount"], "weight": n["weight"]})
                else:
                    merged.append(s)
            payload = {"activityId": int(key), "exerciseSets": merged}
        else:
            payload = {"activityId": int(key), "exerciseSets": new_sets}
        m.write("PUT", f"{ACTIVITY_URL}/{key}/exerciseSets", json_body=payload, refetch=lambda: m.fetch.activity_sub(_ds("activity_exercise_sets"), key, priority="interactive"))
        return WriteResult({"success": True, "activity_id": int(key), "set_count": len(payload["exerciseSets"]), "message": "Exercise sets successfully updated"})

    @reg.tool(annotations=READ)
    def count_activities() -> Any:
        """Get total count of activities in the user's Garmin account"""
        data = m.snapshot("activity_count").data
        if isinstance(data, dict):
            return {"total_activities": data.get("totalCount", data.get("countOfActivities")), **{k: v for k, v in data.items() if k != "totalCount"}}
        return {"total_activities": data}

    @reg.tool(annotations=READ)
    def get_activities(start: int = 0, limit: int = 20) -> Any:
        """Get activities with pagination support, ordered newest-first.

        Each activity includes an event_type field ("race", "training", "uncategorized" when no event type was set).

        Args:
            start: Starting index (default 0)
            limit: Maximum number of activities to return (default 20, max 100)
        """
        limit = max(1, min(int(limit), 100))
        m.fresh_activity_list()
        listing_note()
        rows = summaries("true", [], limit, max(0, int(start)))
        if not rows:
            return f"No activities found starting at index {start}"
        return {"start": start, "limit": limit, "count": len(rows), "activities": [_list_item(a) for a in rows]}

    @reg.tool(annotations=WRITE)
    def create_manual_activity(type_key: str, date: str, duration_minutes: int, start_time: str = "09:00", activity_name: str = "", distance_km: float = 0.0, time_zone: str = "UTC") -> Any:
        """Log a manual activity in Garmin Connect, for activities done without a watch.

        The type_key must match a Garmin activity type (use get_activity_types); common values: yoga, strength_training, meditation,
        indoor_cycling, pilates, bouldering, fitness_equipment. If a write fails with write_committed="unknown", check whether the activity
        exists before retrying to avoid creating a duplicate.

        Args:
            type_key: Activity type key (e.g. "yoga", "strength_training")
            date: Date of the activity in YYYY-MM-DD format
            duration_minutes: Duration of the activity in minutes
            start_time: Start time as HH:MM (24-hour, default 09:00)
            activity_name: Optional title; defaults to the type_key if not provided
            distance_km: Distance in kilometres (default 0.0 for non-distance activities)
            time_zone: IANA time zone for the activity (default UTC)
        """
        type_key = type_key.strip()
        if not type_key:
            raise ToolFailure("type_key is required")
        if duration_minutes <= 0:
            raise ToolFailure("duration_minutes must be greater than 0")
        if distance_km < 0:
            raise ToolFailure("distance_km must be non-negative")
        parse_date(date)
        try:
            if datetime.strptime(start_time, "%H:%M").strftime("%H:%M") != start_time:
                raise ValueError
        except ValueError as err:
            raise ToolFailure("start_time must be in HH:MM (24-hour) format") from err
        name = activity_name.strip() or type_key.replace("_", " ").title()
        payload = {
            "activityTypeDTO": {"typeKey": type_key},
            "accessControlRuleDTO": {"typeId": 2, "typeKey": "private"},
            "timeZoneUnitDTO": {"unitKey": time_zone},
            "activityName": name,
            "metadataDTO": {"autoCalcCalories": True},
            "summaryDTO": {"startTimeLocal": f"{date}T{start_time}:00.000", "distance": distance_km * 1000, "duration": duration_minutes * 60},
        }
        result = m.write("POST", ACTIVITY_URL, json_body=payload, refetch=lambda: m.engine.run_job("list_recent", "self", 5))
        return WriteResult({"success": True, "activity": result})

    @reg.tool(annotations=DESTRUCTIVE)
    def delete_activity(activity_id: int | str, confirm: bool = False) -> Any:
        """Permanently delete an activity from Garmin Connect. This cannot be undone.

        Call once with confirm=false (the default) to preview the activity, then again with confirm=true to delete it.

        Args:
            activity_id: ID of the activity to delete
            confirm: Must be true to actually delete. Default false returns a preview.
        """
        key = activity_key(activity_id)
        try:
            a = m.activity("activity", key).data
        except GarminNotFound as err:
            raise ToolFailure(str(err)) from err
        s = a.get("summaryDTO") or {}
        preview = drop_none({"id": a.get("activityId"), "name": a.get("activityName"), "type": (a.get("activityTypeDTO") or {}).get("typeKey"), "start_time_local": s.get("startTimeLocal"), "duration_seconds": s.get("duration"), "distance_meters": s.get("distance")})
        if not confirm:
            return {"status": "needs_confirmation", "activity": preview, "message": "This permanently deletes the activity from Garmin Connect and cannot be undone. Call again with confirm=true to proceed."}

        def refetch() -> None:
            m.engine.run_job("list_full", "self", 5)

        m.write("DELETE", f"{ACTIVITY_URL}/{key}", refetch=refetch)
        return WriteResult({"status": "deleted", "activity": preview, "deleted_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")})

    @reg.tool(annotations=READ)
    def get_activity_types() -> Any:
        """Get all available activity types supported by Garmin Connect, useful for filtering activities by type."""
        return m.optional("activity_types", default=[])


def _ds(name: str):  # noqa: ANN202
    from ..garmin.datasets import dataset

    return dataset(name)


def _unit_system(m) -> str | None:  # noqa: ANN001
    try:
        return m.snapshot("userprofile_settings").data.get("measurementSystem") or m.snapshot("user_settings").data.get("userData", {}).get("measurementSystem")
    except Exception:  # noqa: BLE001
        return None

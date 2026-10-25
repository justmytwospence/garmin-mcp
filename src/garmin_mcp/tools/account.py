"""Profile, device, gear, weight, challenge/goal/badge, data-entry and women's health
tools (upstream user_profile, devices, gear_management, weight_management, challenges,
data_management, womens_health)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from ..garmin.datasets import dataset
from ..mirror import GarminNotFound, Provenance, note_provenance
from .base import DESTRUCTIVE, IDEMPOTENT_WRITE, READ, WRITE, NoData, Registry, ToolFailure, WriteResult, activity_key, as_dict, drop_none, parse_date, parse_range
from .challenge_support import (
    ADHOC_ACTIVITY_TYPE_MAPPING,
    BADGE_CATEGORY_MAPPING,
    BADGE_DIFFICULTY_MAPPING,
    CHALLENGE_STATUS_MAPPING,
    PR_TYPE_MAPPING,
    _as_goal_list,
    _curate_badge_challenge,
    _curate_connect_ui_goal,
    _first_non_none,
    _format_badge_value,
    _format_pr_value,
    _format_time,
    _format_timestamp,
    _is_connect_ui_goal,
    _parse_iso_date,
)

HR_ZONES_URL = "/biometric-service/heartRateZones"
HR_ZONE_METHODS = {
    "HR_MAX": "HR_MAX", "MAX_HR": "HR_MAX", "MAXHR": "HR_MAX", "%MAX_HR": "HR_MAX",
    "HR_RESERVE": "HR_RESERVE", "HRR": "HR_RESERVE", "KARVONEN": "HR_RESERVE", "%HRR": "HR_RESERVE",
    "LACTATE_THRESHOLD": "LACTATE_THRESHOLD", "LTHR": "LACTATE_THRESHOLD", "%LTHR": "LACTATE_THRESHOLD",
    "CUSTOM": "CUSTOM_BPM", "CUSTOM_BPM": "CUSTOM_BPM", "BPM": "CUSTOM_BPM",
}


def _normalize_sport(sport: str) -> str:
    s = sport.strip().upper().replace("-", "_").replace(" ", "_")
    if s == "GENERIC":
        s = "DEFAULT"
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", s):
        raise ToolFailure("sport must be a non-empty Garmin sport key such as DEFAULT, RUNNING, or CYCLING")
    return s


def _validate_hr_zone_config(c: dict[str, Any]) -> None:
    for api, pub in (("maxHeartRateUsed", "max_hr"), ("restingHeartRateUsed", "resting_hr"), ("lactateThresholdHeartRateUsed", "lactate_threshold_hr")):
        v = c.get(api)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 0 < v <= 300):
            raise ToolFailure(f"{pub} must be an integer BPM value from 1 to 300")
    mx = c.get("maxHeartRateUsed")
    if not isinstance(mx, int) or isinstance(mx, bool) or not 0 < mx <= 300:
        raise ToolFailure("the current or supplied max_hr must be an integer BPM value from 1 to 300")
    if c.get("restingHeartRateUsed") is not None and c["restingHeartRateUsed"] >= mx:
        raise ToolFailure("resting_hr must be lower than max_hr")
    if c.get("lactateThresholdHeartRateUsed") is not None and c["lactateThresholdHeartRateUsed"] > mx:
        raise ToolFailure("lactate_threshold_hr must not exceed max_hr")
    floors = [c.get(f"zone{z}Floor") for z in range(1, 6)]
    if any(isinstance(v, bool) or not isinstance(v, int) or not 0 < v <= 300 for v in floors):
        raise ToolFailure("all five zone boundaries must be integer BPM values from 1 to 300")
    if any(lo >= hi for lo, hi in zip(floors, floors[1:], strict=False)):
        raise ToolFailure("zone boundaries must be strictly monotonically increasing")
    if floors[-1] > mx:
        raise ToolFailure("zone boundaries must not exceed max_hr")


def register(reg: Registry) -> None:
    m = reg.mirror

    # ----------------------------------------------------------------- profile

    @reg.tool(annotations=READ)
    def get_full_name() -> Any:
        """Get user's full name from profile"""
        p = m.snapshot("user_profile_base").data
        name = " ".join(x for x in (p.get("firstName"), p.get("lastName")) if x) or p.get("userName")
        return {"full_name": name}

    @reg.tool(annotations=READ)
    def get_unit_system() -> Any:
        """Get user's preferred unit system from profile"""
        s = m.snapshot("userprofile_settings").data
        return {"unit_system": s.get("measurementSystem")}

    @reg.tool(annotations=READ)
    def get_user_profile() -> Any:
        """Get user profile information"""
        return m.snapshot("user_settings").data

    @reg.tool(annotations=READ)
    def get_userprofile_settings() -> Any:
        """Get user profile settings"""
        return m.snapshot("userprofile_settings").data

    def hr_zone_configs() -> list[dict[str, Any]]:
        zones = m.optional("heart_rate_zones", default=[])
        if not isinstance(zones, list):
            raise ToolFailure("Garmin returned an unexpected heart-rate-zone response")
        return zones

    @reg.tool(annotations=READ)
    def get_heart_rate_zones(sport: str | None = None) -> Any:
        """Get the user's saved heart-rate training-zone configuration: the generic DEFAULT profile plus sport-specific overrides
        (RUNNING, CYCLING, ...). With no sport every saved profile is returned.

        Args:
            sport: Optional Garmin sport key (for example default, running, or cycling). "generic" is accepted as an alias for DEFAULT.
        """
        zones = hr_zone_configs()
        if sport is None:
            if not zones:
                return "No configured heart-rate zones found."
            return zones
        key = _normalize_sport(sport)
        z = next((z for z in zones if z.get("sport") == key), None)
        if z is None:
            return f"No configured heart-rate zones found for sport {key}."
        return z

    @reg.tool(annotations=IDEMPOTENT_WRITE)
    def set_heart_rate_zones(sport: str, max_hr: int | None = None, resting_hr: int | None = None, lactate_threshold_hr: int | None = None, calculation_method: str | None = None, zone_boundaries: list[int] | None = None) -> Any:
        """Set an account-level heart-rate training-zone configuration for one sport (DEFAULT, RUNNING, CYCLING, ... are independent).

        WARNING: this mutates the account configuration and affects future activities (not already-recorded ones). Omitted values are
        read from the sport's current configuration and preserved. calculation_method may be max_hr, hrr/karvonen, lthr, or custom_bpm;
        manual zone_boundaries are only accepted with custom_bpm (Garmin stores them with trainingMethod=HR_MAX, the explicit floors
        remain authoritative). The result is the configuration Garmin actually saved after a read-back.

        Args:
            sport: Garmin sport key, e.g. default/generic, running, or cycling.
            max_hr: Maximum heart rate in BPM.
            resting_hr: Resting heart rate in BPM. Supplying it disables Garmin's resting-HR auto-update for this zone profile.
            lactate_threshold_hr: Lactate-threshold heart rate (LTHR) in BPM.
            calculation_method: max_hr, hrr/karvonen, lthr, or custom_bpm.
            zone_boundaries: Five strictly increasing BPM floors [Z1, Z2, Z3, Z4, Z5]. Every floor must be at most max_hr.
        """
        if all(v is None for v in (max_hr, resting_hr, lactate_threshold_hr, calculation_method, zone_boundaries)):
            raise NoData("No fields to update: supply at least one of max_hr, resting_hr, lactate_threshold_hr, calculation_method, or zone_boundaries.")
        key = _normalize_sport(sport)
        zones = hr_zone_configs()
        current = next((z for z in zones if z.get("sport") == key), None)
        if current is None:
            default = next((z for z in zones if z.get("sport") == "DEFAULT"), None)
            if default is None:
                raise ToolFailure(f"Could not read current heart-rate zones for {key}, and no DEFAULT profile is available to inherit.")
            current = {**default, "sport": key}
        else:
            current = dict(current)
        if max_hr is not None:
            current["maxHeartRateUsed"] = max_hr
        if resting_hr is not None:
            current["restingHeartRateUsed"] = resting_hr
            current["restingHrAutoUpdateUsed"] = False
        if lactate_threshold_hr is not None:
            current["lactateThresholdHeartRateUsed"] = lactate_threshold_hr
        method = None
        if calculation_method is not None:
            method = HR_ZONE_METHODS.get(calculation_method.strip().upper().replace("-", "_").replace(" ", "_"))
            if method is None:
                raise ToolFailure("calculation_method must be one of max_hr, hrr/karvonen, lthr, or custom_bpm")
            current["trainingMethod"] = "HR_MAX" if method == "CUSTOM_BPM" else method
        if zone_boundaries is not None:
            if len(zone_boundaries) != 5:
                raise ToolFailure("zone_boundaries must contain exactly five BPM floors")
            if method != "CUSTOM_BPM":
                raise ToolFailure("manual zone_boundaries require calculation_method='custom_bpm'")
            for z, floor in enumerate(zone_boundaries, start=1):
                current[f"zone{z}Floor"] = floor
        if method == "CUSTOM_BPM" and zone_boundaries is None:
            raise ToolFailure("calculation_method='custom_bpm' requires all five zone_boundaries")
        current["changeState"] = "CHANGED"
        _validate_hr_zone_config(current)
        m.write("PUT", HR_ZONES_URL, json_body=[current], refetch=lambda: m.refresh_snapshot("heart_rate_zones"))
        confirmed = next((z for z in hr_zone_configs() if z.get("sport") == key), None)
        if confirmed is None:
            return WriteResult({"status": "success", "message": "Garmin accepted the update, but the saved profile could not be confirmed. Re-read the profile before another update.", "submitted_configuration": current})
        return WriteResult(confirmed)

    # ----------------------------------------------------------------- devices

    @reg.tool(annotations=READ)
    def get_devices() -> Any:
        """Get all Garmin devices associated with the user account"""
        devices = m.optional("devices", default=[]) or []
        out = []
        for d in devices:
            info = {"device_id": d.get("deviceId"), "device_name": d.get("displayName") or d.get("productDisplayName"), "model": d.get("partNumber"), "manufacturer": d.get("manufacturerName"), "serial_number": d.get("serialNumber"),
                    "software_version": d.get("softwareVersionString") or d.get("softwareVersion"), "status": d.get("deviceStatusName"), "last_sync_time": d.get("lastSyncTime"), "battery_status": d.get("batteryStatus")}
            if d.get("deviceType") or d.get("deviceTypeSimpleName"):
                info["device_type"] = d.get("deviceType") or d.get("deviceTypeSimpleName")
            if d.get("primaryDevice") is not None or d.get("primaryActivityTrackerIndicator") is not None:
                info["is_primary"] = d.get("primaryDevice") if d.get("primaryDevice") is not None else d.get("primaryActivityTrackerIndicator")
            out.append(drop_none(info))
        if not out:
            return "No devices found."
        return out

    @reg.tool(annotations=READ)
    def get_device_last_used() -> Any:
        """Get information about the last used Garmin device"""
        return m.snapshot("device_last_used").data

    @reg.tool(annotations=READ)
    def get_device_settings(device_id: int | str | None = None) -> Any:
        """Get settings for a specific Garmin device: time/date format, units, activity tracking settings, alarms.

        Args:
            device_id: Device ID (optional; defaults to the most recently used device; from get_devices or get_device_last_used)
        """
        if device_id is None:
            device_id = m.snapshot("device_last_used").data.get("userDeviceId")
        if device_id is None:
            raise NoData("No device id given and no last-used device known.")
        try:
            return m.snapshot("device_settings", str(device_id)).data
        except GarminNotFound as err:
            raise NoData(f"No settings found for device {device_id} ({err})") from err

    @reg.tool(annotations=READ)
    def get_primary_training_device() -> Any:
        """Get information about the primary training device and the other wearable devices on the account."""
        return m.snapshot("primary_training_device").data

    @reg.tool(annotations=READ)
    def get_device_solar_data(device_id: int | str, date: str) -> Any:
        """Get solar data for a specific device: activity time gained from solar, average/peak solar utilization and minutes with
        solar input across the day's per-minute readings. Only solar-capable devices.

        Args:
            device_id: Device ID (can be obtained from get_devices)
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        try:
            data = m.report("device_solar", f"/web-gateway/solar/{device_id}/{date}/{date}", {"singleDayView": "true"}, f"{device_id}_{date}").data
        except GarminNotFound as err:
            raise NoData(f"No solar data found for device {device_id} on {date} ({err})") from err
        inp = as_dict(data).get("deviceSolarInput") or data
        readings = (as_dict(inp).get("solarDailyDataDTOs") or [{}])
        day = readings[0] if readings else {}
        values = [v.get("solarUtilization") for v in (day.get("solarInputReadings") or []) if isinstance(v, dict) and isinstance(v.get("solarUtilization"), (int, float))]
        return drop_none({"device_id": device_id, "date": date, "solar_gain_activity_seconds": day.get("solarGainActivitySeconds") or day.get("solarGainActivityTime"),
                          "average_solar_utilization": round(sum(values) / len(values), 1) if values else None, "peak_solar_utilization": max(values) if values else None, "minutes_with_solar_input": sum(1 for v in values if v > 0) if values else None, "readings": len(values)})

    @reg.tool(annotations=READ)
    def get_device_alarms() -> Any:
        """Get alarms from all Garmin devices with their schedules, sounds and enabled status."""
        alarms = []
        for d in m.optional("devices", default=[]) or []:
            did = d.get("deviceId")
            if did is None:
                continue
            try:
                settings = m.snapshot("device_settings", str(did)).data
            except GarminNotFound:
                continue
            for a in as_dict(settings).get("alarms") or []:
                alarms.append({"device_id": did, "device_name": d.get("displayName") or d.get("productDisplayName"), **(a if isinstance(a, dict) else {"alarm": a})})
        if not alarms:
            return "No alarms found."
        return alarms

    @reg.tool(annotations=WRITE)
    def push_workout_to_device(workout_id: int, device_id: int | None = None) -> Any:
        """Send a workout from the library to a Garmin device (queues it like "Send to Device"; the watch picks it up at its next
        sync). Scheduled workouts reach the watch on their own. Each call queues another copy.

        Args:
            workout_id: ID of the workout to send (from get_workouts or upload_workout)
            device_id: Device ID (optional; defaults to the most recently used device)
        """
        if device_id is None:
            device_id = m.snapshot("device_last_used").data.get("userDeviceId")
        if device_id is None:
            raise NoData("No device id given and no last-used device known.")
        try:
            w = m.snapshot("workout", str(int(workout_id))).data
        except GarminNotFound as err:
            raise ToolFailure(f"Workout {workout_id} not found ({err})") from err
        m.call("push_workout_to_device", int(workout_id), int(device_id))
        return WriteResult({"status": "success", "workout_id": int(workout_id), "workout_name": as_dict(w).get("workoutName"), "device_id": int(device_id), "message": "Workout queued for the device; it appears after the next sync."})

    # ----------------------------------------------------------------- gear

    @reg.tool(annotations=READ)
    def get_gear(include_stats: bool = True) -> Any:
        """Get all gear registered with the user account: inventory with notes, usage statistics and default activity associations
        (defaults maps each activity type key to the names of its default gear).

        Args:
            include_stats: Include usage statistics for each gear item (default True).
        """
        gear = m.optional("gear", default=[]) or []
        type_names = {t["typeId"]: t["typeKey"] for t in (m.optional("activity_types", default=[]) or []) if isinstance(t, dict) and "typeId" in t}
        defaults_by_uuid: dict[str, list[str]] = {}
        try:
            for d in m.snapshot("gear_defaults").data or []:
                if isinstance(d, dict) and d.get("uuid"):
                    defaults_by_uuid.setdefault(d["uuid"], []).append(type_names.get(d.get("activityTypePk"), f"type_{d.get('activityTypePk')}"))
        except GarminNotFound:
            pass
        out = []
        for g in gear:
            uuid = g.get("uuid")
            item: dict[str, Any] = {"uuid": uuid, "name": g.get("displayName"), "full_name": g.get("customMakeModel"), "type": g.get("gearTypeName"), "status": (g.get("gearStatusName") or "").lower(),
                                    "date_begin": _parse_iso_date(g.get("dateBegin")) if g.get("dateBegin") else None, "date_end": _parse_iso_date(g.get("dateEnd")) if g.get("dateEnd") else None, "notes": g.get("notes")}
            if g.get("maximumMeters"):
                item["max_distance_km"] = round(g["maximumMeters"] / 1000, 1)
            if uuid in defaults_by_uuid:
                item["is_default_for"] = defaults_by_uuid[uuid]
            if include_stats and uuid:
                try:
                    st = m.snapshot("gear_stats", uuid).data
                    dist = as_dict(st).get("totalDistance")
                    item["stats"] = {"total_activities": as_dict(st).get("totalActivities"), "total_distance_km": round(dist / 1000, 1) if dist is not None else None}
                except Exception as err:  # noqa: BLE001
                    item["stats_error"] = str(err)
            out.append(item)
        out.sort(key=lambda x: (x["status"] != "active", x.get("date_begin") or ""))
        if not out:
            return "No gear found."
        defaults = {}
        for g in out:
            for t in g.get("is_default_for", []):
                defaults.setdefault(t, []).append(g["name"])
        return {"total_gear": len(out), "gear": out, "defaults": defaults}

    @reg.tool(annotations=WRITE)
    def add_gear_to_activity(activity_id: int, gear_uuid: str) -> Any:
        """Associate gear (shoes, bike, ...) with an activity.

        Args:
            activity_id: ID of the activity
            gear_uuid: UUID of the gear to add (get from get_gear)
        """
        key = activity_key(activity_id)
        m.write("PUT", f"/gear-service/gear/link/{gear_uuid}/activity/{key}", refetch=lambda: m.fetch.activity_sub(dataset("activity_gear"), key, priority="interactive"))
        return WriteResult({"status": "success", "activity_id": int(key), "gear_uuid": gear_uuid, "message": "Gear added to activity"})

    @reg.tool(annotations=WRITE)
    def remove_gear_from_activity(activity_id: int, gear_uuid: str) -> Any:
        """Remove a gear association from an activity.

        Args:
            activity_id: ID of the activity
            gear_uuid: UUID of the gear to remove
        """
        key = activity_key(activity_id)
        m.write("PUT", f"/gear-service/gear/unlink/{gear_uuid}/activity/{key}", refetch=lambda: m.fetch.activity_sub(dataset("activity_gear"), key, priority="interactive"))
        return WriteResult({"status": "success", "activity_id": int(key), "gear_uuid": gear_uuid, "message": "Gear removed from activity"})

    # ----------------------------------------------------------------- weight

    def measurement(w: dict[str, Any], with_date: bool) -> dict[str, Any]:
        out = {"date": w.get("calendarDate") if with_date else None, "weight_grams": w.get("weight"), "weight_kg": round(w.get("weight", 0) / 1000, 2) if w.get("weight") else None, "bmi": w.get("bmi"), "body_fat_percent": w.get("bodyFat"),
               "body_water_percent": w.get("bodyWater"), "bone_mass_grams": w.get("boneMass"), "muscle_mass_grams": w.get("muscleMass"), "source_type": w.get("sourceType"), "timestamp_gmt": w.get("timestampGMT")}
        return drop_none(out)

    def weigh_in_days(start: str, end: str) -> list[dict[str, Any]]:
        days = []
        for c in m.chunks("weigh_ins", start, end).data:
            for day in as_dict(c).get("dailyWeightSummaries") or []:
                d = day.get("summaryDate") or ((day.get("allWeightMetrics") or [{}])[0].get("calendarDate"))
                if d and start <= d <= end:
                    days.append(day)
        return days

    @reg.tool(annotations=READ)
    def get_weigh_ins(start_date: str, end_date: str) -> Any:
        """Get weight measurements between specified dates

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
        """
        parse_range(start_date, end_date, 366)
        days = weigh_in_days(start_date, end_date)
        all_m = [w for d in days for w in d.get("allWeightMetrics") or []]
        if not all_m:
            return f"No weight measurements found between {start_date} and {end_date}."
        out: dict[str, Any] = {"date_range": {"start": start_date, "end": end_date}, "measurement_count": len(all_m), "days_with_data": len(days), "measurements": sorted((measurement(w, True) for w in all_m), key=lambda x: x.get("date") or "", reverse=True)}
        weights = [w["weight"] for w in all_m if w.get("weight")]
        if weights:
            out["average_weight_grams"] = round(sum(weights) / len(weights))
            out["average_weight_kg"] = round(sum(weights) / len(weights) / 1000, 2)
        return out

    @reg.tool(annotations=READ)
    def get_daily_weigh_ins(date: str) -> Any:
        """Get weight measurements for a specific date

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        all_m = [w for d in weigh_in_days(date, date) for w in d.get("allWeightMetrics") or []]
        if not all_m:
            return f"No weight measurements found for {date}."
        out: dict[str, Any] = {"date": date, "measurement_count": len(all_m), "measurements": [measurement(w, False) for w in all_m]}
        weights = [w["weight"] for w in all_m if w.get("weight")]
        if weights:
            out["average_weight_grams"] = round(sum(weights) / len(weights))
            out["average_weight_kg"] = round(sum(weights) / len(weights) / 1000, 2)
        return out

    @reg.tool(annotations=DESTRUCTIVE)
    def delete_weigh_ins(date: str, delete_all: bool = True, confirm: bool = False) -> Any:
        """Delete weight measurements for a specific date. This cannot be undone. Call once with confirm=false (the default) to
        preview, then with confirm=true to delete.

        Args:
            date: Date in YYYY-MM-DD format
            delete_all: Whether to delete all measurements for the day
            confirm: Set true after reviewing the preview to perform the deletion
        """
        parse_date(date)
        m.refresh_range_chunk("weigh_ins", date)
        all_m = [w for d in weigh_in_days(date, date) for w in d.get("allWeightMetrics") or []]
        if not all_m or (len(all_m) > 1 and not delete_all):
            msg = f"No weight measurements found for {date}; nothing was deleted" if not all_m else f"Nothing was deleted for {date}: deleting multiple measurements requires delete_all=true"
            return {"status": "nothing_deleted", "date": date, "deleted_count": 0, "message": msg}
        preview = [drop_none({"id": w.get("samplePk"), "weight_kg": round(w["weight"] / 1000, 2) if w.get("weight") is not None else None, "source_type": w.get("sourceType"), "timestamp_gmt": w.get("timestampGMT")}) for w in all_m]
        if not confirm:
            return {"status": "needs_confirmation", "date": date, "delete_all": delete_all, "measurement_count": len(preview), "measurements": preview, "message": "This permanently deletes the listed weight measurements and cannot be undone. Call again with confirm=true to proceed."}
        deleted = 0
        for w in all_m:
            m.write("DELETE", f"/weight-service/weight/{date}/byversion/{w.get('samplePk')}")
            deleted += 1
        m.refresh_range_chunk("weigh_ins", date)
        return WriteResult({"status": "success", "date": date, "deleted_count": deleted, "message": f"Weight measurements deleted for {date}"})

    @reg.tool(annotations=WRITE)
    def add_weigh_in(weight: float, unit_key: str = "kg") -> Any:
        """Add a new weight measurement (now).

        Args:
            weight: Weight value
            unit_key: Unit of weight ('kg' or 'lbs'; 'lb' is accepted as an alias)
        """
        unit_key = unit_key.strip().lower()
        unit_key = "lbs" if unit_key == "lb" else unit_key
        today = m.today().isoformat()
        m.call("add_weigh_in", weight=weight, unitKey=unit_key, refetch=lambda: m.refresh_range_chunk("weigh_ins", today))
        return WriteResult({"status": "success", "weight": weight, "unit": unit_key, "message": "Weight measurement added successfully"})

    @reg.tool(annotations=WRITE)
    def add_weigh_in_with_timestamps(weight: float, unit_key: str = "kg", date_timestamp: str | None = None, gmt_timestamp: str | None = None) -> Any:
        """Add a new weight measurement with specific timestamps. If both are omitted the current time is used; if only
        date_timestamp is given the GMT timestamp is derived from it.

        Args:
            weight: Weight value
            unit_key: Unit of weight ('kg' or 'lbs'; 'lb' is accepted as an alias)
            date_timestamp: Local timestamp in format YYYY-MM-DDThh:mm:ss (required if gmt_timestamp is given)
            gmt_timestamp: GMT timestamp in format YYYY-MM-DDThh:mm:ss
        """
        unit_key = unit_key.strip().lower()
        unit_key = "lbs" if unit_key == "lb" else unit_key
        if not date_timestamp and not gmt_timestamp:
            # The local timestamp sets the day the weigh-in lands on: the profile's zone, not the container's clock.
            now = datetime.now(UTC)
            date_timestamp = now.astimezone(m.engine.zone()).strftime("%Y-%m-%dT%H:%M:%S")
            gmt_timestamp = now.strftime("%Y-%m-%dT%H:%M:%S")
        elif not date_timestamp:
            raise ToolFailure("date_timestamp is required when gmt_timestamp is given: the local timestamp sets the day the weigh-in is recorded on.")
        day = date_timestamp[:10]
        m.call("add_weigh_in_with_timestamps", weight=weight, unitKey=unit_key, dateTimestamp=date_timestamp, gmtTimestamp=gmt_timestamp or "", refetch=lambda: m.refresh_range_chunk("weigh_ins", day))
        return WriteResult({"status": "success", "weight": weight, "unit": unit_key, "date_timestamp": date_timestamp, "gmt_timestamp": gmt_timestamp, "message": "Weight measurement added successfully"})

    # ----------------------------------------------------------------- data entry (upstream data_management)

    @reg.tool(annotations=WRITE)
    def add_body_composition(date: str, weight: float, percent_fat: float | None = None, percent_hydration: float | None = None, visceral_fat_mass: float | None = None, bone_mass: float | None = None, muscle_mass: float | None = None, basal_met: float | None = None, active_met: float | None = None, physique_rating: float | None = None, metabolic_age: float | None = None, visceral_fat_rating: float | None = None, bmi: float | None = None) -> Any:
        """Add a body composition entry (weight in kg plus optional smart-scale metrics) for a date.

        Args:
            date: Date in YYYY-MM-DD format
            weight: Weight in kilograms
            percent_fat: Body fat percentage
            percent_hydration: Body water percentage
            visceral_fat_mass: Visceral fat mass
            bone_mass: Bone mass in kilograms
            muscle_mass: Muscle mass in kilograms
            basal_met: Basal metabolic rate
            active_met: Active metabolic rate
            physique_rating: Physique rating
            metabolic_age: Metabolic age
            visceral_fat_rating: Visceral fat rating
            bmi: Body mass index
        """
        parse_date(date)
        m.call("add_body_composition", date, weight=weight, percent_fat=percent_fat, percent_hydration=percent_hydration, visceral_fat_mass=visceral_fat_mass, bone_mass=bone_mass, muscle_mass=muscle_mass, basal_met=basal_met, active_met=active_met,
               physique_rating=physique_rating, metabolic_age=metabolic_age, visceral_fat_rating=visceral_fat_rating, bmi=bmi, refetch=lambda: m.refresh_range_chunk("weigh_ins", date))
        return WriteResult({"status": "success", "date": date, "weight_kg": weight, "message": "Body composition data added successfully"})

    @reg.tool(annotations=WRITE)
    def set_blood_pressure(systolic: int, diastolic: int, pulse: int, notes: str | None = None) -> Any:
        """Record a blood pressure measurement (now).

        Args:
            systolic: Systolic pressure in mmHg
            diastolic: Diastolic pressure in mmHg
            pulse: Pulse in bpm
            notes: Optional notes
        """
        today = m.today().isoformat()
        m.call("set_blood_pressure", systolic, diastolic, pulse, notes=notes or "", refetch=lambda: m.refresh_range_chunk("blood_pressure", today))
        return WriteResult({"status": "success", "systolic": systolic, "diastolic": diastolic, "pulse": pulse, "message": "Blood pressure recorded successfully"})

    @reg.tool(annotations=WRITE)
    def add_hydration_data(value_in_ml: int, cdate: str, timestamp: str) -> Any:
        """Log a hydration entry.

        Args:
            value_in_ml: Amount of water in millilitres
            cdate: Date in YYYY-MM-DD format
            timestamp: Local timestamp in format YYYY-MM-DDThh:mm:ss
        """
        parse_date(cdate, "cdate")
        m.call("add_hydration_data", value_in_ml=value_in_ml, cdate=cdate, timestamp=timestamp, refetch=lambda: m.refresh_day("hydration", cdate))
        return WriteResult({"status": "success", "date": cdate, "value_in_ml": value_in_ml, "message": "Hydration data added successfully"})

    # ----------------------------------------------------------------- challenges, goals, badges, records

    @reg.tool(annotations=READ)
    def get_goals(goal_type: str = "active") -> Any:
        """Get Garmin Connect goals (active, future, or past), the way the Goals page reads them, so named distance/time targets
        are included with their progress and remaining amounts.

        Args:
            goal_type: Type of goals to retrieve. Options: "active", "future", or "past"
        """
        if goal_type not in {"active", "future", "past"}:
            raise ToolFailure(f"Invalid goal_type {goal_type!r}. Options: active, future, past.")
        goals: list[Any] = []
        if goal_type in ("active", "past"):
            try:
                goals = [(_curate_connect_ui_goal(g) if _is_connect_ui_goal(g) else g) for g in _as_goal_list(m.snapshot(f"goals_{goal_type}").data)]
            except GarminNotFound:
                goals = []
        else:
            try:
                goals = _as_goal_list(m.report("goals_future", "/goal-service/goal/goals", {"status": "future", "start": "1", "limit": "100", "sortOrder": "asc"}, "future").data)
                goals = [(_curate_connect_ui_goal(g) if _is_connect_ui_goal(g) else g) for g in goals]
            except GarminNotFound:
                goals = []
        if not goals and goal_type == "active":
            try:
                goals = _as_goal_list(m.snapshot("user_goals").data)
            except GarminNotFound:
                goals = []
        if not goals:
            return f"No {goal_type} goals found."
        return goals

    @reg.tool(annotations=READ)
    def get_personal_record() -> Any:
        """Get personal records for user"""
        records = m.optional("personal_records", default=[]) or []
        out = []
        for r in records:
            tid = r.get("typeId")
            name, vt = PR_TYPE_MAPPING.get(tid, (f"Unknown Record (typeId={tid})", "unknown"))
            rec = {"record_type": name, "type_id": tid, "value": _format_pr_value(r.get("value"), vt), "raw_value": r.get("value"), "date": _parse_iso_date(r.get("prStartTimeLocalFormatted")) if r.get("prStartTimeLocalFormatted") else _format_timestamp(r.get("prStartTimeGmt")) if r.get("prStartTimeGmt") else None}
            if r.get("activityId"):
                rec["activity_id"] = r["activityId"]
            out.append(drop_none(rec))
        if not out:
            return "No personal records found."
        out.sort(key=lambda x: x["type_id"] or 0)
        return out

    @reg.tool(annotations=READ)
    def get_earned_badges() -> Any:
        """Get earned badges for user"""
        badges = m.optional("earned_badges", default=[]) or []
        out = []
        for b in badges:
            progress, target, unit = b.get("badgeProgressValue"), b.get("badgeTargetValue"), b.get("badgeUnitId")
            cb: dict[str, Any] = {"name": b.get("badgeName"), "category": BADGE_CATEGORY_MAPPING.get(b.get("badgeCategoryId"), f"category_{b.get('badgeCategoryId')}"), "difficulty": BADGE_DIFFICULTY_MAPPING.get(b.get("badgeDifficultyId"), f"level_{b.get('badgeDifficultyId')}"),
                                  "points": b.get("badgePoints"), "earned_date": _parse_iso_date(b.get("badgeEarnedDate")) if b.get("badgeEarnedDate") else None}
            if target is not None and progress is not None:
                cb["progress"], cb["target"] = _format_badge_value(progress, unit), _format_badge_value(target, unit)
            sd = _parse_iso_date(b.get("badgeStartDate")) if b.get("badgeStartDate") else None
            ed = _parse_iso_date(b.get("badgeEndDate")) if b.get("badgeEndDate") else None
            if sd and ed:
                cb["challenge_period"] = f"{sd} to {ed}"
            if b.get("badgeAssocType") == "activityId" and b.get("badgeAssocDataId"):
                cb["activity_id"] = b["badgeAssocDataId"]
            if b.get("badgeSeriesId"):
                cb["series_id"] = b["badgeSeriesId"]
            out.append(cb)
        if not out:
            return "No earned badges found."
        out.sort(key=lambda x: x.get("earned_date") or "", reverse=True)
        return {"total_badges": len(out), "badges": out}

    def page(items: list[Any], start: int, limit: int, one_based: bool) -> list[Any]:
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(start) - (1 if one_based else 0))
        return items[offset : offset + limit]

    @reg.tool(annotations=READ)
    def get_adhoc_challenges(start: int = 0, limit: int = 20) -> Any:
        """Get user-created social/group challenges (e.g. step competitions with friends), distinct from official Garmin badge challenges.

        Args:
            start: Starting index for pagination (default 0)
            limit: Maximum number of challenges to return (default 20, max 100)
        """
        try:
            items = m.snapshot("adhoc_challenges").data or []
        except GarminNotFound:
            items = []
        out = [{"name": c.get("adHocChallengeName"), "description": c.get("adHocChallengeDesc"), "uuid": c.get("uuid"), "activity_type": ADHOC_ACTIVITY_TYPE_MAPPING.get(c.get("socialChallengeActivityTypeId"), f"type_{c.get('socialChallengeActivityTypeId')}"),
                "status": CHALLENGE_STATUS_MAPPING.get(c.get("socialChallengeStatusId"), f"status_{c.get('socialChallengeStatusId')}"), "start_date": _parse_iso_date(c.get("startDate")) if c.get("startDate") else None, "end_date": _parse_iso_date(c.get("endDate")) if c.get("endDate") else None,
                "your_ranking": c.get("userRanking"), "player_count": c.get("playerCount")} for c in items if isinstance(c, dict)]
        out.sort(key=lambda x: x.get("start_date") or "", reverse=True)
        out = page(out, start, limit, False)
        if not out:
            return "No adhoc challenges found."
        return {"total": len(out), "challenges": out}

    def badge_challenges(name: str, start: int, limit: int, sort_key: str, reverse: bool, empty: str, joinable: bool = False) -> Any:
        try:
            items = m.snapshot(name).data or []
        except GarminNotFound:
            items = []
        out = []
        for c in items:
            if not isinstance(c, dict):
                continue
            cur = _curate_badge_challenge(c)
            if joinable:
                cur["joinable"] = c.get("joinable", True)
            out.append(cur)
        out.sort(key=lambda x: x.get(sort_key) or "", reverse=reverse)
        out = page(out, start, limit, True)
        if not out:
            return empty
        return {"total": len(out), "challenges": out}

    @reg.tool(annotations=READ)
    def get_available_badge_challenges(start: int = 1, limit: int = 20) -> Any:
        """Get official Garmin badge challenges available to join (monthly/seasonal challenges that award badges and points).

        Args:
            start: Starting index for pagination (starts at 1)
            limit: Maximum number of challenges to return (default 20, max 100)
        """
        return badge_challenges("available_badge_challenges", start, limit, "start_date", False, "No available badge challenges found.", joinable=True)

    @reg.tool(annotations=READ)
    def get_badge_challenges(start: int = 1, limit: int = 20) -> Any:
        """Get badge challenges the user has completed, with earned dates (for in-progress ones use get_non_completed_badge_challenges).

        Args:
            start: Starting index for pagination (starts at 1)
            limit: Maximum number of challenges to return (default 20, max 100)
        """
        return badge_challenges("badge_challenges", start, limit, "start_date", True, "No badge challenges found.")

    @reg.tool(annotations=READ)
    def get_non_completed_badge_challenges(start: int = 1, limit: int = 20) -> Any:
        """Get badge challenges currently in progress (joined but not yet completed).

        Args:
            start: Starting index for pagination (starts at 1)
            limit: Maximum number of challenges to return (default 20, max 100)
        """
        return badge_challenges("non_completed_badge_challenges", start, limit, "end_date", False, "No in-progress badge challenges found.")

    @reg.tool(annotations=READ)
    def get_race_predictions() -> Any:
        """Get predicted race times (5K, 10K, half marathon, marathon) based on current fitness level and VO2 max."""
        p = m.optional("race_predictions")
        if not p:
            return "No race predictions found."
        return {"prediction_date": p.get("calendarDate"), "predictions": {k: {"time": _format_time(p.get(f)), "time_seconds": p.get(f)} for k, f in (("5K", "time5K"), ("10K", "time10K"), ("half_marathon", "timeHalfMarathon"), ("marathon", "timeMarathon"))}}

    @reg.tool(annotations=READ)
    def get_inprogress_virtual_challenges(start: int = 1, limit: int = 20) -> Any:
        """Get in-progress virtual challenges/expeditions the user is participating in.

        Args:
            start: Starting index for pagination (starts at 1; Garmin rejects 0)
            limit: Maximum number of challenges to return (default 20, max 100)
        """
        try:
            data = m.snapshot("inprogress_virtual_challenges").data
        except GarminNotFound:
            data = []
        items = data.get("challenges", [data]) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        out = []
        for c in items:
            if not isinstance(c, dict):
                continue
            cur: dict[str, Any] = {"name": c.get("badgeChallengeName") or c.get("name") or c.get("challengeName"), "uuid": c.get("uuid"), "start_date": _parse_iso_date(c.get("startDate")) if c.get("startDate") else None, "end_date": _parse_iso_date(c.get("endDate")) if c.get("endDate") else None}
            progress = _first_non_none(c, "badgeProgressValue", "progress", "progressValue")
            target = _first_non_none(c, "badgeTargetValue", "target", "targetValue")
            if progress is not None and target is not None and target > 0:
                unit = c.get("badgeUnitId")
                if unit in (None, 1):
                    cur.update({"progress_meters": progress, "target_meters": target, "progress_km": f"{progress / 1000:.2f} km", "target_km": f"{target / 1000:.2f} km"})
                else:
                    cur.update({"progress": _format_badge_value(progress, unit), "target": _format_badge_value(target, unit)})
                cur["progress_percent"] = f"{progress / target * 100:.1f}%"
            out.append(drop_none(cur))
        out = page(out, start, limit, True)
        if not out:
            return "No in-progress virtual challenges found."
        return {"total": len(out), "challenges": out}

    # ----------------------------------------------------------------- women's health (passthrough, cached)

    @reg.tool(annotations=READ)
    def get_pregnancy_summary() -> Any:
        """Get pregnancy summary data"""
        try:
            return m.report("pregnancy_snapshot", "/periodichealth-service/menstrualcycle/pregnancysnapshot", None, "self").data
        except GarminNotFound:
            return "No pregnancy data found."

    @reg.tool(annotations=READ)
    def get_menstrual_data_for_date(date: str) -> Any:
        """Get menstrual cycle data for a specific date

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        try:
            return m.report("menstrual_dayview", f"/periodichealth-service/menstrualcycle/dayview/{date}", None, date).data
        except GarminNotFound:
            return f"No menstrual data found for {date}."

    @reg.tool(annotations=READ)
    def get_menstrual_calendar_data(start_date: str, end_date: str) -> Any:
        """Get menstrual calendar data between dates

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
        """
        parse_range(start_date, end_date, 366)
        try:
            return m.report("menstrual_calendar", f"/periodichealth-service/menstrualcycle/calendar/{start_date}/{end_date}", None, f"{start_date}_{end_date}").data
        except GarminNotFound:
            return f"No menstrual calendar data found between {start_date} and {end_date}."


__all__ = ["register", "note_provenance", "Provenance"]

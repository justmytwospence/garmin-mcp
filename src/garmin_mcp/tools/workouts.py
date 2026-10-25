"""Workout tools (upstream workouts, workout_builders, calendar_events): the workout
library comes from the mirror; uploads, scheduling and deletions go to Garmin and the
library (and calendar month) are re-fetched."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from ..garmin.datasets import dataset
from ..mirror import GarminNotFound, Provenance, note_provenance
from .base import DESTRUCTIVE, IDEMPOTENT_WRITE, READ, WRITE, NoData, Registry, ToolFailure, WriteResult, drop_none, parse_date, parse_range
from .workout_builders import build_run_interval_json, build_run_json, build_strength_json, build_walk_run_json, build_z2_walk_json
from .workout_support import (
    _curate_scheduled_workout,
    _curate_workout_details,
    _curate_workout_summary,
    _normalize_workout_steps,
    _validate_end_condition_steps,
    _validate_target_type_steps,
)

GRAPHQL = "/graphql-gateway/graphql"
DISTANCE_UNIT_TO_METERS = {"meter": 1.0, "kilometer": 1000.0, "yard": 0.9144, "mile": 1609.344}


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def register(reg: Registry) -> None:
    m = reg.mirror

    def refresh_workouts() -> None:
        m.refresh_snapshot("workouts")

    def refresh_calendar(day: str) -> None:
        d = date.fromisoformat(day)
        m.fetch.snapshot(dataset("calendar_month"), f"{d.year}-{d.month:02d}", priority="interactive")

    def all_workouts() -> list[dict[str, Any]]:
        data = m.optional("workouts", default=[])
        return [w for w in (data if isinstance(data, list) else []) if isinstance(w, dict)]

    def graphql(name: str, query: str, key: str) -> dict[str, Any]:
        try:
            return _as_dict(m.report(name, GRAPHQL, None, key, method="POST", json_body={"query": query}).data)
        except GarminNotFound:
            return {}

    def scheduled_between(start: str, end: str) -> list[dict[str, Any]]:
        result = graphql("scheduled_workouts", f'query{{workoutScheduleSummariesScalar(startDate:"{start}", endDate:"{end}")}}', f"{start}_{end}")
        return [s for s in (_as_dict(result.get("data")).get("workoutScheduleSummariesScalar") or []) if isinstance(s, dict)]

    def already_scheduled(workout_id: int, day: str) -> bool:
        try:
            return any(s.get("workoutId") == workout_id and s.get("scheduleDate") == day for s in scheduled_between(day, day))
        except Exception:  # noqa: BLE001
            return False

    def upload(workout_data: dict[str, Any]) -> dict[str, Any]:
        _normalize_workout_steps(workout_data)
        _validate_end_condition_steps(workout_data)
        _validate_target_type_steps(workout_data)
        result = m.write("POST", "/workout-service/workout", json_body=workout_data, refetch=refresh_workouts)
        if isinstance(result, dict):
            return drop_none({"status": "success", "workout_id": result.get("workoutId"), "name": result.get("workoutName"), "message": "Workout uploaded successfully"})
        return {"status": "success", "result": result}

    def schedule(workout_id: int, day: str) -> dict[str, Any]:
        parse_date(day, "calendar_date")
        if already_scheduled(workout_id, day):
            return {"status": "success", "workout_id": workout_id, "scheduled_date": day, "idempotent": True, "message": f"Workout {workout_id} already scheduled for {day} - no action taken"}
        result = m.write("POST", f"/workout-service/schedule/{workout_id}", json_body={"date": day}, refetch=lambda: refresh_calendar(day))
        m.db.run("DELETE FROM raw_latest WHERE object_type = 'scheduled_workouts'")
        out = {"status": "success", "workout_id": workout_id, "scheduled_date": day, "message": f"Successfully scheduled workout {workout_id} for {day}"}
        if isinstance(result, dict) and result.get("workoutScheduleId"):
            out["scheduled_workout_id"] = result["workoutScheduleId"]
        return out

    @reg.tool(annotations=READ)
    def get_workouts() -> Any:
        """Get all workouts with curated summary list (count and essential metadata; use get_workout_by_id for segments)."""
        workouts = all_workouts()
        if not workouts:
            return "No workouts found."
        return {"count": len(workouts), "workouts": [_curate_workout_summary(w) for w in workouts]}

    @reg.tool(annotations=READ)
    def get_workout_by_id(workout_id: int | str, raw: bool = False) -> Any:
        """Get detailed information for a specific workout, including segments and step structure.

        Accepts a numeric workout ID (from get_workouts or get_scheduled_workouts) or a workout UUID (adaptive Garmin Coach/training
        plan workouts). Pace target values are speeds in meters per second; target_value_low/high preserve Garmin's order.

        Args:
            workout_id: Workout ID (numeric) or UUID (for training plan workouts)
            raw: Return Garmin's full workout JSON (the DTO format upload_workout describes) instead of the simplified summary.
        """
        wid = str(workout_id).strip()
        try:
            if "-" in wid:
                workout = m.report("workout_fbt_adaptive", f"/workout-service/fbt-adaptive/{wid}", None, wid).data
            else:
                workout = m.snapshot("workout", wid).data
        except GarminNotFound as err:
            raise NoData(f"No workout found with ID {wid}. ({err})") from err
        if not workout:
            raise NoData(f"No workout found with ID {wid}.")
        return workout if raw else _curate_workout_details(workout)

    @reg.tool(annotations=READ)
    def download_workout(workout_id: int) -> Any:
        """Download a workout as a FIT file. The binary cannot be returned through MCP; this confirms the workout is available and reports its size.

        Args:
            workout_id: ID of the workout to download
        """
        data = m.client.get_bytes(f"/workout-service/workout/FIT/{int(workout_id)}", priority="interactive")
        note_provenance(Provenance("garmin", None))
        if not data:
            raise NoData(f"No workout data found for workout with ID {workout_id}.")
        return {"workout_id": workout_id, "format": "FIT", "size_bytes": len(data), "message": "Workout data is available in FIT format. Use Garmin Connect API to save to file."}

    UPLOAD_DOC = """Upload a workout from JSON data (creates a new workout in Garmin Connect).

        Step types use Garmin's DTO format: "ExecutableStepDTO" for regular steps, "RepeatGroupDTO" for repeat groups with
        numberOfIterations and an endCondition {conditionTypeId 7, "iterations"}. stepOrder is one depth-first counter over the whole
        workout. Heart-rate targets: named zone -> targetType "heart.rate.zone" (workoutTargetTypeId 4) with zoneNumber 1-5; custom
        bpm range -> targetValueOne/targetValueTwo, no zoneNumber. Pace targets are speeds in m/s (5:00/km = 3.333). Cycling power:
        zone -> workoutTargetTypeId 2 "power.zone" with zoneNumber 1-7; percent FTP -> id 2 with targetValueOne/Two and
        targetValueUnit {"unitKey": "percent"}; absolute watts -> workoutTargetTypeId 6 "power.between". Sport type ids: 1 running,
        2 cycling, 5 strength_training, 6 cardio_training, 12 walking. End condition ids: 1 lap.button, 2 time, 3 distance,
        4 calories, 5 power, 6 heart.rate, 7 iterations, 10 reps. Strength steps add category, exerciseName, weightValue and
        weightUnit {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}. Mismatched ids/keys are rejected before upload.
        Templates: resources workout://templates/simple-run, interval-running, tempo-run, strength-circuit and
        workout://reference/structure.

        Args:
            workout_data: Dictionary containing workout structure (name, sport type, segments, etc.)
        """

    @reg.tool(annotations=WRITE, description=UPLOAD_DOC)
    def upload_workout(workout_data: dict[str, Any]) -> Any:
        return WriteResult(upload(workout_data))

    @reg.tool(annotations=WRITE)
    def upload_workouts(workouts: list[dict[str, Any]]) -> Any:
        """Upload multiple workouts from JSON data in a single call. Each item uses the same structure and rules as upload_workout.
        Each workout is handled on its own: a failing one is reported and the rest still upload.

        Args:
            workouts: List of workout dictionaries, each with the upload_workout structure
        """
        results = []
        for i, w in enumerate(workouts):
            try:
                results.append({"index": i, **upload(dict(w))})
            except Exception as err:  # noqa: BLE001
                results.append({"index": i, "status": "failed", "name": (w or {}).get("workoutName"), "message": str(err)})
        ok = sum(1 for r in results if r.get("status") == "success")
        return WriteResult({"total": len(results), "succeeded": ok, "failed": len(results) - ok, "results": results}, True if ok else False)

    def preview(workout_id: int) -> dict[str, Any]:
        for w in all_workouts():
            if w.get("workoutId") == workout_id:
                s = _curate_workout_summary(w)
                return drop_none({"workout_id": workout_id, "name": s.get("name"), "sport": s.get("sport"), "provider": s.get("provider"), "updated_date": s.get("updated_date")})
        return {"workout_id": workout_id}

    @reg.tool(annotations=DESTRUCTIVE)
    def delete_workout(workout_id: int, confirm: bool = False) -> Any:
        """Permanently delete a workout from Garmin Connect. Call once with confirm=false (the default) to preview, then with confirm=true.

        Args:
            workout_id: ID of the workout to delete
            confirm: Must be true to actually delete. Default false returns a preview.
        """
        p = preview(int(workout_id))
        if not confirm:
            return {"status": "needs_confirmation", "workout": p, "message": "This permanently deletes the workout from Garmin Connect and cannot be undone. Call again with confirm=true to proceed."}
        m.write("DELETE", f"/workout-service/workout/{int(workout_id)}", refetch=refresh_workouts)
        return WriteResult({"status": "deleted", "workout": p, "message": f"Workout {workout_id} deleted"})

    @reg.tool(annotations=DESTRUCTIVE)
    def delete_workouts(workout_ids: list[int], confirm: bool = False) -> Any:
        """Permanently delete several workouts. Call once with confirm=false to preview them, then with confirm=true.

        Args:
            workout_ids: IDs of the workouts to delete
            confirm: Must be true to actually delete. Default false returns a preview.
        """
        previews = [preview(int(w)) for w in workout_ids]
        if not confirm:
            return {"status": "needs_confirmation", "workouts": previews, "message": "This permanently deletes the listed workouts and cannot be undone. Call again with confirm=true to proceed."}
        results = []
        for wid in workout_ids:
            try:
                m.write("DELETE", f"/workout-service/workout/{int(wid)}")
                results.append({"workout_id": wid, "status": "deleted"})
            except Exception as err:  # noqa: BLE001
                results.append({"workout_id": wid, "status": "failed", "message": str(err)})
        try:
            refresh_workouts()
        except Exception:  # noqa: BLE001
            m.enqueue_refresh("snap:workouts", "self")
        ok = sum(1 for r in results if r["status"] == "deleted")
        return WriteResult({"total": len(results), "deleted": ok, "failed": len(results) - ok, "results": results}, True if ok else False)

    @reg.tool(annotations=READ)
    def get_scheduled_workouts(start_date: str, end_date: str) -> Any:
        """Get scheduled workouts between two dates: workouts on the Garmin Connect calendar with their dates and completion status.

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
        """
        parse_range(start_date, end_date, 366)
        scheduled = scheduled_between(start_date, end_date)
        if not scheduled:
            return f"No workouts scheduled between {start_date} and {end_date}."
        return {"count": len(scheduled), "date_range": {"start": start_date, "end": end_date}, "scheduled_workouts": [_curate_scheduled_workout(s) for s in scheduled]}

    def coach_workouts(calendar_date: str) -> Any:
        parse_date(calendar_date, "calendar_date")
        result = graphql("training_plan_week", f'query{{trainingPlanScalar(calendarDate:"{calendar_date}", lang:"en-US", firstDayOfWeek:"monday")}}', calendar_date)
        plan_data = _as_dict(_as_dict(result.get("data")).get("trainingPlanScalar"))
        plans_raw = plan_data.get("trainingPlanWorkoutScheduleDTOS") or []
        if not isinstance(plans_raw, list) or not plans_raw:
            return f"No training plan workouts scheduled for {calendar_date}."
        workouts, names, plans = [], [], []
        for plan in plans_raw:
            if not isinstance(plan, dict):
                continue
            if plan.get("planName") and plan["planName"] not in names:
                names.append(plan["planName"])
            det = _as_dict(plan.get("trainingPlanDetailsDTO"))
            race = _as_dict(det.get("athleteRace"))
            ps = drop_none({"name": plan.get("planName"), "training_plan_id": plan.get("trainingPlanId"), "classification": plan.get("trainingPlanClassification"), "training_type": det.get("trainingType"), "race_name": race.get("raceName"),
                            "race_day": race.get("raceDay"), "workouts_per_week": det.get("workoutsPerWeek"), "remaining_workouts_this_week": det.get("remainingAtpWorkoutsForWeek"), "registration_date": det.get("registrationDate"), "plan_completed": det.get("planCompleted")})
            if ps:
                plans.append(ps)
            workouts.extend(_curate_scheduled_workout(w) for w in (plan.get("workoutScheduleSummaries") or []) if isinstance(w, dict))
        return drop_none({"date": calendar_date, "training_plans": names or None, "plans": plans or None, "count": len(workouts), "workouts": workouts})

    @reg.tool(annotations=READ)
    def get_garmin_coach_workouts(calendar_date: str) -> Any:
        """Get the active Garmin Coach plan's workouts for the week containing calendar_date (plan summary plus each scheduled workout; the count includes rest days).

        Args:
            calendar_date: Reference date in YYYY-MM-DD format (returns week's workouts)
        """
        return coach_workouts(calendar_date)

    @reg.tool(annotations=READ)
    def get_training_plan_workouts(calendar_date: str) -> Any:
        """Get the active training plan's workouts for the week containing calendar_date (same data as get_garmin_coach_workouts; pass a workout_uuid to get_workout_by_id for details).

        Args:
            calendar_date: Reference date in YYYY-MM-DD format (returns week's workouts)
        """
        return coach_workouts(calendar_date)

    @reg.tool(annotations=WRITE)
    def schedule_workout(workout_id: int, calendar_date: str) -> Any:
        """Schedule a workout on the Garmin Connect calendar. Idempotent: a workout already scheduled on that date is reported and not duplicated.

        Args:
            workout_id: ID of the workout to schedule
            calendar_date: Date to schedule the workout in YYYY-MM-DD format
        """
        return WriteResult(schedule(int(workout_id), calendar_date))

    @reg.tool(annotations=WRITE)
    def schedule_workouts(schedules: list[dict[str, Any]]) -> Any:
        """Schedule multiple workouts in a single call. Each item references an existing workout by workout_id, or provides inline
        workout_data (upload_workout structure) to upload-and-schedule in one step. Each item is handled on its own.

        Args:
            schedules: List of dicts with calendar_date (YYYY-MM-DD, required) and workout_id (int) or workout_data (dict)
        """
        results = []
        for item in schedules:
            wid, day, wdata = item.get("workout_id"), item.get("calendar_date"), item.get("workout_data")
            try:
                if not day:
                    raise ValueError("calendar_date is required")
                if wid is None and wdata:
                    up = upload(dict(wdata))
                    wid = up.get("workout_id")
                    if wid is None:
                        raise ValueError("upload did not return a workout id")
                if wid is None:
                    raise ValueError("workout_id or workout_data is required")
                results.append(schedule(int(wid), str(day)))
            except Exception as err:  # noqa: BLE001
                results.append({"status": "failed", "workout_id": wid, "scheduled_date": day, "message": str(err)})
        ok = sum(1 for r in results if r.get("status") == "success")
        return WriteResult({"total": len(results), "succeeded": ok, "failed": len(results) - ok, "results": results}, True if ok else False)

    @reg.tool(annotations=IDEMPOTENT_WRITE)
    def reschedule_workout(scheduled_workout_id: int, calendar_date: str) -> Any:
        """Move a scheduled workout to another date in place (same calendar-entry id, no duplicate). scheduled_workout_id is the
        calendar-entry id from get_scheduled_workouts, not the workout's id.

        Args:
            scheduled_workout_id: Calendar-entry id from get_scheduled_workouts
            calendar_date: New date in YYYY-MM-DD format
        """
        parse_date(calendar_date, "calendar_date")
        sid = int(scheduled_workout_id)
        try:
            current = m.report("scheduled_workout", f"/workout-service/schedule/{sid}", None, str(sid)).data
        except GarminNotFound as err:
            raise ToolFailure(f"Scheduled workout {sid} not found ({err})") from err
        workout = _as_dict(_as_dict(current).get("workout"))
        payload = {**_as_dict(current), "calendarDate": calendar_date, "workout": {"workoutId": workout.get("workoutId")} if workout else None}
        prev = _as_dict(current).get("calendarDate")

        def refetch() -> None:
            refresh_calendar(calendar_date)
            if prev:
                refresh_calendar(prev)
            m.db.run("DELETE FROM raw_latest WHERE object_type IN ('scheduled_workouts', 'scheduled_workout')")

        m.write("PUT", f"/workout-service/schedule/{sid}", json_body=payload, refetch=refetch)
        return WriteResult(drop_none({"status": "success", "scheduled_workout_id": sid, "workout_id": workout.get("workoutId"), "previous_date": prev, "scheduled_date": calendar_date, "message": f"Moved scheduled workout {sid} to {calendar_date}"}))

    @reg.tool(annotations=DESTRUCTIVE)
    def unschedule_workout(scheduled_workout_id: int) -> Any:
        """Remove a scheduled workout from the calendar without deleting the workout template. scheduled_workout_id is the
        calendar-entry id from get_scheduled_workouts, not the workout's id.

        Args:
            scheduled_workout_id: Calendar-entry id from get_scheduled_workouts
        """
        sid = int(scheduled_workout_id)
        m.write("DELETE", f"/workout-service/schedule/{sid}", refetch=lambda: m.db.run("DELETE FROM raw_latest WHERE object_type IN ('scheduled_workouts', 'scheduled_workout')"))
        m.enqueue_refresh("snap:calendar_month", f"{m.today().year}-{m.today().month:02d}")
        return WriteResult({"status": "success", "scheduled_workout_id": sid, "message": f"Scheduled workout {sid} removed from calendar"})

    @reg.tool(annotations=DESTRUCTIVE)
    def unschedule_workouts(scheduled_workout_ids: list[int]) -> Any:
        """Remove several scheduled workouts from the calendar in one call (workout templates stay in the library). Each id is a calendar-entry id.

        Args:
            scheduled_workout_ids: Calendar-entry ids from get_scheduled_workouts
        """
        results = []
        for sid in scheduled_workout_ids:
            try:
                m.write("DELETE", f"/workout-service/schedule/{int(sid)}")
                results.append({"scheduled_workout_id": sid, "status": "success"})
            except Exception as err:  # noqa: BLE001
                results.append({"scheduled_workout_id": sid, "status": "failed", "message": str(err)})
        m.db.run("DELETE FROM raw_latest WHERE object_type IN ('scheduled_workouts', 'scheduled_workout')")
        ok = sum(1 for r in results if r["status"] == "success")
        return WriteResult({"total": len(results), "succeeded": ok, "failed": len(results) - ok, "results": results}, True if ok else False)

    # ----------------------------------------------------------------- builders (upstream workout_builders)

    def built(workout: dict[str, Any]) -> WriteResult:
        return WriteResult(upload(workout))

    @reg.tool(annotations=WRITE)
    def create_walk_run_workout(name: str, run_seconds: int, walk_seconds: int, repeats: int, warmup_min: int, cooldown_min: int, hr_zone: str = "Z3") -> Any:
        """Create a run/walk interval workout (warmup, repeats of run then walk, cooldown) and upload it to Garmin Connect.

        Args:
            name: Workout name
            run_seconds: Run interval length in seconds
            walk_seconds: Walk interval length in seconds
            repeats: Number of run/walk repeats
            warmup_min: Warmup minutes
            cooldown_min: Cooldown minutes
            hr_zone: Heart-rate zone for the run intervals (Z1-Z5, default Z3)
        """
        return built(build_walk_run_json(name, run_seconds, walk_seconds, repeats, warmup_min, cooldown_min, hr_zone))

    @reg.tool(annotations=WRITE)
    def create_run_workout(name: str, run_seconds: int, warmup_min: int, cooldown_min: int, hr_zone: str = "Z3", hr_min: int | None = None, hr_max: int | None = None) -> Any:
        """Create a continuous run workout (warmup, run, cooldown) targeting a heart-rate zone, or a custom bpm range when hr_min and hr_max are given, and upload it.

        Args:
            name: Workout name
            run_seconds: Run duration in seconds
            warmup_min: Warmup minutes
            cooldown_min: Cooldown minutes
            hr_zone: Heart-rate zone for the run (Z1-Z5, default Z3)
            hr_min: Custom heart-rate range lower bound in bpm (with hr_max)
            hr_max: Custom heart-rate range upper bound in bpm (with hr_min)
        """
        return built(build_run_json(name, run_seconds, warmup_min, cooldown_min, hr_zone, hr_min, hr_max))

    @reg.tool(annotations=WRITE)
    def create_run_interval_workout(name: str, repeats: int, rep_seconds: int, recovery_seconds: int, warmup_min: int, cooldown_min: int, hr_zone: str = "Z4", hr_min: int | None = None, hr_max: int | None = None, recovery_hr_min: int | None = None, recovery_hr_max: int | None = None) -> Any:
        """Create a running interval workout (warmup, repeats of work then recovery, cooldown) and upload it. Work and recovery
        target a zone or, with the *_hr_min/*_hr_max pairs, custom bpm ranges.

        Args:
            name: Workout name
            repeats: Number of work/recovery repeats
            rep_seconds: Work interval length in seconds
            recovery_seconds: Recovery interval length in seconds
            warmup_min: Warmup minutes
            cooldown_min: Cooldown minutes
            hr_zone: Heart-rate zone for the work intervals (Z1-Z5, default Z4)
            hr_min: Custom work range lower bound in bpm
            hr_max: Custom work range upper bound in bpm
            recovery_hr_min: Custom recovery range lower bound in bpm
            recovery_hr_max: Custom recovery range upper bound in bpm
        """
        return built(build_run_interval_json(name, repeats, rep_seconds, recovery_seconds, warmup_min, cooldown_min, hr_zone, hr_min, hr_max, recovery_hr_min, recovery_hr_max))

    @reg.tool(annotations=WRITE)
    def create_z2_walk_workout(name: str, duration_min: int, hr_min: int, hr_max: int) -> Any:
        """Create a walking workout held in a custom heart-rate range (Zone 2 walk) and upload it.

        Args:
            name: Workout name
            duration_min: Duration in minutes
            hr_min: Heart-rate range lower bound in bpm
            hr_max: Heart-rate range upper bound in bpm
        """
        return built(build_z2_walk_json(name, duration_min, hr_min, hr_max))

    @reg.tool(annotations=WRITE)
    def create_strength_workout(name: str, exercises: list[dict[str, Any]]) -> Any:
        """Create a strength workout and upload it to Garmin Connect. Each exercise becomes a reps-based step; when sets > 1 it is a
        repeat group of that many iterations (work + rest per set). The name is kept in the step description and sent as
        exerciseName, which Garmin retains only when it matches one of its own exercise keys (e.g. "FARMERS_CARRY").

        Args:
            name: Workout name (max 256 characters; Garmin silently truncates longer names)
            exercises: List of dicts with keys: name, sets, reps, rest_seconds and an optional category (one of Garmin's exercise
                categories such as SQUAT, DEADLIFT, PUSH_UP; see https://connect.garmin.com/web-data/exercises/Exercises.json)
        """
        return built(build_strength_json(name, exercises))

    @reg.tool(annotations=WRITE)
    def schedule_week(week: list[dict[str, Any]]) -> Any:
        """Schedule several workouts on their dates in one call. Idempotent: a workout already scheduled on that date is reported
        and the POST skipped. Each entry is handled on its own.

        Args:
            week: List of dicts with keys: date (YYYY-MM-DD), workout_id (int)
        """
        results = []
        for item in week:
            day, wid = item.get("date"), item.get("workout_id")
            try:
                if not day:
                    raise ValueError("Missing required field: date")
                if wid is None or int(wid) < 1:
                    raise ValueError("workout_id is required and must be a positive integer")
                r = schedule(int(wid), str(day))
                results.append({"date": day, **{k: v for k, v in r.items() if k != "scheduled_date"}})
            except Exception as err:  # noqa: BLE001
                results.append({"date": day, "workout_id": wid, "status": "failed", "message": str(err)})
        ok = sum(1 for r in results if r.get("status") == "success")
        return WriteResult({"total": len(results), "succeeded": ok, "failed": len(results) - ok, "results": results}, True if ok else False)

    # ----------------------------------------------------------------- calendar events (upstream calendar_events)

    @reg.tool(annotations=READ)
    def get_calendar_events(start_date: str, end_date: str) -> Any:
        """Get races and events on the Garmin Connect calendar between two dates (entries the user added or subscribed to, such as
        upcoming races; not returned by get_scheduled_workouts or get_goals). Each event reports distance in meters when stored, the
        custom goal time in seconds when set, the local start time, is_race and primary_event (the goal race of an active plan).

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
        """
        s, e = parse_range(start_date, end_date, 731)
        events: list[dict[str, Any]] = []
        seen = set()
        y, mo = s.year, s.month
        while (y, mo) <= (e.year, e.month):
            try:
                data = m.snapshot("calendar_month", f"{y}-{mo:02d}", max_age=_month_age(y, mo, m.today())).data
            except GarminNotFound:
                data = {}
            for item in (_as_dict(data).get("calendarItems") or []):
                if not isinstance(item, dict) or item.get("itemType") != "event":
                    continue
                day = item.get("date")
                if not isinstance(day, str) or not start_date <= day <= end_date:
                    continue
                key = (item.get("shareableEventUuid"), item.get("title"), day)
                if key in seen:
                    continue
                seen.add(key)
                detail: dict[str, Any] = {}
                try:
                    if item.get("id") is not None:
                        detail = _as_dict(m.report("calendar_event_detail", f"/calendar-service/event/{item['id']}", None, str(item["id"])).data)
                    elif item.get("shareableEventUuid"):
                        detail = _as_dict(m.report("calendar_event_detail", f"/calendar-service/event/{item['shareableEventUuid']}/shareable", None, str(item["shareableEventUuid"])).data)
                except Exception:  # noqa: BLE001
                    detail = {}
                target = _as_dict(item.get("completionTarget"))
                dist = None
                if target.get("unitType") == "distance" and isinstance(target.get("value"), (int, float)) and target.get("unit") in DISTANCE_UNIT_TO_METERS:
                    dist = float(target["value"]) * DISTANCE_UNIT_TO_METERS[target["unit"]]
                goal = _as_dict(_as_dict(detail.get("eventCustomization")).get("customGoal"))
                goal_s = float(goal["value"]) if goal.get("unitType") == "time" and goal.get("unit") == "second" and isinstance(goal.get("value"), (int, float)) else None
                et = _as_dict(item.get("eventTimeLocal"))
                loc = item.get("location").strip() if isinstance(item.get("location"), str) else None
                events.append({"title": item.get("title"), "date": day, "is_race": bool(item.get("isRace")), "primary_event": bool(item.get("primaryEvent")), "subscribed": bool(item.get("subscribed")), "distance_meters": dist, "goal_time_seconds": goal_s,
                               "start_time_local": et.get("startTimeHhMm"), "time_zone": et.get("timeZoneId"), "location": loc or None, "url": item.get("url"), "event_uuid": item.get("shareableEventUuid")})
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        events.sort(key=lambda ev: (ev["date"], ev["title"] or ""))
        return {"count": len(events), "date_range": {"start": start_date, "end": end_date}, "events": events}


def _month_age(y: int, mo: int, today: date):  # noqa: ANN202
    from datetime import timedelta

    if abs((date(y, mo, 1) - date(today.year, today.month, 1)).days) <= 31:
        return timedelta(hours=6)
    return timedelta(days=36500)


__all__ = ["register", "json"]

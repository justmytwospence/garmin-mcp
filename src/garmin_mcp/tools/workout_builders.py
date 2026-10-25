# Vendored from https://github.com/Taxuspt/garmin_mcp (MIT), src/garmin_mcp/workout_builders.py, at upstream commit a2ca5fc (2026-10-20), vendored 2026-10-22.
"""Workout JSON builders for Garmin Connect's workout-service.

Vendored from Taxuspt/garmin_mcp (MIT), src/garmin_mcp/workout_builders.py: the payload
shapes Garmin's workout editor sends for run, run/walk, interval, Zone 2 walk and
strength workouts.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

HR_ZONE_MAP = {"Z1": 1, "Z2": 2, "Z3": 3, "Z4": 4, "Z5": 5}

def _require_minimum(minimum: int, **fields: int) -> None:
    """Reject durations/counts that cannot describe the requested workout."""
    for field, value in fields.items():
        if value < minimum:
            raise ValueError(f"{field} must be >= {minimum}")


def _zone_number(zone: str) -> int:
    """Resolve a human-friendly zone string like 'Z3' to Garmin's zoneNumber."""
    zone_upper = zone.strip().upper()
    if zone_upper in HR_ZONE_MAP:
        return HR_ZONE_MAP[zone_upper]
    # Fallback: if user passed a digit directly
    try:
        z = int(zone_upper)
        if 1 <= z <= 5:
            return z
    except ValueError:
        pass
    raise ValueError(f"Invalid hr_zone '{zone}'. Use Z1-Z5 or 1-5.")


def _hr_target(
    hr_zone: str,
    hr_min: Optional[int],
    hr_max: Optional[int],
    prefix: str = "",
) -> tuple:
    """Resolve HR target fields and a short description suffix.

    Returns (target_extra_fields, description_suffix). If hr_min/hr_max are
    both given, builds a custom bpm-range target (targetValueOne/targetValueTwo)
    instead of a named Garmin zone. A custom range and a named zone are mutually
    exclusive -- if a range is given, hr_zone is ignored. prefix names the
    caller's parameters in errors (e.g. "recovery_" for recovery_hr_min).
    """
    if hr_min is not None or hr_max is not None:
        if hr_min is None or hr_max is None:
            raise ValueError(
                f"{prefix}hr_min and {prefix}hr_max must both be provided together."
            )
        if hr_min >= hr_max:
            raise ValueError(
                f"{prefix}hr_min ({hr_min}) must be less than "
                f"{prefix}hr_max ({hr_max})."
            )
        return (
            {"targetValueOne": float(hr_min), "targetValueTwo": float(hr_max)},
            f"{hr_min}-{hr_max}bpm",
        )
    zone = _zone_number(hr_zone)
    return ({"zoneNumber": zone}, f"Z{zone}")


def build_run_json(
    name: str,
    run_seconds: int,
    warmup_min: int,
    cooldown_min: int,
    hr_zone: str = "Z3",
    hr_min: Optional[int] = None,
    hr_max: Optional[int] = None,
) -> dict:
    """Build the Garmin Connect JSON for a continuous run workout.

    Targets a named heart-rate zone (hr_zone) by default. Pass hr_min and
    hr_max together to target an exact custom bpm range instead (e.g. a
    136-148 bpm range that doesn't line up with any single Garmin zone).
    """
    _require_minimum(1, run_seconds=run_seconds)
    _require_minimum(0, warmup_min=warmup_min, cooldown_min=cooldown_min)
    hr_target_fields, hr_desc = _hr_target(hr_zone, hr_min, hr_max)
    run_display = (
        f"{run_seconds // 60}m" if run_seconds % 60 == 0 else f"{run_seconds}s"
    )
    return {
        "workoutName": name,
        "description": (
            f"{warmup_min}m warmup + {run_display} run {hr_desc} + {cooldown_min}m cooldown"
        ),
        "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
            "workoutSteps": [
                {
                    "type": "ExecutableStepDTO",
                    "stepOrder": 1,
                    "stepType": {"stepTypeId": 1, "stepTypeKey": "warmup"},
                    "description": f"Warmup {warmup_min} min",
                    "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                    "endConditionValue": float(warmup_min * 60),
                    "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
                },
                {
                    "type": "ExecutableStepDTO",
                    "stepOrder": 2,
                    "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
                    "description": f"Run {run_seconds}s {hr_desc}",
                    "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                    "endConditionValue": float(run_seconds),
                    "targetType": {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"},
                    **hr_target_fields,
                },
                {
                    "type": "ExecutableStepDTO",
                    "stepOrder": 3,
                    "stepType": {"stepTypeId": 2, "stepTypeKey": "cooldown"},
                    "description": f"Cooldown {cooldown_min} min",
                    "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                    "endConditionValue": float(cooldown_min * 60),
                    "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
                },
            ],
        }],
    }


def build_run_interval_json(
    name: str,
    repeats: int,
    rep_seconds: int,
    recovery_seconds: int,
    warmup_min: int,
    cooldown_min: int,
    hr_zone: str = "Z4",
    hr_min: Optional[int] = None,
    hr_max: Optional[int] = None,
    recovery_hr_min: Optional[int] = None,
    recovery_hr_max: Optional[int] = None,
) -> dict:
    """Build the Garmin Connect JSON for a repeat-based interval run workout.

    Structure: warmup -> repeats x [work interval + jog recovery] -> cooldown.

    The work intervals target a named heart-rate zone (hr_zone) by default;
    pass hr_min and hr_max together to target an exact custom bpm range
    instead. Named zones are usually too wide for interval work -- a
    lactate-threshold session at 172-180 bpm sits inside a Z3 that may span
    158-184, so the watch would report "in range" for efforts well below and
    above the intended one.

    Recovery steps carry no target unless recovery_hr_min/max are given.
    A capped recovery alerts continuously for the first minute or two after
    a hard rep, because heart rate lags the effort -- usually noise rather
    than signal.
    """
    _require_minimum(1, repeats=repeats, rep_seconds=rep_seconds)
    _require_minimum(0, recovery_seconds=recovery_seconds, warmup_min=warmup_min, cooldown_min=cooldown_min)

    hr_target_fields, hr_desc = _hr_target(hr_zone, hr_min, hr_max)
    rec_fields = None
    if recovery_hr_min is not None or recovery_hr_max is not None:
        rec_fields, rec_desc = _hr_target(
            None, recovery_hr_min, recovery_hr_max, prefix="recovery_"
        )
        if recovery_seconds == 0:
            raise ValueError("recovery_hr_min/recovery_hr_max require recovery_seconds > 0")

    rep_display = f"{rep_seconds // 60}m" if rep_seconds % 60 == 0 else f"{rep_seconds}s"
    rec_display = (
        f"{recovery_seconds // 60}m"
        if recovery_seconds % 60 == 0
        else f"{recovery_seconds}s"
    )

    # Steps inside the repeat group. The group itself occupies one step order
    # and its children continue numbering from there, so a mis-numbered child
    # is the usual cause of a workout that uploads cleanly but renders
    # scrambled on the watch.
    repeat_children = [
        {
            "type": "ExecutableStepDTO",
            "stepOrder": 3,
            "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
            "description": f"Rep {rep_seconds}s {hr_desc}",
            "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
            "endConditionValue": float(rep_seconds),
            "targetType": {
                "workoutTargetTypeId": 4,
                "workoutTargetTypeKey": "heart.rate.zone",
            },
            **hr_target_fields,
        }
    ]

    if recovery_seconds > 0:
        recovery_step = {
            "type": "ExecutableStepDTO",
            "stepOrder": 4,
            "stepType": {"stepTypeId": 4, "stepTypeKey": "recovery"},
            "description": f"Jog {recovery_seconds}s",
            "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
            "endConditionValue": float(recovery_seconds),
            "targetType": {
                "workoutTargetTypeId": 1,
                "workoutTargetTypeKey": "no.target",
            },
        }
        if rec_fields is not None:
            recovery_step["targetType"] = {
                "workoutTargetTypeId": 4,
                "workoutTargetTypeKey": "heart.rate.zone",
            }
            recovery_step.update(rec_fields)
            recovery_step["description"] = f"Jog {recovery_seconds}s {rec_desc}"
        repeat_children.append(recovery_step)

    cooldown_order = 3 + len(repeat_children)
    interval_description = f"{repeats}x({rep_display} {hr_desc}"
    if recovery_seconds > 0:
        interval_description += f" / {rec_display} jog"
    interval_description += ")"

    return {
        "workoutName": name,
        "description": (
            f"{warmup_min}m warmup + {interval_description} + {cooldown_min}m cooldown"
        ),
        "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
                "workoutSteps": [
                    {
                        "type": "ExecutableStepDTO",
                        "stepOrder": 1,
                        "stepType": {"stepTypeId": 1, "stepTypeKey": "warmup"},
                        "description": f"Warmup {warmup_min} min",
                        "endCondition": {
                            "conditionTypeId": 2,
                            "conditionTypeKey": "time",
                        },
                        "endConditionValue": float(warmup_min * 60),
                        "targetType": {
                            "workoutTargetTypeId": 1,
                            "workoutTargetTypeKey": "no.target",
                        },
                    },
                    {
                        "type": "RepeatGroupDTO",
                        "stepOrder": 2,
                        "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
                        "numberOfIterations": repeats,
                        "smartRepeat": False,
                        "endCondition": {
                            "conditionTypeId": 7,
                            "conditionTypeKey": "iterations",
                        },
                        "endConditionValue": float(repeats),
                        "workoutSteps": repeat_children,
                    },
                    {
                        "type": "ExecutableStepDTO",
                        "stepOrder": cooldown_order,
                        "stepType": {"stepTypeId": 2, "stepTypeKey": "cooldown"},
                        "description": f"Cooldown {cooldown_min} min",
                        "endCondition": {
                            "conditionTypeId": 2,
                            "conditionTypeKey": "time",
                        },
                        "endConditionValue": float(cooldown_min * 60),
                        "targetType": {
                            "workoutTargetTypeId": 1,
                            "workoutTargetTypeKey": "no.target",
                        },
                    },
                ],
            }
        ],
    }


def build_walk_run_json(
    name: str,
    run_seconds: int,
    walk_seconds: int,
    repeats: int,
    warmup_min: int,
    cooldown_min: int,
    hr_zone: str = "Z3",
) -> dict:
    """Build the Garmin Connect JSON for a walk/run interval workout.

    Parameters match create_walk_run_workout exactly.

    stepOrder is one depth-first counter over the whole workout, the way
    Garmin Connect numbers its own workouts: the repeat group takes 2, its
    children continue at 3 and 4, and the cooldown is 5. Restarting at 1
    inside the group uploads cleanly but leaves duplicate step orders. The
    group also carries the explicit repeat stepType and iterations
    endCondition, as build_run_interval_json does.
    """
    _require_minimum(1, run_seconds=run_seconds, walk_seconds=walk_seconds, repeats=repeats)
    _require_minimum(0, warmup_min=warmup_min, cooldown_min=cooldown_min)
    zone = _zone_number(hr_zone)
    return {
        "workoutName": name,
        "description": (
            f"{warmup_min}m warmup + {repeats}x({run_seconds}s run / {walk_seconds}s walk) Z{zone} + "
            f"{cooldown_min}m cooldown"
        ),
        "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
            "workoutSteps": [
                {
                    "type": "ExecutableStepDTO",
                    "stepOrder": 1,
                    "stepType": {"stepTypeId": 1, "stepTypeKey": "warmup"},
                    "description": f"Warmup {warmup_min} min",
                    "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                    "endConditionValue": float(warmup_min * 60),
                    "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
                },
                {
                    "type": "RepeatGroupDTO",
                    "stepOrder": 2,
                    "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
                    "numberOfIterations": repeats,
                    "smartRepeat": False,
                    "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations"},
                    "endConditionValue": float(repeats),
                    "workoutSteps": [
                        {
                            "type": "ExecutableStepDTO",
                            "stepOrder": 3,
                            "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
                            "description": f"Run {run_seconds}s Z{zone}",
                            "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                            "endConditionValue": float(run_seconds),
                            "targetType": {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"},
                            "zoneNumber": zone,
                        },
                        {
                            "type": "ExecutableStepDTO",
                            "stepOrder": 4,
                            "stepType": {"stepTypeId": 4, "stepTypeKey": "recovery"},
                            "description": f"Walk {walk_seconds}s Z{zone}",
                            "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                            "endConditionValue": float(walk_seconds),
                            "targetType": {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"},
                            "zoneNumber": zone,
                        },
                    ],
                },
                {
                    "type": "ExecutableStepDTO",
                    "stepOrder": 5,
                    "stepType": {"stepTypeId": 2, "stepTypeKey": "cooldown"},
                    "description": f"Cooldown {cooldown_min} min",
                    "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                    "endConditionValue": float(cooldown_min * 60),
                    "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
                },
            ],
        }],
    }


def build_z2_walk_json(
    name: str,
    duration_min: int,
    hr_min: int,
    hr_max: int,
) -> dict:
    """Build the Garmin Connect JSON for a steady Z2 walking workout with absolute HR range."""
    _require_minimum(1, duration_min=duration_min)
    return {
        "workoutName": name,
        "description": f"Walk {duration_min} min at Z2 ({hr_min}-{hr_max} bpm)",
        "sportType": {"sportTypeId": 12, "sportTypeKey": "walking"},
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": {"sportTypeId": 12, "sportTypeKey": "walking"},
            "workoutSteps": [
                {
                    "type": "ExecutableStepDTO",
                    "stepOrder": 1,
                    "stepType": {"stepTypeId": 1, "stepTypeKey": "warmup"},
                    "description": "Warmup 5 min",
                    "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                    "endConditionValue": 300.0,
                    "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
                },
                {
                    "type": "ExecutableStepDTO",
                    "stepOrder": 2,
                    "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
                    "description": f"Walk {duration_min} min Z2",
                    "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                    "endConditionValue": float(duration_min * 60),
                    "targetType": {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"},
                    "zoneNumber": 2,
                },
                {
                    "type": "ExecutableStepDTO",
                    "stepOrder": 3,
                    "stepType": {"stepTypeId": 2, "stepTypeKey": "cooldown"},
                    "description": "Cooldown 5 min",
                    "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                    "endConditionValue": 300.0,
                    "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
                },
            ],
        }],
    }


def build_strength_json(
    name: str,
    exercises: List[Dict[str, Any]],
) -> dict:
    """Build the Garmin Connect JSON for a strength workout.

    Each exercise becomes a reps-based work step. When "sets" > 1 the exercise is
    emitted as a RepeatGroupDTO iterated that many times (work + rest per set), so
    Garmin shows the real set count — previously "sets" only ever reached the
    description text and every exercise displayed as 1 set. The name is preserved
    in the step "description", which is what survives the round trip. It is also
    sent as "exerciseName", but Garmin only retains that when it matches one of
    its own exercise keys (e.g. "FARMERS_CARRY"); any other value is accepted and
    then stored as an empty string.

    "category" is optional and only emitted when the caller supplies one, uppercased
    and otherwise passed through untouched. Garmin validates it against its own enum
    and rejects anything outside it, including "UNASSIGNED" and "OTHER"; omitting the
    key is accepted. Valid values come from Garmin's published catalog:
    https://connect.garmin.com/web-data/exercises/Exercises.json
    """
    steps: List[dict] = []
    step_order = 1

    for index, ex in enumerate(exercises):
        ex_name = ex.get("name", "Exercise")
        sets = int(ex.get("sets", 1))
        reps = int(ex.get("reps", 1))
        rest_seconds = int(ex.get("rest_seconds", 60))
        try:
            _require_minimum(1, sets=sets, reps=reps)
            _require_minimum(0, rest_seconds=rest_seconds)
        except ValueError as e:
            raise ValueError(f"exercise {ex_name!r}: {e}") from e

        # Work step
        step = {
            "type": "ExecutableStepDTO",
            "stepOrder": step_order,
            "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
            "description": f"{ex_name}: {sets} sets x {reps} reps",
            "endCondition": {"conditionTypeId": 10, "conditionTypeKey": "reps"},
            "endConditionValue": float(reps),
            "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
            "exerciseName": ex_name,
        }

        # Only set when the caller asked for it: Garmin rejects values outside its own
        # enum, and an absent category is accepted, so an unknown one stays absent
        # rather than being guessed into a wrong record.
        category = ex.get("category")
        if category is not None:
            if not isinstance(category, str) or not category.strip():
                raise ValueError(
                    f"category for exercise {ex_name!r} must be a non-empty string"
                )
            step["category"] = category.strip().upper()

        if sets > 1:
            # N sets = a repeat group iterated N times (work + rest per iteration).
            # The explicit stepTypeId 6 / endCondition "iterations" shape matches
            # what _sanitize_repeat_group enforces on the upload path: the Garmin
            # API silently corrupts a RepeatGroupDTO without a valid iterations
            # endCondition.
            # Children continue the workout-wide stepOrder count after the
            # group, as Garmin numbers its own workouts. Restarting at 1 in
            # every group gave each multi-set exercise the same 1/2 orders.
            group_steps = [{**step, "stepOrder": step_order + 1}]
            if rest_seconds > 0:
                group_steps.append({
                    "type": "ExecutableStepDTO",
                    "stepOrder": step_order + 2,
                    "stepType": {"stepTypeId": 4, "stepTypeKey": "recovery"},
                    "description": f"Rest {rest_seconds}s",
                    "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                    "endConditionValue": float(rest_seconds),
                    "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
                })
            steps.append({
                "type": "RepeatGroupDTO",
                "stepOrder": step_order,
                "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
                "numberOfIterations": sets,
                "smartRepeat": False,
                "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations"},
                "endConditionValue": float(sets),
                "workoutSteps": group_steps,
            })
            step_order += 1 + len(group_steps)
            # The in-group rest after the final set already separates this exercise
            # from the next one — no extra inter-exercise rest step.
            continue

        steps.append(step)
        step_order += 1

        # Rest step (skip after last exercise). Compared by index, not by value:
        # two identical exercise dicts would otherwise make an earlier one look
        # like the last and lose its rest step.
        if rest_seconds > 0 and index != len(exercises) - 1:
            steps.append({
                "type": "ExecutableStepDTO",
                "stepOrder": step_order,
                "stepType": {"stepTypeId": 4, "stepTypeKey": "recovery"},
                "description": f"Rest {rest_seconds}s",
                "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                "endConditionValue": float(rest_seconds),
                "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
            })
            step_order += 1

    return {
        "workoutName": name,
        "description": f"Strength: {len(exercises)} exercises",
        "sportType": {"sportTypeId": 5, "sportTypeKey": "strength_training"},
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": {"sportTypeId": 5, "sportTypeKey": "strength_training"},
            "workoutSteps": steps,
        }],
    }


# =============================================================================
# MCP TOOLS
# =============================================================================


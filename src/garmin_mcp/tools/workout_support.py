"""Workout JSON validation and curation helpers.

Vendored from Taxuspt/garmin_mcp (MIT), src/garmin_mcp/workouts.py: the step-tree
normalization, the target/end-condition checks Garmin would otherwise silently
mis-save, and the curated summaries of workouts and scheduled workouts.
"""


END_CONDITION_TYPE_IDS = {
    "lap.button": 1,
    "time": 2,
    "distance": 3,
    "calories": 4,
    "power": 5,
    "heart.rate": 6,
    "iterations": 7,
    "fixed.rest": 8,
    "fixed.repetition": 9,
    "reps": 10,
    "training.peaks.tss": 11,
}
END_CONDITION_TYPE_KEYS = {
    condition_id: condition_key
    for condition_key, condition_id in END_CONDITION_TYPE_IDS.items()
}

# Verified from Garmin-created workouts and live upload/fetch probes. Unknown
# target type IDs are allowed so we do not block valid Garmin targets that are
# not in this partial mapping yet.
KNOWN_TARGET_TYPE_IDS = {
    1: frozenset(["no.target"]),
    2: frozenset(["power.zone"]),
    4: frozenset(["heart.rate.zone"]),
    # ID 6 is sport-context-dependent:
    #   - running / swimming: "pace.zone"
    #   - cycling: "power.between" (absolute watt range, uses targetValueOne/targetValueTwo)
    6: frozenset(["pace.zone", "power.between"]),
}

# Reverse map: workoutTargetTypeKey -> workoutTargetTypeId (each key maps to exactly one ID).
KNOWN_TARGET_TYPE_KEYS = {
    key: target_id
    for target_id, keys in KNOWN_TARGET_TYPE_IDS.items()
    for key in keys
}



_TARGET_FIELD_LAYOUTS = {
    'targetType': {
        'bounds': ('targetValueOne', 'targetValueTwo'),
        'zone': 'zoneNumber',
    },
    'secondaryTargetType': {
        'bounds': ('secondaryTargetValueOne', 'secondaryTargetValueTwo'),
        'zone': 'secondaryZoneNumber',
    },
}


def _iter_step_tree(step: dict, path: str):
    """Yield a workout step and every nested step with its request path."""
    if not isinstance(step, dict):
        raise ValueError(f"{path} must be an object")
    yield step, path
    nested_steps = step.get('workoutSteps', [])
    if not isinstance(nested_steps, list):
        raise ValueError(f"{path}.workoutSteps must be a list")
    for index, nested in enumerate(nested_steps):
        yield from _iter_step_tree(
            nested,
            f"{path}.workoutSteps[{index}]",
        )


def _iter_workout_steps(workout_data: dict):
    """Yield every workout step with a stable request path."""
    segments = workout_data.get('workoutSegments', [])
    if not isinstance(segments, list):
        raise ValueError("workoutSegments must be a list")
    for segment_index, segment in enumerate(segments):
        segment_path = f"workoutSegments[{segment_index}]"
        if not isinstance(segment, dict):
            raise ValueError(f"{segment_path} must be an object")
        steps = segment.get('workoutSteps', [])
        if not isinstance(steps, list):
            raise ValueError(f"{segment_path}.workoutSteps must be a list")
        for step_index, step in enumerate(steps):
            path = (
                f"workoutSegments[{segment_index}]"
                f".workoutSteps[{step_index}]"
            )
            yield from _iter_step_tree(step, path)


def _validate_nested_target_fields(step: dict, path: str) -> None:
    """Reject conflicting or ambiguous target fields before any repair."""
    for target_field, layout in _TARGET_FIELD_LAYOUTS.items():
        target_type = step.get(target_field)
        if not isinstance(target_type, dict):
            continue

        fields = (*layout['bounds'], layout['zone'])
        for field in fields:
            nested_value = target_type.get(field)
            if (
                nested_value is not None
                and step.get(field) is not None
                and step[field] != nested_value
            ):
                raise ValueError(
                    f"{path}.{field}={step[field]!r} conflicts with "
                    f"{path}.{target_field}.{field}={nested_value!r}; "
                    f"keep only the step-level {field}"
                )

        zone_field = layout['zone']
        zone_value = step.get(zone_field)
        if zone_value is None:
            zone_value = target_type.get(zone_field)

        bound_values = []
        for field in layout['bounds']:
            value = step.get(field)
            if value is None:
                value = target_type.get(field)
            if value is not None:
                bound_values.append((field, value))

        if zone_value is not None and bound_values:
            bounds = ", ".join(
                f"{field}={value!r}"
                for field, value in bound_values
            )
            raise ValueError(
                f"{path} mixes {zone_field}={zone_value!r} with custom "
                f"range fields ({bounds}); use either a named zone or a "
                f"custom range"
            )


def _move_nested_target_fields(step: dict) -> None:
    """Move target fields to the step level, where Garmin reads them."""
    for target_field, layout in _TARGET_FIELD_LAYOUTS.items():
        target_type = step.get(target_field)
        if not isinstance(target_type, dict):
            continue

        fields = (*layout['bounds'], layout['zone'])
        for field in fields:
            if field not in target_type:
                continue
            value = target_type.pop(field)
            if value is not None and step.get(field) is None:
                step[field] = value


def _fix_hr_zone_step(step: dict) -> None:
    """Fix a common mistake where HR zone targets use targetValueOne instead of zoneNumber.

    When targetType is heart.rate.zone and a named zone is intended, Garmin expects
    zoneNumber (1-5). If targetValueOne is set to a small integer (1-5) and zoneNumber
    is missing, this is almost certainly a zone number, not an absolute HR value.

    Custom HR bpm ranges (e.g. targetValueOne=105, targetValueTwo=143) are left
    unchanged — these are legitimate custom heart rate targets in Garmin Connect.
    """
    target_type = step.get('targetType')
    target_key = (
        target_type.get('workoutTargetTypeKey', '')
        if isinstance(target_type, dict)
        else ''
    )

    if target_key == 'heart.rate.zone' and step.get('zoneNumber') is None:
        zone = step.get('targetValueOne')
        try:
            zone = float(zone)
        except (TypeError, ValueError):
            zone = None
        if zone is not None and zone.is_integer() and 1 <= zone <= 5:
            step['zoneNumber'] = int(zone)
            step.pop('targetValueOne', None)
            step.pop('targetValueTwo', None)

    # Recurse into nested steps (RepeatGroupDTO)
    for nested in step.get('workoutSteps', []):
        _fix_hr_zone_step(nested)


def _fix_repeat_group_step(step: dict) -> None:
    """Ensure RepeatGroupDTO steps have a valid endCondition and numberOfIterations.

    The Garmin API silently corrupts a RepeatGroupDTO when conditionTypeId is
    missing from its endCondition — it falls back to an unrelated condition type
    (observed: "heart.rate") and drops numberOfIterations entirely.

    This function:
    - Adds conditionTypeId: 7 ("iterations") when conditionTypeKey is "iterations"
      but conditionTypeId is absent.
    - Backfills numberOfIterations from endConditionValue when the former is missing.
    - Recurses into nested workoutSteps so nested repeat groups are also fixed.
    """
    if step.get('type') != 'RepeatGroupDTO':
        for nested in step.get('workoutSteps', []):
            _fix_repeat_group_step(nested)
        return

    end_condition = step.get('endCondition')
    if isinstance(end_condition, dict):
        if (
            end_condition.get('conditionTypeKey') == 'iterations'
            and 'conditionTypeId' not in end_condition
        ):
            end_condition['conditionTypeId'] = 7

    if step.get('numberOfIterations') is None:
        value = step.get('endConditionValue')
        if value is not None:
            step['numberOfIterations'] = int(value)

    for nested in step.get('workoutSteps', []):
        _fix_repeat_group_step(nested)


def _normalize_workout_steps(workout_data: dict) -> None:
    """Repair recoverable step-shape mistakes before validation and upload."""
    steps = list(_iter_workout_steps(workout_data))

    # Preflight the complete workout so a later conflict cannot leave an
    # earlier step partially repaired.
    for step, path in steps:
        _validate_nested_target_fields(step, path)
    for step, _ in steps:
        _move_nested_target_fields(step)

    # These helpers recurse, so invoke them only for top-level steps.
    for segment in workout_data.get('workoutSegments', []):
        for step in segment.get('workoutSteps', []):
            _fix_hr_zone_step(step)
            _fix_repeat_group_step(step)


def _validate_end_condition_step(step: dict, path: str) -> None:
    """Reject endCondition id/key pairs Garmin would silently reinterpret."""
    end_condition = step.get('endCondition')
    if isinstance(end_condition, dict):
        condition_key = end_condition.get('conditionTypeKey')
        condition_id = end_condition.get('conditionTypeId')

        if isinstance(condition_id, str):
            try:
                condition_id = int(condition_id)
            except (TypeError, ValueError):
                raise ValueError(f"{path}.endCondition.conditionTypeId must be numeric")
            end_condition['conditionTypeId'] = condition_id

        expected_id = END_CONDITION_TYPE_IDS.get(condition_key)
        expected_key = END_CONDITION_TYPE_KEYS.get(condition_id)

        if expected_id is not None:
            if condition_id is None:
                raise ValueError(
                    f"{path}.endCondition conditionTypeKey '{condition_key}' "
                    f"requires conditionTypeId {expected_id}"
                )
            if condition_id != expected_id:
                actual = expected_key or "unknown"
                raise ValueError(
                    f"{path}.endCondition conditionTypeKey '{condition_key}' "
                    f"requires conditionTypeId {expected_id}, got {condition_id} "
                    f"({actual})"
                )
        elif expected_key is not None and condition_key is not None:
            raise ValueError(
                f"{path}.endCondition conditionTypeId {condition_id} "
                f"requires conditionTypeKey '{expected_key}', got '{condition_key}'"
            )

    for index, nested in enumerate(step.get('workoutSteps', [])):
        _validate_end_condition_step(nested, f"{path}.workoutSteps[{index}]")


def _validate_end_condition_steps(workout_data: dict) -> None:
    """Validate all workout step endCondition blocks before upload."""
    for segment_index, segment in enumerate(workout_data.get('workoutSegments', [])):
        for step_index, step in enumerate(segment.get('workoutSteps', [])):
            path = f"workoutSegments[{segment_index}].workoutSteps[{step_index}]"
            _validate_end_condition_step(step, path)


def _validate_target_type_block(step: dict, path: str, target_field: str) -> None:
    """Reject a target type id/key pair Garmin would silently reinterpret."""
    target_type = step.get(target_field)
    if isinstance(target_type, dict):
        target_key = target_type.get('workoutTargetTypeKey')
        target_id = target_type.get('workoutTargetTypeId')

        if target_id is not None:
            try:
                target_id = int(target_id)
            except (TypeError, ValueError):
                raise ValueError(f"{path}.{target_field}.workoutTargetTypeId must be numeric")
            target_type['workoutTargetTypeId'] = target_id

        valid_keys = KNOWN_TARGET_TYPE_IDS.get(target_id)
        if valid_keys is not None and target_key is not None and target_key not in valid_keys:
            if len(valid_keys) == 1:
                (only_key,) = valid_keys
                raise ValueError(
                    f"{path}.{target_field} mismatch: workoutTargetTypeId {target_id} is "
                    f"{only_key!r}, not {target_key!r}"
                )
            else:
                valid_list = ", ".join(sorted(repr(k) for k in valid_keys))
                raise ValueError(
                    f"{path}.{target_field} mismatch: workoutTargetTypeId {target_id} is "
                    f"one of ({valid_list}), not {target_key!r}"
                )

        expected_id = KNOWN_TARGET_TYPE_KEYS.get(target_key)
        if expected_id is not None and target_id is not None and target_id != expected_id:
            raise ValueError(
                f"{path}.{target_field} mismatch: workoutTargetTypeKey {target_key!r} "
                f"requires workoutTargetTypeId {expected_id}, not {target_id}"
            )


def _validate_target_type_step(step: dict, path: str) -> None:
    """Reject targetType id/key pairs Garmin would silently reinterpret."""
    _validate_target_type_block(step, path, 'targetType')
    _validate_target_type_block(step, path, 'secondaryTargetType')

    for index, nested in enumerate(step.get('workoutSteps', [])):
        _validate_target_type_step(nested, f"{path}.workoutSteps[{index}]")


def _validate_target_type_steps(workout_data: dict) -> None:
    """Walk all workout steps and validate known targetType id/key pairs."""
    for segment_index, segment in enumerate(workout_data.get('workoutSegments', [])):
        for step_index, step in enumerate(segment.get('workoutSteps', [])):
            path = f"workoutSegments[{segment_index}].workoutSteps[{step_index}]"
            _validate_target_type_step(step, path)


def _curate_workout_summary(workout: dict) -> dict:
    """Extract essential workout metadata for list views"""
    sport_type = workout.get('sportType') or {}

    summary = {
        "id": workout.get('workoutId'),
        "name": workout.get('workoutName'),
        "sport": sport_type.get('sportTypeKey'),
        "provider": workout.get('workoutProvider'),
        "created_date": workout.get('createdDate'),
        "updated_date": workout.get('updatedDate'),
    }

    # Add optional fields if present
    if workout.get('description'):
        summary['description'] = workout.get('description')

    duration = workout.get('estimatedDurationInSecs')
    if duration is None:
        duration = workout.get('estimatedDuration')
    if duration is not None:
        summary['estimated_duration_seconds'] = duration

    distance = workout.get('estimatedDistanceInMeters')
    if distance is None:
        distance = workout.get('estimatedDistance')
    if distance is not None:
        summary['estimated_distance_meters'] = distance

    # Remove None values
    return {k: v for k, v in summary.items() if v is not None}


# garminconnect's get_workouts returns a single page (start=0, limit=100 by
# default), so one call silently drops the rest of a larger library.
_WORKOUTS_PAGE_SIZE = 100


def _curate_step_target(
    curated: dict,
    step: dict,
    target_field: str,
    value_one_field: str,
    value_two_field: str,
    zone_field: str,
    unit_field: str,
    prefix: str = "",
) -> None:
    """Curate a workout target block, handling Garmin null target payloads safely."""
    target_type = step.get(target_field)
    if not isinstance(target_type, dict):
        target_type = {}
    target_key = target_type.get('workoutTargetTypeKey')

    if not target_key or target_key == 'no.target':
        return

    curated[f'{prefix}target_type'] = target_key

    if step.get(value_one_field) is not None:
        curated[f'{prefix}target_value_low'] = step.get(value_one_field)
    if step.get(value_two_field) is not None:
        curated[f'{prefix}target_value_high'] = step.get(value_two_field)
    if step.get(zone_field) is not None:
        curated[f'{prefix}target_zone'] = step.get(zone_field)
    # Without the unit, a power range in % FTP reads the same as one in watts
    unit = step.get(unit_field)
    if isinstance(unit, dict) and unit.get('unitKey'):
        curated[f'{prefix}target_value_unit'] = unit.get('unitKey')
    elif target_key == 'pace.zone' and (
        step.get(value_one_field) is not None or step.get(value_two_field) is not None
    ):
        curated[f'{prefix}target_value_unit'] = 'meters_per_second'


def _curate_workout_step(step: dict) -> dict:
    """Extract essential workout step information"""
    step_type = step.get('stepType') or {}
    end_condition = step.get('endCondition') or {}

    curated = {
        "order": step.get('stepOrder'),
        "type": step_type.get('stepTypeKey'),  # warmup, interval, cooldown, rest, recover
    }

    # Description
    if step.get('description'):
        curated['description'] = step.get('description')

    # End condition (duration/distance/lap press)
    if end_condition.get('conditionTypeKey'):
        curated['end_condition'] = end_condition.get('conditionTypeKey')
    if step.get('endConditionValue'):
        # Value meaning depends on condition type (seconds for time, meters for distance)
        curated['end_condition_value'] = step.get('endConditionValue')

    # Primary target (heart rate, pace, power, etc.)
    _curate_step_target(
        curated,
        step,
        target_field='targetType',
        value_one_field='targetValueOne',
        value_two_field='targetValueTwo',
        zone_field='zoneNumber',
        unit_field='targetValueUnit',
    )

    # Any sport can have one, e.g. swim pace or cadence alongside bike power.
    _curate_step_target(
        curated,
        step,
        target_field='secondaryTargetType',
        value_one_field='secondaryTargetValueOne',
        value_two_field='secondaryTargetValueTwo',
        zone_field='secondaryZoneNumber',
        unit_field='secondaryTargetValueUnit',
        prefix='secondary_',
    )
    # Swim stroke / equipment / drill info (Garmin returns these as nested dicts;
    # previously dropped entirely -- strokeType/equipmentType/drillType are real
    # fields Garmin provides for swim steps, unrelated to secondaryTargetValueOne).
    stroke_type = step.get('strokeType')
    if isinstance(stroke_type, dict) and stroke_type.get('strokeTypeKey'):
        curated['stroke_type'] = stroke_type.get('strokeTypeKey')
    equipment_type = step.get('equipmentType')
    if isinstance(equipment_type, dict) and equipment_type.get('equipmentTypeKey'):
        curated['equipment_type'] = equipment_type.get('equipmentTypeKey')
    drill_type = step.get('drillType')
    if isinstance(drill_type, dict) and drill_type.get('drillTypeKey'):
        curated['drill_type'] = drill_type.get('drillTypeKey')
    # Strength training exercise info
    if step.get('category'):
        curated['category'] = step.get('category')
    if step.get('exerciseName'):
        curated['exercise_name'] = step.get('exerciseName')
    if step.get('weightValue') is not None:
        curated['weight_value'] = step.get('weightValue')
        weight_unit = step.get('weightUnit', {})
        if weight_unit and weight_unit.get('unitKey'):
            curated['weight_unit'] = weight_unit.get('unitKey')

    # Repeat info for repeat steps
    if step.get('type') == 'RepeatGroupDTO':
        curated['repeat_count'] = step.get('numberOfIterations')
        nested_steps = step.get('workoutSteps', [])
        if nested_steps:
            curated['steps'] = [_curate_workout_step(s) for s in nested_steps]
            curated['step_count'] = len(nested_steps)

    return {k: v for k, v in curated.items() if v is not None}


def _curate_workout_segment(segment: dict) -> dict:
    """Extract essential segment information including workout steps"""
    sport_type = segment.get('sportType') or {}

    curated = {
        "order": segment.get('segmentOrder'),
        "sport": sport_type.get('sportTypeKey'),
    }

    # Estimated metrics
    if segment.get('estimatedDurationInSecs'):
        curated['estimated_duration_seconds'] = segment.get('estimatedDurationInSecs')
    if segment.get('estimatedDistanceInMeters'):
        curated['estimated_distance_meters'] = segment.get('estimatedDistanceInMeters')

    # Workout steps - the actual content of the segment
    steps = segment.get('workoutSteps', [])
    if steps:
        curated['steps'] = [_curate_workout_step(s) for s in steps]
        curated['step_count'] = len(steps)

    return {k: v for k, v in curated.items() if v is not None}


def _curate_workout_details(workout: dict) -> dict:
    """Extract detailed workout information with segments

    Handles both regular workouts (from get_workout_by_id) and training plan workouts
    (from fbt-adaptive endpoint) which use slightly different field names.
    """
    sport_type = workout.get('sportType') or {}

    details = {
        "id": workout.get('workoutId'),
        "uuid": workout.get('workoutUuid'),
        "name": workout.get('workoutName'),
        "sport": sport_type.get('sportTypeKey') if sport_type else None,
        "provider": workout.get('workoutProvider'),
        "created_date": workout.get('createdDate'),
        "updated_date": workout.get('updatedDate'),
    }

    # Optional fields
    if workout.get('description'):
        details['description'] = workout.get('description')

    # Handle both field name variants (regular vs training plan workouts)
    duration = workout.get('estimatedDuration') or workout.get('estimatedDurationInSecs')
    if duration:
        details['estimated_duration_seconds'] = duration

    distance = workout.get('estimatedDistance') or workout.get('estimatedDistanceInMeters')
    if distance:
        details['estimated_distance_meters'] = distance

    if workout.get('avgTrainingSpeed'):
        details['avg_training_speed_mps'] = workout.get('avgTrainingSpeed')

    if workout.get('poolLength'):
        details['pool_length'] = workout.get('poolLength')
        pool_unit = workout.get('poolLengthUnit')
        if isinstance(pool_unit, dict) and pool_unit.get('unitKey'):
            details['pool_length_unit'] = pool_unit.get('unitKey')

    # Training plan specific fields
    if workout.get('workoutPhrase'):
        details['workout_type'] = workout.get('workoutPhrase')

    if workout.get('trainingEffectLabel'):
        details['training_effect_label'] = workout.get('trainingEffectLabel')

    if workout.get('estimatedTrainingEffect'):
        details['estimated_training_effect'] = workout.get('estimatedTrainingEffect')

    # Curate segments with workout steps
    segments = workout.get('workoutSegments', [])
    if segments:
        details['segments'] = [_curate_workout_segment(seg) for seg in segments]
        details['segment_count'] = len(segments)

    # Remove None values
    return {k: v for k, v in details.items() if v is not None}


def _curate_scheduled_workout(scheduled: dict) -> dict:
    """Extract essential scheduled workout information from GraphQL response"""
    # GraphQL response has workout data at top level (not nested)
    # Completed is determined by presence of associatedActivityId
    is_completed = scheduled.get('associatedActivityId') is not None

    summary = {
        "date": scheduled.get('scheduleDate'),
        # Calendar-entry id (distinct from workout_id). Pass this to
        # unschedule_workout to remove the entry from the calendar.
        "scheduled_workout_id": scheduled.get('scheduledWorkoutId'),
        "workout_uuid": scheduled.get('workoutUuid'),
        "workout_id": scheduled.get('workoutId'),
        "training_plan_id": scheduled.get('trainingPlanId'),
        "fbt_adaptive_plan_id": scheduled.get('fbtAdaptivePlanId'),
        "tp_type": scheduled.get('tpType'),
        "name": scheduled.get('workoutName'),
        "sport": scheduled.get('workoutType'),
        "completed": is_completed,
    }

    # Training plan info
    if scheduled.get('tpPlanName'):
        summary['training_plan'] = scheduled.get('tpPlanName')

    # Workout type description (e.g., "AEROBIC_LOW_SHORTAGE_BASE", "ANAEROBIC_SPEED", "LONG_WORKOUT")
    # This describes the intent/type of the workout from Garmin Coach
    if scheduled.get('workoutPhrase'):
        summary['workout_type'] = scheduled.get('workoutPhrase')

    # Rest day and race day flags
    if scheduled.get('isRestDay'):
        summary['is_rest_day'] = True
    if scheduled.get('race'):
        summary['is_race_day'] = True

    # Optional fields
    if scheduled.get('estimatedDurationInSecs'):
        summary['estimated_duration_seconds'] = scheduled.get('estimatedDurationInSecs')

    if scheduled.get('estimatedDistanceInMeters'):
        summary['estimated_distance_meters'] = scheduled.get('estimatedDistanceInMeters')

    # If completed, include the activity ID
    if is_completed:
        summary['activity_id'] = scheduled.get('associatedActivityId')

    # Remove None values
    return {k: v for k, v in summary.items() if v is not None}



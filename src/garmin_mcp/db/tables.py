"""The typed layer for Garmin Connect's JSON payloads: every table, its columns, their
comments (with units), and how each column is read out of the raw JSON. DDL,
projections and the drift check are generated from these definitions.

Expressions are DuckDB SQL over the alias ``b`` (the JSON object being projected) plus
whatever context aliases the table's projection provides (``src`` ...). Units follow
Garmin Connect: meters, seconds, meters per second, degrees Celsius, watts, bpm, kcal.
Timestamps named *_gmt are UTC; *_local are wall-clock in the user's or activity's zone.
"""

from __future__ import annotations

from .tabledef import Column, Table, col, expr, fill_extra, local_ms, table_ddl

__all__ = ["TABLES", "table", "table_ddl", "Column", "Table"]

fetched_at = expr("fetched_at", "TIMESTAMPTZ", "When this row's source payload was fetched from Garmin.", "src.fetched_at")
cal_date = expr("calendar_date", "DATE", "Calendar day (Garmin's day in the user's time zone).", "src.calendar_date")
activity_id_ctx = expr("activity_id", "BIGINT", "Activity id.", "src.activity_id")


def ts(name: str, comment: str, key: str | None = None) -> Column:
    """Garmin's 'YYYY-MM-DDTHH:MM:SS.0' GMT strings -> TIMESTAMPTZ."""
    k = key or name
    return expr(name, "TIMESTAMPTZ", comment, f"TRY_CAST(left(b->>'{k}', 23) AS TIMESTAMP)::TIMESTAMPTZ", k)


def tsl(name: str, comment: str, key: str | None = None) -> Column:
    """Garmin's local wall-clock strings -> TIMESTAMP (no zone)."""
    k = key or name
    return expr(name, "TIMESTAMP", comment, f"TRY_CAST(left(b->>'{k}', 23) AS TIMESTAMP)", k)


def ms(name: str, comment: str, key: str | None = None) -> Column:
    """Epoch milliseconds (GMT) -> TIMESTAMPTZ."""
    k = key or name
    return expr(name, "TIMESTAMPTZ", comment, f"to_timestamp(TRY_CAST(b->>'{k}' AS BIGINT) / 1000.0)", k)


def nested(name: str, type_: str, comment: str, path: str, key: str) -> Column:
    """A scalar read from a nested path like 'activityType.typeKey' (key = top-level key)."""
    parts = path.split(".")
    sql = "b" + "".join(f"->'{p}'" for p in parts[:-1]) + f"->>'{parts[-1]}'"
    if type_ == "VARCHAR":
        return expr(name, type_, comment, sql, key)
    if type_ == "TIMESTAMPTZ":
        return expr(name, type_, comment, f"TRY_CAST(left({sql}, 23) AS TIMESTAMP)::TIMESTAMPTZ", key)
    if type_ == "TIMESTAMP":
        return expr(name, type_, comment, f"TRY_CAST(left({sql}, 23) AS TIMESTAMP)", key)
    return expr(name, type_, comment, f"TRY_CAST({sql} AS {type_})", key)


# ---------------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------------

# Columns shared by the activity list element and summaryDTO of the activity detail.
# Names are snake_case of Garmin's keys; the list element and summaryDTO sometimes use
# different keys for the same quantity (averageHR vs averageHR, avgPower vs averagePower).
_perf: list[tuple[str, str, str, str]] = [
    # (column, type, comment, list key)
    ("distance", "DOUBLE", "Distance in meters.", "distance"),
    ("duration", "DOUBLE", "Timer duration in seconds.", "duration"),
    ("elapsed_duration", "DOUBLE", "Elapsed duration in seconds.", "elapsedDuration"),
    ("moving_duration", "DOUBLE", "Moving duration in seconds.", "movingDuration"),
    ("elevation_gain", "DOUBLE", "Elevation gain in meters.", "elevationGain"),
    ("elevation_loss", "DOUBLE", "Elevation loss in meters.", "elevationLoss"),
    ("min_elevation", "DOUBLE", "Lowest elevation in meters.", "minElevation"),
    ("max_elevation", "DOUBLE", "Highest elevation in meters.", "maxElevation"),
    ("avg_elevation", "DOUBLE", "Average elevation in meters.", "avgElevation"),
    ("average_speed", "DOUBLE", "Average speed in m/s.", "averageSpeed"),
    ("max_speed", "DOUBLE", "Maximum speed in m/s.", "maxSpeed"),
    ("start_latitude", "DOUBLE", "Start latitude in degrees.", "startLatitude"),
    ("start_longitude", "DOUBLE", "Start longitude in degrees.", "startLongitude"),
    ("end_latitude", "DOUBLE", "End latitude in degrees.", "endLatitude"),
    ("end_longitude", "DOUBLE", "End longitude in degrees.", "endLongitude"),
    ("calories", "DOUBLE", "Calories in kcal.", "calories"),
    ("bmr_calories", "DOUBLE", "Basal metabolic calories during the activity in kcal.", "bmrCalories"),
    ("average_hr", "DOUBLE", "Average heart rate in bpm.", "averageHR"),
    ("max_hr", "DOUBLE", "Maximum heart rate in bpm.", "maxHR"),
    ("steps", "INTEGER", "Steps.", "steps"),
    ("aerobic_training_effect", "DOUBLE", "Aerobic training effect (0-5).", "aerobicTrainingEffect"),
    ("anaerobic_training_effect", "DOUBLE", "Anaerobic training effect (0-5).", "anaerobicTrainingEffect"),
    ("aerobic_training_effect_message", "VARCHAR", "Aerobic training effect label key.", "aerobicTrainingEffectMessage"),
    ("anaerobic_training_effect_message", "VARCHAR", "Anaerobic training effect label key.", "anaerobicTrainingEffectMessage"),
    ("training_effect_label", "VARCHAR", "Primary benefit label (e.g. AEROBIC_BASE, TEMPO, VO2MAX).", "trainingEffectLabel"),
    ("activity_training_load", "DOUBLE", "Training load (EPOC-based) of the activity.", "activityTrainingLoad"),
    ("vo2max_value", "DOUBLE", "VO2 max estimate attached to the activity in ml/kg/min.", "vO2MaxValue"),
    ("max_vertical_speed", "DOUBLE", "Maximum vertical speed in m/s.", "maxVerticalSpeed"),
    ("water_estimated", "DOUBLE", "Estimated sweat loss in ml.", "waterEstimated"),
    ("min_activity_lap_duration", "DOUBLE", "Shortest lap in seconds.", "minActivityLapDuration"),
    ("moderate_intensity_minutes", "INTEGER", "Moderate intensity minutes earned.", "moderateIntensityMinutes"),
    ("vigorous_intensity_minutes", "INTEGER", "Vigorous intensity minutes earned.", "vigorousIntensityMinutes"),
    ("difference_body_battery", "INTEGER", "Body Battery change during the activity.", "differenceBodyBattery"),
    ("min_temperature", "DOUBLE", "Minimum temperature in degrees Celsius.", "minTemperature"),
    ("max_temperature", "DOUBLE", "Maximum temperature in degrees Celsius.", "maxTemperature"),
    ("min_respiration_rate", "DOUBLE", "Minimum respiration rate in breaths/min.", "minRespirationRate"),
    ("max_respiration_rate", "DOUBLE", "Maximum respiration rate in breaths/min.", "maxRespirationRate"),
    ("avg_respiration_rate", "DOUBLE", "Average respiration rate in breaths/min.", "avgRespirationRate"),
    ("avg_grade_adjusted_speed", "DOUBLE", "Grade-adjusted average speed in m/s (runs).", "avgGradeAdjustedSpeed"),
    ("training_stress_score", "DOUBLE", "Training Stress Score (power).", "trainingStressScore"),
    ("intensity_factor", "DOUBLE", "Intensity factor (power).", "intensityFactor"),
    ("standing_time", "DOUBLE", "Time standing in seconds (cycling dynamics).", "standingTime"),
    ("seated_time", "DOUBLE", "Time seated in seconds (cycling dynamics).", "seatedTime"),
    ("max_standing_power", "DOUBLE", "Max power while standing in watts.", "maxStandingPower"),
    ("max_seated_power", "DOUBLE", "Max power while seated in watts.", "maxSeatedPower"),
    ("begin_potential_stamina", "DOUBLE", "Potential stamina at start (%).", "beginPotentialStamina"),
    ("end_potential_stamina", "DOUBLE", "Potential stamina at end (%).", "endPotentialStamina"),
    ("min_available_stamina", "DOUBLE", "Lowest available stamina (%).", "minAvailableStamina"),
    ("gain_solar_activity_time", "DOUBLE", "Solar gain time in seconds (solar devices).", "gainSolarActivityTime"),
    ("avg_solar_charge_percent", "DOUBLE", "Average solar charge percent.", "avgSolarChargePercent"),
    ("surface_type_unpaved_percentage", "DOUBLE", "Share of distance on unpaved surface (%).", "surfaceTypeUnpavedPercentage"),
]

# Keys that differ between the list element (search) and summaryDTO (detail).
_list_only: list[tuple[str, str, str, str]] = [
    ("average_running_cadence", "DOUBLE", "Average running cadence in steps/min.", "averageRunningCadenceInStepsPerMinute"),
    ("max_running_cadence", "DOUBLE", "Maximum running cadence in steps/min.", "maxRunningCadenceInStepsPerMinute"),
    ("average_biking_cadence", "DOUBLE", "Average cycling cadence in rpm.", "averageBikingCadenceInRevPerMinute"),
    ("max_biking_cadence", "DOUBLE", "Maximum cycling cadence in rpm.", "maxBikingCadenceInRevPerMinute"),
    ("avg_stride_length", "DOUBLE", "Average stride length in cm.", "avgStrideLength"),
    ("avg_vertical_oscillation", "DOUBLE", "Average vertical oscillation in cm.", "avgVerticalOscillation"),
    ("avg_ground_contact_time", "DOUBLE", "Average ground contact time in ms.", "avgGroundContactTime"),
    ("avg_vertical_ratio", "DOUBLE", "Average vertical ratio in %.", "avgVerticalRatio"),
    ("avg_ground_contact_balance", "DOUBLE", "Average ground contact balance (% left).", "avgGroundContactBalance"),
    ("avg_power", "DOUBLE", "Average power in watts.", "avgPower"),
    ("max_power", "DOUBLE", "Maximum power in watts.", "maxPower"),
    ("norm_power", "DOUBLE", "Normalized power in watts.", "normPower"),
    ("max_20min_power", "DOUBLE", "Best 20-minute power in watts.", "max20MinPower"),
    ("avg_left_balance", "DOUBLE", "Average left/right balance (% left).", "avgLeftBalance"),
    ("avg_standing_power", "DOUBLE", "Average power standing in watts.", "avgStandingPower"),
    ("avg_seated_power", "DOUBLE", "Average power seated in watts.", "avgSeatedPower"),
    ("strokes", "DOUBLE", "Total strokes (cycling: pedal strokes; swimming: strokes).", "strokes"),
    ("max_double_cadence", "DOUBLE", "Maximum double cadence (both feet) in steps/min.", "maxDoubleCadence"),
]

_detail_summary_only: list[tuple[str, str, str, str]] = [
    ("average_run_cadence", "DOUBLE", "Average running cadence in steps/min.", "averageRunCadence"),
    ("max_run_cadence", "DOUBLE", "Maximum running cadence in steps/min.", "maxRunCadence"),
    ("average_bike_cadence", "DOUBLE", "Average cycling cadence in rpm.", "averageBikeCadence"),
    ("max_bike_cadence", "DOUBLE", "Maximum cycling cadence in rpm.", "maxBikeCadence"),
    ("min_hr", "DOUBLE", "Minimum heart rate in bpm.", "minHR"),
    ("average_moving_speed", "DOUBLE", "Average moving speed in m/s.", "averageMovingSpeed"),
    ("average_temperature", "DOUBLE", "Average temperature in degrees Celsius.", "averageTemperature"),
    ("ground_contact_time", "DOUBLE", "Average ground contact time in ms.", "groundContactTime"),
    ("stride_length", "DOUBLE", "Average stride length in cm.", "strideLength"),
    ("vertical_oscillation", "DOUBLE", "Average vertical oscillation in cm.", "verticalOscillation"),
    ("vertical_ratio", "DOUBLE", "Average vertical ratio in %.", "verticalRatio"),
    ("training_effect", "DOUBLE", "Aerobic training effect (0-5).", "trainingEffect"),
    ("average_power", "DOUBLE", "Average power in watts.", "averagePower"),
    ("max_power_twenty_minutes", "DOUBLE", "Best 20-minute power in watts.", "maxPowerTwentyMinutes"),
    ("min_power", "DOUBLE", "Minimum power in watts.", "minPower"),
    ("normalized_power", "DOUBLE", "Normalized power in watts.", "normalizedPower"),
    ("functional_threshold_power", "DOUBLE", "FTP used for the activity in watts.", "functionalThresholdPower"),
    ("total_work", "DOUBLE", "Work in kJ.", "totalWork"),
    ("left_balance", "DOUBLE", "Left pedal balance in %.", "leftBalance"),
    ("right_balance", "DOUBLE", "Right pedal balance in %.", "rightBalance"),
    ("total_number_of_strokes", "INTEGER", "Total strokes.", "totalNumberOfStrokes"),
    ("average_seated_power", "DOUBLE", "Average power seated in watts.", "averageSeatedPower"),
    ("average_standing_power", "DOUBLE", "Average power standing in watts.", "averageStandingPower"),
    ("max_seated_cadence", "DOUBLE", "Max cadence seated in rpm.", "maxSeatedCadence"),
    ("max_standing_cadence", "DOUBLE", "Max cadence standing in rpm.", "maxStandingCadence"),
    ("average_seated_cadence", "DOUBLE", "Average cadence seated in rpm.", "averageSeatedCadence"),
    ("average_standing_cadence", "DOUBLE", "Average cadence standing in rpm.", "averageStandingCadence"),
]

_power_phase_keys = [
    f"{side}{part}"
    for side in ("left", "right")
    for part in ("PowerPhaseStart", "PowerPhaseEnd", "PowerPhaseArcLength", "PowerPhaseArcCenter", "PowerPhasePeakStart", "PowerPhasePeakEnd", "PowerPhasePeakArcLength", "PowerPhasePeakArcCenter", "PlatformCenterOffset")
]


def extra_col(comment: str, _key: None = None) -> Column:
    """Placeholder filled by _fill_extra: the JSON object minus every key the table consumes."""
    return Column("extra", "JSON", comment, "__EXTRA__", None)


def nested_ms(name: str, comment: str, path: str, key: str) -> Column:
    parts = path.split(".")
    sql = "b" + "".join(f"->'{p}'" for p in parts[:-1]) + f"->>'{parts[-1]}'"
    return expr(name, "TIMESTAMPTZ", comment, f"to_timestamp(TRY_CAST({sql} AS BIGINT) / 1000.0)", key)


def _cols(spec: list[tuple[str, str, str, str]], obj: str = "b", key_prefix: str | None = None) -> list[Column]:
    out = []
    for name, type_, comment, key in spec:
        if obj == "b":
            out.append(col(name, type_, comment, key))
        else:
            # Read from a nested object (summaryDTO) of the detail payload; drift key is the top-level one.
            sql = f"TRY_CAST({obj}->>'{key}' AS {type_})" if type_ != "VARCHAR" else f"{obj}->>'{key}'"
            out.append(expr(name, type_, comment, sql, key))
    return out


activity_table = Table(
    name="activity",
    comment=(
        "One row per Garmin Connect activity. Summary columns come from the activity list (search) and are "
        "overwritten by the activity detail once fetched; detail-only columns are null until then. "
        "Sport-specific metrics are null when not recorded. Deleted activities keep deleted_at set."
    ),
    primary_key=["activity_id"],
    columns=[
        col("activity_id", "BIGINT", "Garmin activity id.", "activityId"),
        col("activity_name", "VARCHAR", "Activity title.", "activityName"),
        nested("activity_type", "VARCHAR", "Activity type key (running, cycling, strength_training, ...).", "activityType.typeKey", "activityType"),
        nested("activity_type_id", "INTEGER", "Activity type id.", "activityType.typeId", "activityType"),
        nested("parent_type_id", "INTEGER", "Parent activity type id.", "activityType.parentTypeId", "activityType"),
        nested("event_type", "VARCHAR", "Event type key (uncategorized, race, training, ...).", "eventType.typeKey", "eventType"),
        tsl("start_time_local", "Start, local wall clock.", "startTimeLocal"),
        ts("start_time_gmt", "Start (UTC).", "startTimeGMT"),
        ts("end_time_gmt", "End (UTC).", "endTimeGMT"),
        ms("begin_timestamp", "Start as epoch (UTC).", "beginTimestamp"),
        col("time_zone_id", "INTEGER", "Garmin time zone unit id.", "timeZoneId"),
        col("sport_type_id", "INTEGER", "Sport type id.", "sportTypeId"),
        col("device_id", "BIGINT", "Recording device id.", "deviceId"),
        col("manufacturer", "VARCHAR", "Device manufacturer.", "manufacturer"),
        col("location_name", "VARCHAR", "Location name.", "locationName"),
        col("owner_id", "BIGINT", "Owner user profile id.", "ownerId"),
        col("owner_display_name", "VARCHAR", "Owner display name.", "ownerDisplayName"),
        col("owner_full_name", "VARCHAR", "Owner full name.", "ownerFullName"),
        col("course_id", "BIGINT", "Course followed, if any.", "courseId"),
        col("lap_count", "INTEGER", "Number of laps.", "lapCount"),
        col("is_favorite", "BOOLEAN", "Favorite flag.", "isFavorite"),
        col("is_pr", "BOOLEAN", "Contains a personal record.", "isPR"),
        col("is_parent", "BOOLEAN", "Multisport parent.", "isParent"),
        col("is_manual_activity", "BOOLEAN", "Entered manually.", "isManualActivity"),
        col("is_auto_calc_calories", "BOOLEAN", "Calories auto-calculated.", "isAutoCalcCalories"),
        col("is_elevation_corrected", "BOOLEAN", "Elevation corrected by Garmin.", "isElevationCorrected"),
        col("is_purposeful", "BOOLEAN", "Purposeful activity flag.", "isPurposeful"),
        col("is_atp_activity", "BOOLEAN", "Belongs to an adaptive training plan.", "isAtpActivity"),
        col("has_polyline", "BOOLEAN", "Has GPS track.", "hasPolyline"),
        col("has_images", "BOOLEAN", "Has photos.", "hasImages"),
        col("has_video", "BOOLEAN", "Has video.", "hasVideo"),
        col("has_splits", "BOOLEAN", "Has splits.", "hasSplits"),
        col("has_intensity_intervals", "BOOLEAN", "Has intensity intervals.", "hasIntensityIntervals"),
        col("has_heat_map", "BOOLEAN", "Has heat map.", "hasHeatMap"),
        nested("privacy", "VARCHAR", "Privacy rule key (private, public, subscribers).", "privacy.typeKey", "privacy"),
        *_cols(_perf),
        *_cols(_list_only),
        col("hr_time_in_zone_1", "DOUBLE", "Seconds in HR zone 1.", "hrTimeInZone_1"),
        col("hr_time_in_zone_2", "DOUBLE", "Seconds in HR zone 2.", "hrTimeInZone_2"),
        col("hr_time_in_zone_3", "DOUBLE", "Seconds in HR zone 3.", "hrTimeInZone_3"),
        col("hr_time_in_zone_4", "DOUBLE", "Seconds in HR zone 4.", "hrTimeInZone_4"),
        col("hr_time_in_zone_5", "DOUBLE", "Seconds in HR zone 5.", "hrTimeInZone_5"),
        *[col(f"power_time_in_zone_{i}", "DOUBLE", f"Seconds in power zone {i}.", f"powerTimeInZone_{i}") for i in range(1, 8)],
        *[col(f"fastest_split_{d}", "DOUBLE", f"Fastest {d} m split in seconds.", f"fastestSplit_{d}") for d in (1000, 1609, 5000, 10000, 21098, 40000)],
        *[col(f"max_avg_power_{s}", "INTEGER", f"Best {s} s average power in watts.", f"maxAvgPower_{s}") for s in (1, 2, 5, 10, 20, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200)],
        col("exclude_from_power_curve_reports", "BOOLEAN", "Excluded from power curve.", "excludeFromPowerCurveReports"),
        col("is_deco_dive", "BOOLEAN", "Decompression dive.", "isDecoDive"),
        col("qualifying_dive", "BOOLEAN", "Qualifying dive.", "qualifyingDive"),
        col("summarized_dive_info", "JSON", "Dive summary (gases).", "summarizedDiveInfo"),
        col("user_roles", "JSON", "Owner roles.", "userRoles"),
        col("user_pro", "BOOLEAN", "Owner is Garmin Pro.", "userPro"),
        col("owner_profile_image_url_small", "VARCHAR", "Owner avatar (small).", "ownerProfileImageUrlSmall"),
        col("owner_profile_image_url_medium", "VARCHAR", "Owner avatar (medium).", "ownerProfileImageUrlMedium"),
        col("owner_profile_image_url_large", "VARCHAR", "Owner avatar (large).", "ownerProfileImageUrlLarge"),
        col("activity_uuid", "VARCHAR", "Activity UUID.", "activityUUID"),
        col("parent", "BOOLEAN", "Alias of isParent.", "parent"),
        col("pr", "BOOLEAN", "Alias of isPR.", "pr"),
        col("favorite", "BOOLEAN", "Alias of isFavorite.", "favorite"),
        col("atp_activity", "BOOLEAN", "Alias of isAtpActivity.", "atpActivity"),
        col("deco_dive", "BOOLEAN", "Alias of isDecoDive.", "decoDive"),
        col("manual_activity", "BOOLEAN", "Alias of isManualActivity.", "manualActivity"),
        col("elevation_corrected", "BOOLEAN", "Alias of isElevationCorrected.", "elevationCorrected"),
        col("purposeful", "BOOLEAN", "Alias of isPurposeful.", "purposeful"),
        col("auto_calc_calories", "BOOLEAN", "Alias of isAutoCalcCalories.", "autoCalcCalories"),
        col("split_summaries_list", "JSON", "Split summaries as embedded in the list element (usually empty; see activity_split_summary).", "splitSummaries"),
        # detail-only (activity payload), read from d
        expr("description", "VARCHAR", "Description (detail).", "d->>'description'", None),
        expr("user_profile_id", "BIGINT", "Owner user profile id (detail).", "TRY_CAST(d->>'userProfileId' AS BIGINT)", None),
        expr("is_multi_sport_parent", "BOOLEAN", "Multisport parent (detail).", "TRY_CAST(d->>'isMultiSportParent' AS BOOLEAN)", None),
        expr("access_control_rule", "VARCHAR", "Access control rule key (detail).", "d->'accessControlRuleDTO'->>'typeKey'", None),
        expr("time_zone", "VARCHAR", "IANA time zone of the activity (detail).", "d->'timeZoneUnitDTO'->>'timeZone'", None),
        expr("metadata", "JSON", "metadataDTO of the detail: device, sensors, upload dates, images, child activities, flags.", "d->'metadataDTO'", None),
        expr("upload_date", "TIMESTAMPTZ", "When the activity was uploaded (detail).", "TRY_CAST(left(d->'metadataDTO'->>'uploadedDate', 23) AS TIMESTAMP)::TIMESTAMPTZ", None),
        expr("last_update_date", "TIMESTAMPTZ", "Last edit on Garmin Connect (detail).", "TRY_CAST(left(d->'metadataDTO'->>'lastUpdateDate', 23) AS TIMESTAMP)::TIMESTAMPTZ", None),
        expr("associated_course_id", "BIGINT", "Course associated with the activity (detail).", "TRY_CAST(d->'metadataDTO'->>'associatedCourseId' AS BIGINT)", None),
        expr("associated_workout_id", "BIGINT", "Workout associated with the activity (detail).", "TRY_CAST(d->'metadataDTO'->>'associatedWorkoutId' AS BIGINT)", None),
        expr("connect_iq_measurements", "JSON", "Connect IQ developer field summary values (detail).", "d->'connectIQMeasurements'", None),
        *_cols(_detail_summary_only, "d->'summaryDTO'", None),
        *[
            expr(
                "".join("_" + c.lower() if c.isupper() else c for c in k),
                "DOUBLE",
                f"Cycling dynamics {k} (degrees or mm) (detail).",
                f"TRY_CAST(d->'summaryDTO'->>'{k}' AS DOUBLE)",
                k,
            )
            for k in _power_phase_keys
        ],
        expr("summary_fetched_at", "TIMESTAMPTZ", "When the newest list (summary) payload was fetched.", "src.summary_fetched_at"),
        expr("detail_fetched_at", "TIMESTAMPTZ", "When the activity detail was fetched; null if never.", "src.detail_fetched_at"),
        expr("deleted_at", "TIMESTAMPTZ", "When the activity was found deleted on Garmin; null while it exists.", "src.deleted_at"),
    ],
)

_lap_common: list[tuple[str, str, str, str]] = [
    ("start_latitude", "DOUBLE", "Start latitude.", "startLatitude"),
    ("start_longitude", "DOUBLE", "Start longitude.", "startLongitude"),
    ("end_latitude", "DOUBLE", "End latitude.", "endLatitude"),
    ("end_longitude", "DOUBLE", "End longitude.", "endLongitude"),
    ("distance", "DOUBLE", "Distance in meters.", "distance"),
    ("duration", "DOUBLE", "Timer duration in seconds.", "duration"),
    ("moving_duration", "DOUBLE", "Moving duration in seconds.", "movingDuration"),
    ("elapsed_duration", "DOUBLE", "Elapsed duration in seconds.", "elapsedDuration"),
    ("elevation_gain", "DOUBLE", "Elevation gain in meters.", "elevationGain"),
    ("elevation_loss", "DOUBLE", "Elevation loss in meters.", "elevationLoss"),
    ("max_elevation", "DOUBLE", "Highest elevation in meters.", "maxElevation"),
    ("min_elevation", "DOUBLE", "Lowest elevation in meters.", "minElevation"),
    ("average_speed", "DOUBLE", "Average speed in m/s.", "averageSpeed"),
    ("average_moving_speed", "DOUBLE", "Average moving speed in m/s.", "averageMovingSpeed"),
    ("max_speed", "DOUBLE", "Max speed in m/s.", "maxSpeed"),
    ("calories", "DOUBLE", "Calories in kcal.", "calories"),
    ("bmr_calories", "DOUBLE", "BMR calories in kcal.", "bmrCalories"),
    ("average_hr", "DOUBLE", "Average heart rate in bpm.", "averageHR"),
    ("max_hr", "DOUBLE", "Max heart rate in bpm.", "maxHR"),
    ("min_hr", "DOUBLE", "Min heart rate in bpm.", "minHR"),
    ("average_run_cadence", "DOUBLE", "Average run cadence in steps/min.", "averageRunCadence"),
    ("max_run_cadence", "DOUBLE", "Max run cadence in steps/min.", "maxRunCadence"),
    ("average_bike_cadence", "DOUBLE", "Average bike cadence in rpm.", "averageBikeCadence"),
    ("max_bike_cadence", "DOUBLE", "Max bike cadence in rpm.", "maxBikeCadence"),
    ("average_temperature", "DOUBLE", "Average temperature in degrees Celsius.", "averageTemperature"),
    ("max_temperature", "DOUBLE", "Max temperature in degrees Celsius.", "maxTemperature"),
    ("min_temperature", "DOUBLE", "Min temperature in degrees Celsius.", "minTemperature"),
    ("average_power", "DOUBLE", "Average power in watts.", "averagePower"),
    ("max_power", "DOUBLE", "Max power in watts.", "maxPower"),
    ("min_power", "DOUBLE", "Min power in watts.", "minPower"),
    ("normalized_power", "DOUBLE", "Normalized power in watts.", "normalizedPower"),
    ("total_work", "DOUBLE", "Work in kJ.", "totalWork"),
    ("left_balance", "DOUBLE", "Left balance in %.", "leftBalance"),
    ("right_balance", "DOUBLE", "Right balance in %.", "rightBalance"),
    ("total_number_of_strokes", "INTEGER", "Strokes.", "totalNumberOfStrokes"),
    ("standing_time", "DOUBLE", "Standing time in seconds.", "standingTime"),
    ("max_seated_power", "DOUBLE", "Max seated power in watts.", "maxSeatedPower"),
    ("max_standing_power", "DOUBLE", "Max standing power in watts.", "maxStandingPower"),
    ("average_seated_power", "DOUBLE", "Average seated power in watts.", "averageSeatedPower"),
    ("average_standing_power", "DOUBLE", "Average standing power in watts.", "averageStandingPower"),
    ("max_seated_cadence", "DOUBLE", "Max seated cadence in rpm.", "maxSeatedCadence"),
    ("max_standing_cadence", "DOUBLE", "Max standing cadence in rpm.", "maxStandingCadence"),
    ("average_seated_cadence", "DOUBLE", "Average seated cadence in rpm.", "averageSeatedCadence"),
    ("average_standing_cadence", "DOUBLE", "Average standing cadence in rpm.", "averageStandingCadence"),
    ("ground_contact_time", "DOUBLE", "Ground contact time in ms.", "groundContactTime"),
    ("stride_length", "DOUBLE", "Stride length in cm.", "strideLength"),
    ("vertical_oscillation", "DOUBLE", "Vertical oscillation in cm.", "verticalOscillation"),
    ("vertical_ratio", "DOUBLE", "Vertical ratio in %.", "verticalRatio"),
    ("avg_step_frequency", "DOUBLE", "Average step frequency in steps/min.", "avgStepFrequency"),
    ("avg_step_length", "DOUBLE", "Average step length in cm.", "avgStepLength"),
    ("max_vertical_speed", "DOUBLE", "Max vertical speed in m/s.", "maxVerticalSpeed"),
    ("avg_vertical_speed", "DOUBLE", "Average vertical speed in m/s.", "avgVerticalSpeed"),
    ("avg_elapsed_duration_vertical_speed", "DOUBLE", "Average vertical speed over elapsed time in m/s.", "avgElapsedDurationVerticalSpeed"),
    ("avg_grade_adjusted_speed", "DOUBLE", "Grade-adjusted speed in m/s.", "avgGradeAdjustedSpeed"),
    ("max_respiration_rate", "DOUBLE", "Max respiration in breaths/min.", "maxRespirationRate"),
    ("avg_respiration_rate", "DOUBLE", "Average respiration in breaths/min.", "avgRespirationRate"),
    ("surface_type_unpaved_percentage", "DOUBLE", "Unpaved share in %.", "surfaceTypeUnpavedPercentage"),
    ("total_exercise_reps", "INTEGER", "Exercise reps (strength).", "totalExerciseReps"),
    ("avg_force", "DOUBLE", "Average force in N.", "avgForce"),
    ("max_force", "DOUBLE", "Max force in N.", "maxForce"),
    ("normalized_force", "DOUBLE", "Normalized force in N.", "normalizedForce"),
    ("start_elevation", "DOUBLE", "Elevation at start in meters.", "startElevation"),
    ("max_grade", "DOUBLE", "Max grade in %.", "maxGrade"),
    ("average_grade", "DOUBLE", "Average grade in %.", "averageGrade"),
    ("min_cadence", "DOUBLE", "Min cadence.", "minCadence"),
    ("climb_pro_difficulty", "VARCHAR", "ClimbPro difficulty.", "climbProDifficulty"),
    ("seated_time", "DOUBLE", "Seated time in seconds.", "seatedTime"),
    ("standing_count", "INTEGER", "Number of stands.", "standingCount"),
    ("rider_position", "VARCHAR", "Rider position (SEATED, STANDING).", "riderPosition"),
    ("avg_descent_speed", "DOUBLE", "Average descent speed in m/s.", "avgDescentSpeed"),
    ("parent_index", "INTEGER", "Index of the parent split.", "parentIndex"),
]

_power_phase_cols = [
    expr("".join("_" + c.lower() if c.isupper() else c for c in k), "DOUBLE", f"Cycling dynamics {k}.", f"TRY_CAST(b->>'{k}' AS DOUBLE)", k)
    for k in _power_phase_keys
]

activity_lap_table = Table(
    name="activity_lap",
    comment="Laps of each activity (GET activity/{id}/splits, lapDTOs). Sport-specific columns are null when not recorded.",
    primary_key=["activity_id", "lap_index"],
    columns=[
        activity_id_ctx,
        col("lap_index", "INTEGER", "1-based lap number.", "lapIndex"),
        col("message_index", "INTEGER", "FIT message index.", "messageIndex"),
        ts("start_time_gmt", "Lap start (UTC).", "startTimeGMT"),
        col("intensity_type", "VARCHAR", "Intensity type (ACTIVE, REST, WARMUP, COOLDOWN, ...).", "intensityType"),
        *_cols(_lap_common),
        *_power_phase_cols,
        col("length_dtos", "JSON", "Pool lengths (swimming).", "lengthDTOs"),
        col("connect_iq_measurement", "JSON", "Connect IQ developer fields of the lap.", "connectIQMeasurement"),
        fetched_at,
    ],
)

activity_lap_event_table = Table(
    name="activity_lap_event",
    comment="Timer and section events of each activity (GET activity/{id}/splits, eventDTOs).",
    primary_key=["activity_id", "idx"],
    columns=[
        activity_id_ctx,
        expr("idx", "INTEGER", "Position in the event list.", "src.idx"),
        ts("start_time_gmt", "Event time (UTC).", "startTimeGMT"),
        col("start_time_gmt_double", "DOUBLE", "Event time as epoch ms.", "startTimeGMTDoubleValue"),
        nested("section_type", "VARCHAR", "Section type key (start, stop, lap, ...).", "sectionTypeDTO.key", "sectionTypeDTO"),
        nested("section_type_id", "INTEGER", "Section type id.", "sectionTypeDTO.id", "sectionTypeDTO"),
        nested("section_type_key", "VARCHAR", "Section type key (upper case).", "sectionTypeDTO.sectionTypeKey", "sectionTypeDTO"),
        col("rider_position", "INTEGER", "Rider position for riderPosition events (0 seated, 1 standing).", "riderPosition"),
        fetched_at,
    ],
)

activity_typed_split_table = Table(
    name="activity_typed_split",
    comment="Typed splits (intervals, climbs, run/walk, ...) of each activity (GET activity/{id}/typedsplits).",
    primary_key=["activity_id", "idx"],
    columns=[
        activity_id_ctx,
        expr("idx", "INTEGER", "Position in the split list.", "src.idx"),
        col("type", "VARCHAR", "Split type (INTERVAL_ACTIVE, INTERVAL_RECOVERY, CLIMB_ACTIVE, RWD_RUN, RWD_WALK, ...).", "type"),
        col("message_index", "INTEGER", "FIT message index.", "messageIndex"),
        tsl("start_time_local", "Start, local wall clock.", "startTimeLocal"),
        ts("start_time_gmt", "Start (UTC).", "startTimeGMT"),
        ts("end_time_gmt", "End (UTC).", "endTimeGMT"),
        col("lap_indexes", "INTEGER[]", "Laps covered by the split.", "lapIndexes"),
        *_cols(_lap_common),
        *_power_phase_cols,
        fetched_at,
    ],
)

activity_split_summary_table = Table(
    name="activity_split_summary",
    comment="Per split-type summaries of each activity (GET activity/{id}/split_summaries).",
    primary_key=["activity_id", "split_type"],
    columns=[
        activity_id_ctx,
        col("split_type", "VARCHAR", "Split type summarized.", "splitType"),
        col("no_of_splits", "INTEGER", "Number of splits of this type.", "noOfSplits"),
        col("max_elevation_gain", "DOUBLE", "Max elevation gain of a split in meters.", "maxElevationGain"),
        col("average_elevation_gain", "DOUBLE", "Average elevation gain in meters.", "averageElevationGain"),
        col("max_distance", "INTEGER", "Longest split in meters.", "maxDistance"),
        col("max_distance_with_precision", "DOUBLE", "Longest split in meters (precise).", "maxDistanceWithPrecision"),
        *_cols(_lap_common),
        *_power_phase_cols,
        fetched_at,
    ],
)

activity_weather_table = Table(
    name="activity_weather",
    comment="Weather at the activity start (GET activity/{id}/weather).",
    primary_key=["activity_id"],
    columns=[
        activity_id_ctx,
        ts("issue_date", "Weather observation time (UTC).", "issueDate"),
        col("temp", "INTEGER", "Temperature in degrees Fahrenheit (Garmin returns F here).", "temp"),
        col("apparent_temp", "INTEGER", "Apparent temperature in degrees Fahrenheit.", "apparentTemp"),
        col("dew_point", "INTEGER", "Dew point in degrees Fahrenheit.", "dewPoint"),
        col("relative_humidity", "INTEGER", "Relative humidity in %.", "relativeHumidity"),
        col("wind_direction", "INTEGER", "Wind direction in degrees.", "windDirection"),
        col("wind_direction_compass_point", "VARCHAR", "Wind direction compass point.", "windDirectionCompassPoint"),
        col("wind_speed", "INTEGER", "Wind speed in mph.", "windSpeed"),
        col("wind_gust", "INTEGER", "Wind gust in mph.", "windGust"),
        col("latitude", "DOUBLE", "Station latitude.", "latitude"),
        col("longitude", "DOUBLE", "Station longitude.", "longitude"),
        nested("weather_station_id", "VARCHAR", "Weather station id.", "weatherStationDTO.id", "weatherStationDTO"),
        nested("weather_station_name", "VARCHAR", "Weather station name.", "weatherStationDTO.name", "weatherStationDTO"),
        nested("weather_type", "VARCHAR", "Weather description.", "weatherTypeDTO.desc", "weatherTypeDTO"),
        fetched_at,
    ],
)

activity_time_in_zone_table = Table(
    name="activity_time_in_zone",
    comment="Time in heart-rate and power zones per activity (GET activity/{id}/hrTimeInZones and /powerTimeInZones).",
    primary_key=["activity_id", "kind", "zone_number"],
    columns=[
        activity_id_ctx,
        expr("kind", "VARCHAR", "hr or power.", "src.kind"),
        col("zone_number", "INTEGER", "Zone number (1-based).", "zoneNumber"),
        col("secs_in_zone", "DOUBLE", "Seconds in the zone.", "secsInZone"),
        col("zone_low_boundary", "INTEGER", "Lower boundary (bpm or watts).", "zoneLowBoundary"),
        fetched_at,
    ],
)

activity_gear_table = Table(
    name="activity_gear",
    comment="Gear linked to each activity (GET gear-service/gear/filterGear?activityId=).",
    primary_key=["activity_id", "gear_uuid"],
    columns=[
        activity_id_ctx,
        col("gear_uuid", "VARCHAR", "Gear uuid (references gear.uuid).", "uuid"),
        col("gear_pk", "BIGINT", "Gear primary key.", "gearPk"),
        col("display_name", "VARCHAR", "Gear name.", "displayName"),
        col("custom_make_model", "VARCHAR", "Make and model.", "customMakeModel"),
        col("gear_type_name", "VARCHAR", "Gear type (Shoes, Bike, ...).", "gearTypeName"),
        col("gear_status_name", "VARCHAR", "active or retired.", "gearStatusName"),
        extra_col("All other fields of the gear object.", None),
        fetched_at,
    ],
)

activity_exercise_set_table = Table(
    name="activity_exercise_set",
    comment="Exercise sets of strength activities (GET activity/{id}/exerciseSets).",
    primary_key=["activity_id", "idx"],
    columns=[
        activity_id_ctx,
        expr("idx", "INTEGER", "Position in the set list.", "src.idx"),
        col("set_type", "VARCHAR", "ACTIVE or REST.", "setType"),
        expr("category", "VARCHAR", "Exercise category of the most likely exercise.", "b->'exercises'->0->>'category'", "exercises"),
        expr("exercise_name", "VARCHAR", "Exercise name of the most likely exercise.", "b->'exercises'->0->>'name'", "exercises"),
        col("exercises", "JSON", "Exercise candidates ([{category, name, probability}]).", "exercises"),
        col("repetition_count", "INTEGER", "Repetitions.", "repetitionCount"),
        col("weight", "DOUBLE", "Weight in grams.", "weight"),
        col("duration", "DOUBLE", "Duration in seconds.", "duration"),
        ts("start_time", "Set start (UTC).", "startTime"),
        col("message_index", "INTEGER", "FIT message index.", "messageIndex"),
        col("wkt_step_index", "INTEGER", "Workout step index.", "wktStepIndex"),
        extra_col("Other fields of the set object.", None),
        fetched_at,
    ],
)

activity_polyline_table = Table(
    name="activity_polyline",
    comment="Encoded GPS track of each activity (GET activity/polyline/{id}): Google polyline format.",
    primary_key=["activity_id"],
    columns=[
        activity_id_ctx,
        nested("number_of_points", "INTEGER", "Number of points.", "gPolyline.numberOfPoints", "gPolyline"),
        nested("encoded_samples", "VARCHAR", "Google-encoded polyline (lat/lon).", "gPolyline.encodedSamples", "gPolyline"),
        nested("encoded_levels", "VARCHAR", "Google-encoded levels.", "gPolyline.encodedLevels", "gPolyline"),
        nested("min_lat", "DOUBLE", "Bounding box min latitude.", "gPolyline.minLat", "gPolyline"),
        nested("max_lat", "DOUBLE", "Bounding box max latitude.", "gPolyline.maxLat", "gPolyline"),
        nested("min_lon", "DOUBLE", "Bounding box min longitude.", "gPolyline.minLon", "gPolyline"),
        nested("max_lon", "DOUBLE", "Bounding box max longitude.", "gPolyline.maxLon", "gPolyline"),
        nested("start_lat", "DOUBLE", "Start latitude.", "gPolyline.startLat", "gPolyline"),
        nested("start_lon", "DOUBLE", "Start longitude.", "gPolyline.startLon", "gPolyline"),
        nested("end_lat", "DOUBLE", "End latitude.", "gPolyline.endLat", "gPolyline"),
        nested("end_lon", "DOUBLE", "End longitude.", "gPolyline.endLon", "gPolyline"),
        fetched_at,
    ],
)

activity_detail_metric_table = Table(
    name="activity_detail_metric",
    comment=(
        "Metric descriptors of the activity details chart payload (GET activity/{id}/details): which metric sits at "
        "which index of each activityDetailMetrics[].metrics array in raw_current (object_type activity_details). "
        "The per-second series themselves come from the original FIT file (fit_record)."
    ),
    primary_key=["activity_id", "metrics_index"],
    columns=[
        activity_id_ctx,
        col("metrics_index", "INTEGER", "Index into the metrics arrays.", "metricsIndex"),
        col("key", "VARCHAR", "Metric key (directHeartRate, directSpeed, directElevation, sumDistance, ...).", "key"),
        nested("unit_key", "VARCHAR", "Unit key.", "unit.key", "unit"),
        nested("unit_factor", "DOUBLE", "Unit factor.", "unit.factor", "unit"),
        col("app_id", "VARCHAR", "Connect IQ app id (developer fields).", "appID"),
        col("developer_field_number", "INTEGER", "Developer field number.", "developerFieldNumber"),
        expr("measurement_count", "INTEGER", "Points in the chart payload.", "src.measurement_count"),
        expr("details_available", "BOOLEAN", "Whether Garmin had chart data.", "src.details_available"),
        fetched_at,
    ],
)

# ---------------------------------------------------------------------------------
# Daily wellness
# ---------------------------------------------------------------------------------

daily_summary_table = Table(
    name="daily_summary",
    comment="One row per day: the Garmin daily user summary (steps, calories, heart rate, stress, Body Battery, floors, intensity minutes, respiration, SpO2). Source: usersummary-service daily.",
    primary_key=["calendar_date"],
    columns=[
        col("calendar_date", "DATE", "Calendar day.", "calendarDate"),
        col("user_profile_id", "BIGINT", "User profile id.", "userProfileId"),
        col("user_daily_summary_id", "BIGINT", "Daily summary id.", "userDailySummaryId"),
        col("uuid", "VARCHAR", "Summary uuid.", "uuid"),
        col("source", "VARCHAR", "Source (GARMIN).", "source"),
        nested("rule", "VARCHAR", "Privacy rule key.", "rule.typeKey", "rule"),
        col("total_kilocalories", "DOUBLE", "Total calories in kcal.", "totalKilocalories"),
        col("active_kilocalories", "DOUBLE", "Active calories in kcal.", "activeKilocalories"),
        col("bmr_kilocalories", "DOUBLE", "BMR calories in kcal.", "bmrKilocalories"),
        col("wellness_kilocalories", "DOUBLE", "Wellness calories in kcal.", "wellnessKilocalories"),
        col("burned_kilocalories", "DOUBLE", "Burned calories in kcal.", "burnedKilocalories"),
        col("consumed_kilocalories", "DOUBLE", "Consumed calories in kcal.", "consumedKilocalories"),
        col("remaining_kilocalories", "DOUBLE", "Remaining calories in kcal.", "remainingKilocalories"),
        col("wellness_active_kilocalories", "DOUBLE", "Wellness active calories in kcal.", "wellnessActiveKilocalories"),
        col("net_remaining_kilocalories", "DOUBLE", "Net remaining calories in kcal.", "netRemainingKilocalories"),
        col("net_calorie_goal", "INTEGER", "Net calorie goal in kcal.", "netCalorieGoal"),
        col("resting_calories_from_activity", "DOUBLE", "Resting calories during activities in kcal.", "restingCaloriesFromActivity"),
        col("total_steps", "INTEGER", "Steps.", "totalSteps"),
        col("daily_step_goal", "INTEGER", "Step goal.", "dailyStepGoal"),
        col("total_distance_meters", "INTEGER", "Distance in meters.", "totalDistanceMeters"),
        col("wellness_distance_meters", "INTEGER", "Wellness (non-activity) distance in meters.", "wellnessDistanceMeters"),
        ts("wellness_start_time_gmt", "Day start (UTC).", "wellnessStartTimeGmt"),
        tsl("wellness_start_time_local", "Day start, local.", "wellnessStartTimeLocal"),
        ts("wellness_end_time_gmt", "Day end (UTC).", "wellnessEndTimeGmt"),
        tsl("wellness_end_time_local", "Day end, local.", "wellnessEndTimeLocal"),
        col("duration_in_milliseconds", "BIGINT", "Day duration in ms.", "durationInMilliseconds"),
        col("wellness_description", "VARCHAR", "Description.", "wellnessDescription"),
        col("highly_active_seconds", "INTEGER", "Highly active seconds.", "highlyActiveSeconds"),
        col("active_seconds", "INTEGER", "Active seconds.", "activeSeconds"),
        col("sedentary_seconds", "INTEGER", "Sedentary seconds.", "sedentarySeconds"),
        col("sleeping_seconds", "INTEGER", "Sleeping seconds.", "sleepingSeconds"),
        col("includes_wellness_data", "BOOLEAN", "Has wellness data.", "includesWellnessData"),
        col("includes_activity_data", "BOOLEAN", "Has activity data.", "includesActivityData"),
        col("includes_calorie_consumed_data", "BOOLEAN", "Has food log data.", "includesCalorieConsumedData"),
        col("privacy_protected", "BOOLEAN", "Privacy protected.", "privacyProtected"),
        col("moderate_intensity_minutes", "INTEGER", "Moderate intensity minutes.", "moderateIntensityMinutes"),
        col("vigorous_intensity_minutes", "INTEGER", "Vigorous intensity minutes.", "vigorousIntensityMinutes"),
        col("intensity_minutes_goal", "INTEGER", "Weekly intensity minutes goal.", "intensityMinutesGoal"),
        col("floors_ascended_in_meters", "DOUBLE", "Ascent in meters.", "floorsAscendedInMeters"),
        col("floors_descended_in_meters", "DOUBLE", "Descent in meters.", "floorsDescendedInMeters"),
        col("floors_ascended", "DOUBLE", "Floors climbed.", "floorsAscended"),
        col("floors_descended", "DOUBLE", "Floors descended.", "floorsDescended"),
        col("user_floors_ascended_goal", "INTEGER", "Floors goal.", "userFloorsAscendedGoal"),
        col("min_heart_rate", "INTEGER", "Min heart rate in bpm.", "minHeartRate"),
        col("max_heart_rate", "INTEGER", "Max heart rate in bpm.", "maxHeartRate"),
        col("resting_heart_rate", "INTEGER", "Resting heart rate in bpm.", "restingHeartRate"),
        col("last_seven_days_avg_resting_heart_rate", "INTEGER", "7-day average resting HR in bpm.", "lastSevenDaysAvgRestingHeartRate"),
        col("min_avg_heart_rate", "INTEGER", "Lowest 2-minute average HR in bpm.", "minAvgHeartRate"),
        col("max_avg_heart_rate", "INTEGER", "Highest 2-minute average HR in bpm.", "maxAvgHeartRate"),
        col("abnormal_heart_rate_alerts_count", "INTEGER", "Abnormal HR alerts.", "abnormalHeartRateAlertsCount"),
        col("average_stress_level", "INTEGER", "Average stress (0-100).", "averageStressLevel"),
        col("max_stress_level", "INTEGER", "Max stress (0-100).", "maxStressLevel"),
        col("stress_duration", "INTEGER", "Stress duration in seconds.", "stressDuration"),
        col("rest_stress_duration", "INTEGER", "Rest duration in seconds.", "restStressDuration"),
        col("activity_stress_duration", "INTEGER", "Activity stress duration in seconds.", "activityStressDuration"),
        col("uncategorized_stress_duration", "INTEGER", "Uncategorized stress duration in seconds.", "uncategorizedStressDuration"),
        col("total_stress_duration", "INTEGER", "Total measured duration in seconds.", "totalStressDuration"),
        col("low_stress_duration", "INTEGER", "Low stress duration in seconds.", "lowStressDuration"),
        col("medium_stress_duration", "INTEGER", "Medium stress duration in seconds.", "mediumStressDuration"),
        col("high_stress_duration", "INTEGER", "High stress duration in seconds.", "highStressDuration"),
        col("stress_percentage", "DOUBLE", "Stress share in %.", "stressPercentage"),
        col("rest_stress_percentage", "DOUBLE", "Rest share in %.", "restStressPercentage"),
        col("activity_stress_percentage", "DOUBLE", "Activity share in %.", "activityStressPercentage"),
        col("uncategorized_stress_percentage", "DOUBLE", "Uncategorized share in %.", "uncategorizedStressPercentage"),
        col("low_stress_percentage", "DOUBLE", "Low stress share in %.", "lowStressPercentage"),
        col("medium_stress_percentage", "DOUBLE", "Medium stress share in %.", "mediumStressPercentage"),
        col("high_stress_percentage", "DOUBLE", "High stress share in %.", "highStressPercentage"),
        col("stress_qualifier", "VARCHAR", "Stress qualifier (BALANCED, STRESSFUL, ...).", "stressQualifier"),
        col("measurable_awake_duration", "INTEGER", "Measurable awake seconds.", "measurableAwakeDuration"),
        col("measurable_asleep_duration", "INTEGER", "Measurable asleep seconds.", "measurableAsleepDuration"),
        ts("last_sync_timestamp_gmt", "Last device sync (UTC).", "lastSyncTimestampGMT"),
        col("body_battery_charged_value", "INTEGER", "Body Battery charged.", "bodyBatteryChargedValue"),
        col("body_battery_drained_value", "INTEGER", "Body Battery drained.", "bodyBatteryDrainedValue"),
        col("body_battery_highest_value", "INTEGER", "Body Battery high.", "bodyBatteryHighestValue"),
        col("body_battery_lowest_value", "INTEGER", "Body Battery low.", "bodyBatteryLowestValue"),
        col("body_battery_most_recent_value", "INTEGER", "Body Battery latest.", "bodyBatteryMostRecentValue"),
        col("body_battery_during_sleep", "INTEGER", "Body Battery gained during sleep.", "bodyBatteryDuringSleep"),
        col("body_battery_at_wake_time", "INTEGER", "Body Battery at wake.", "bodyBatteryAtWakeTime"),
        col("body_battery_version", "DOUBLE", "Body Battery algorithm version.", "bodyBatteryVersion"),
        col("body_battery_dynamic_feedback_event", "JSON", "Current Body Battery feedback event.", "bodyBatteryDynamicFeedbackEvent"),
        col("end_of_day_body_battery_dynamic_feedback_event", "JSON", "End-of-day Body Battery feedback event.", "endOfDayBodyBatteryDynamicFeedbackEvent"),
        col("body_battery_activity_event_list", "JSON", "Body Battery events (sleep, activities, naps) with impact.", "bodyBatteryActivityEventList"),
        col("average_spo2", "DOUBLE", "Average SpO2 in %.", "averageSpo2"),
        col("lowest_spo2", "INTEGER", "Lowest SpO2 in %.", "lowestSpo2"),
        col("latest_spo2", "INTEGER", "Latest SpO2 in %.", "latestSpo2"),
        ts("latest_spo2_reading_time_gmt", "Latest SpO2 reading (UTC).", "latestSpo2ReadingTimeGmt"),
        tsl("latest_spo2_reading_time_local", "Latest SpO2 reading, local.", "latestSpo2ReadingTimeLocal"),
        col("average_monitoring_environment_altitude", "DOUBLE", "Average altitude in meters.", "averageMonitoringEnvironmentAltitude"),
        col("avg_waking_respiration_value", "DOUBLE", "Average waking respiration in breaths/min.", "avgWakingRespirationValue"),
        col("highest_respiration_value", "DOUBLE", "Highest respiration in breaths/min.", "highestRespirationValue"),
        col("lowest_respiration_value", "DOUBLE", "Lowest respiration in breaths/min.", "lowestRespirationValue"),
        col("latest_respiration_value", "DOUBLE", "Latest respiration in breaths/min.", "latestRespirationValue"),
        ts("latest_respiration_time_gmt", "Latest respiration reading (UTC).", "latestRespirationTimeGMT"),
        col("respiration_algorithm_version", "INTEGER", "Respiration algorithm version.", "respirationAlgorithmVersion"),
        fetched_at,
    ],
)

daily_summary_v2_table = Table(
    name="daily_summary_v2",
    comment="One row per day from the range endpoint usersummary-service/v2/dailysummaries (up to a year per call): compact movement, calories, heart rate, stress, Body Battery and respiration figures.",
    primary_key=["calendar_date"],
    columns=[
        col("calendar_date", "DATE", "Calendar day.", "calendarDate"),
        col("uuid", "VARCHAR", "Summary uuid.", "uuid"),
        col("user_profile_pk", "BIGINT", "User profile id.", "userProfilePk"),
        col("source", "VARCHAR", "Source.", "source"),
        col("includes_wellness_data", "BOOLEAN", "Has wellness data.", "includesWellnessData"),
        col("includes_activity_data", "BOOLEAN", "Has activity data.", "includesActivityData"),
        nested("start_timestamp_gmt", "TIMESTAMPTZ", "Day start (UTC).", "wellnessChronology.startTimestampGmt", "wellnessChronology"),
        nested("end_timestamp_gmt", "TIMESTAMPTZ", "Day end (UTC).", "wellnessChronology.endTimestampGmt", "wellnessChronology"),
        nested("start_timestamp_local", "TIMESTAMP", "Day start, local.", "wellnessChronology.startTimestampLocal", "wellnessChronology"),
        nested("end_timestamp_local", "TIMESTAMP", "Day end, local.", "wellnessChronology.endTimestampLocal", "wellnessChronology"),
        nested("steps", "INTEGER", "Steps.", "movement.steps.value", "movement"),
        nested("step_goal", "INTEGER", "Step goal.", "movement.steps.goal", "movement"),
        nested("floors_ascended", "DOUBLE", "Floors climbed.", "movement.floorsAscended.value", "movement"),
        nested("floors_descended", "DOUBLE", "Floors descended.", "movement.floorsDescended.value", "movement"),
        col("movement", "JSON", "Movement block (steps, pushes, floors with goals).", "movement"),
        nested("calories_resting", "INTEGER", "Resting calories in kcal.", "calories.burnedResting", "calories"),
        nested("calories_active", "INTEGER", "Active calories in kcal.", "calories.burnedActive", "calories"),
        nested("calories_total", "INTEGER", "Total calories in kcal.", "calories.burnedTotal", "calories"),
        nested("calories_consumed_goal", "INTEGER", "Consumed calorie goal in kcal.", "calories.consumedGoal", "calories"),
        nested("hr_min", "INTEGER", "Min heart rate in bpm.", "heartRate.minValue", "heartRate"),
        nested("hr_max", "INTEGER", "Max heart rate in bpm.", "heartRate.maxValue", "heartRate"),
        nested("hr_resting", "INTEGER", "Resting heart rate in bpm.", "heartRate.restingValue", "heartRate"),
        nested("intensity_minutes_goal", "INTEGER", "Weekly goal.", "intensityMinutes.goal", "intensityMinutes"),
        nested("intensity_minutes_moderate", "INTEGER", "Moderate minutes.", "intensityMinutes.moderate", "intensityMinutes"),
        nested("intensity_minutes_vigorous", "INTEGER", "Vigorous minutes.", "intensityMinutes.vigorous", "intensityMinutes"),
        nested("stress_avg", "INTEGER", "Average stress.", "stress.avgLevel", "stress"),
        nested("stress_max", "INTEGER", "Max stress.", "stress.maxLevel", "stress"),
        nested("stress_qualifier", "VARCHAR", "Stress qualifier.", "stress.qualifier", "stress"),
        col("stress", "JSON", "Stress block (proportions and durations in ms).", "stress"),
        nested("body_battery_min", "INTEGER", "Body Battery low.", "bodyBattery.minValue", "bodyBattery"),
        nested("body_battery_max", "INTEGER", "Body Battery high.", "bodyBattery.maxValue", "bodyBattery"),
        nested("body_battery_charged", "INTEGER", "Charged.", "bodyBattery.chargedValue", "bodyBattery"),
        nested("body_battery_drained", "INTEGER", "Drained.", "bodyBattery.drainedValue", "bodyBattery"),
        nested("body_battery_latest", "INTEGER", "Latest.", "bodyBattery.latestValue", "bodyBattery"),
        col("body_battery", "JSON", "Body Battery block (feedback events, activity events).", "bodyBattery"),
        col("hydration", "JSON", "Hydration block.", "hydration"),
        nested("respiration_avg", "INTEGER", "Average respiration in breaths/min.", "respiration.avgValue", "respiration"),
        nested("respiration_min", "INTEGER", "Min respiration.", "respiration.minValue", "respiration"),
        nested("respiration_max", "INTEGER", "Max respiration.", "respiration.maxValue", "respiration"),
        col("respiration", "JSON", "Respiration block.", "respiration"),
        col("pulse_ox", "JSON", "Pulse ox block.", "pulseOx"),
        col("jet_lag", "JSON", "Jet lag block.", "jetLag"),
        fetched_at,
    ],
)

steps_interval_table = Table(
    name="steps_interval",
    comment="15-minute step intervals per day (GET wellness/dailySummaryChart). Still available for days whose other intraday data is archived.",
    primary_key=["calendar_date", "start_gmt"],
    columns=[
        cal_date,
        ts("start_gmt", "Interval start (UTC).", "startGMT"),
        ts("end_gmt", "Interval end (UTC).", "endGMT"),
        col("steps", "INTEGER", "Steps in the interval.", "steps"),
        col("pushes", "INTEGER", "Wheelchair pushes.", "pushes"),
        col("primary_activity_level", "VARCHAR", "sedentary, active, highlyActive, sleeping, generic.", "primaryActivityLevel"),
        col("activity_level_constant", "BOOLEAN", "Level constant across the interval.", "activityLevelConstant"),
        fetched_at,
    ],
)

heart_rate_day_table = Table(
    name="heart_rate_day",
    comment="Daily heart-rate summary (GET wellness/dailyHeartRate). Intraday samples are in heart_rate_sample.",
    primary_key=["calendar_date"],
    columns=[
        col("calendar_date", "DATE", "Calendar day.", "calendarDate"),
        col("user_profile_pk", "BIGINT", "User profile id.", "userProfilePK"),
        ts("start_timestamp_gmt", "Day start (UTC).", "startTimestampGMT"),
        ts("end_timestamp_gmt", "Day end (UTC).", "endTimestampGMT"),
        tsl("start_timestamp_local", "Day start, local.", "startTimestampLocal"),
        tsl("end_timestamp_local", "Day end, local.", "endTimestampLocal"),
        col("max_heart_rate", "INTEGER", "Max HR in bpm.", "maxHeartRate"),
        col("min_heart_rate", "INTEGER", "Min HR in bpm.", "minHeartRate"),
        col("resting_heart_rate", "INTEGER", "Resting HR in bpm.", "restingHeartRate"),
        col("last_seven_days_avg_resting_heart_rate", "INTEGER", "7-day average resting HR in bpm.", "lastSevenDaysAvgRestingHeartRate"),
        expr("sample_count", "INTEGER", "Number of intraday samples (0 when archived).", "coalesce(json_array_length(b->'heartRateValues'), 0)", "heartRateValues"),
        col("heart_rate_value_descriptors", "JSON", "Column descriptors of the samples.", "heartRateValueDescriptors"),
        fetched_at,
    ],
)

heart_rate_sample_table = Table(
    name="heart_rate_sample",
    comment="Intraday heart rate, one row per 2-minute sample (GET wellness/dailyHeartRate, heartRateValues). Null heart rate means no reading.",
    primary_key=["calendar_date", "timestamp_gmt"],
    columns=[
        cal_date,
        expr("timestamp_gmt", "TIMESTAMPTZ", "Sample time (UTC).", "to_timestamp(TRY_CAST(src.v[1] AS BIGINT) / 1000.0)"),
        expr("heart_rate", "INTEGER", "Heart rate in bpm.", "TRY_CAST(src.v[2] AS INTEGER)"),
        fetched_at,
    ],
)

stress_day_table = Table(
    name="stress_day",
    comment="Daily stress summary (GET wellness/dailyStress). Intraday samples are in stress_sample (stress and Body Battery every 3 minutes).",
    primary_key=["calendar_date"],
    columns=[
        col("calendar_date", "DATE", "Calendar day.", "calendarDate"),
        col("user_profile_pk", "BIGINT", "User profile id.", "userProfilePK"),
        ts("start_timestamp_gmt", "Day start (UTC).", "startTimestampGMT"),
        ts("end_timestamp_gmt", "Day end (UTC).", "endTimestampGMT"),
        tsl("start_timestamp_local", "Day start, local.", "startTimestampLocal"),
        tsl("end_timestamp_local", "Day end, local.", "endTimestampLocal"),
        col("max_stress_level", "INTEGER", "Max stress (0-100).", "maxStressLevel"),
        col("avg_stress_level", "INTEGER", "Average stress (0-100).", "avgStressLevel"),
        col("stress_chart_value_offset", "INTEGER", "Chart offset.", "stressChartValueOffset"),
        col("stress_chart_y_axis_origin", "INTEGER", "Chart origin.", "stressChartYAxisOrigin"),
        expr("sample_count", "INTEGER", "Number of intraday samples (0 when archived).", "coalesce(json_array_length(b->'stressValuesArray'), 0)", "stressValuesArray"),
        col("stress_value_descriptors", "JSON", "Column descriptors of the stress samples.", "stressValueDescriptorsDTOList"),
        col("body_battery_value_descriptors", "JSON", "Column descriptors of the Body Battery samples.", "bodyBatteryValueDescriptorsDTOList"),
        fetched_at,
    ],
)

stress_sample_table = Table(
    name="stress_sample",
    comment="Intraday stress and Body Battery, one row per 3-minute sample (GET wellness/dailyStress). stress_level -1 = no reading, -2 = activity.",
    primary_key=["calendar_date", "timestamp_gmt"],
    columns=[
        cal_date,
        expr("timestamp_gmt", "TIMESTAMPTZ", "Sample time (UTC).", "to_timestamp(TRY_CAST(src.ts AS BIGINT) / 1000.0)"),
        expr("stress_level", "INTEGER", "Stress level (0-100; negative = unmeasurable/activity).", "src.stress"),
        expr("body_battery_status", "VARCHAR", "MEASURED, ESTIMATED or null.", "src.bb_status"),
        expr("body_battery_level", "INTEGER", "Body Battery (0-100).", "src.bb_level"),
        expr("body_battery_version", "DOUBLE", "Body Battery algorithm version.", "src.bb_version"),
        fetched_at,
    ],
)

body_battery_day_table = Table(
    name="body_battery_day",
    comment="Daily Body Battery (GET wellness/bodyBattery/reports/daily, range endpoint): charged/drained and the per-day feedback events; samples in body_battery_sample.",
    primary_key=["calendar_date"],
    columns=[
        col("calendar_date", "DATE", "Calendar day.", "date"),
        col("charged", "INTEGER", "Charged during the day.", "charged"),
        col("drained", "INTEGER", "Drained during the day.", "drained"),
        ts("start_timestamp_gmt", "Day start (UTC).", "startTimestampGMT"),
        ts("end_timestamp_gmt", "Day end (UTC).", "endTimestampGMT"),
        tsl("start_timestamp_local", "Day start, local.", "startTimestampLocal"),
        tsl("end_timestamp_local", "Day end, local.", "endTimestampLocal"),
        col("body_battery_value_descriptors", "JSON", "Column descriptors of the samples.", "bodyBatteryValueDescriptorDTOList"),
        col("body_battery_dynamic_feedback_event", "JSON", "Current feedback event.", "bodyBatteryDynamicFeedbackEvent"),
        col("end_of_day_body_battery_dynamic_feedback_event", "JSON", "End-of-day feedback event.", "endOfDayBodyBatteryDynamicFeedbackEvent"),
        col("body_battery_activity_event", "JSON", "Events (sleep, activity, nap, recovery) with Body Battery impact.", "bodyBatteryActivityEvent"),
        expr("sample_count", "INTEGER", "Number of samples.", "coalesce(json_array_length(b->'bodyBatteryValuesArray'), 0)", "bodyBatteryValuesArray"),
        fetched_at,
    ],
)

body_battery_sample_table = Table(
    name="body_battery_sample",
    comment="Body Battery level over the day from the reports/daily endpoint (coarser than stress_sample; kept for archived days).",
    primary_key=["calendar_date", "timestamp_gmt"],
    columns=[
        cal_date,
        expr("timestamp_gmt", "TIMESTAMPTZ", "Sample time (UTC).", "to_timestamp(TRY_CAST(src.v[1] AS BIGINT) / 1000.0)"),
        expr("body_battery_level", "INTEGER", "Body Battery (0-100).", "TRY_CAST(src.v[2] AS INTEGER)"),
        fetched_at,
    ],
)

body_battery_event_table = Table(
    name="body_battery_event",
    comment="Body Battery events of the day (GET wellness/bodyBattery/events/{date}): sleep, activities, naps, with their stress/Body Battery traces as JSON.",
    primary_key=["calendar_date", "idx"],
    columns=[
        cal_date,
        expr("idx", "INTEGER", "Position in the event list.", "src.idx"),
        nested("event_type", "VARCHAR", "SLEEP, ACTIVITY, NAP, RECOVERY_CHECK ...", "event.eventType", "event"),
        nested("event_start_time_gmt", "TIMESTAMPTZ", "Event start (UTC).", "event.eventStartTimeGmt", "event"),
        nested("timezone_offset", "INTEGER", "Offset from UTC in ms.", "event.timezoneOffset", "event"),
        nested("duration_in_milliseconds", "BIGINT", "Duration in ms.", "event.durationInMilliseconds", "event"),
        nested("body_battery_impact", "INTEGER", "Body Battery change caused by the event.", "event.bodyBatteryImpact", "event"),
        nested("feedback_type", "VARCHAR", "Feedback type.", "event.feedbackType", "event"),
        nested("short_feedback", "VARCHAR", "Short feedback key.", "event.shortFeedback", "event"),
        col("activity_name", "VARCHAR", "Activity name (activity events).", "activityName"),
        col("activity_type", "VARCHAR", "Activity type (activity events).", "activityType"),
        col("activity_id", "BIGINT", "Activity id (activity events).", "activityId"),
        col("average_stress", "DOUBLE", "Average stress during the event.", "averageStress"),
        col("stress_value_descriptors", "JSON", "Descriptors of stressValuesArray.", "stressValueDescriptorsDTOList"),
        col("stress_values", "JSON", "[[timestamp, stressLevel], ...] during the event.", "stressValuesArray"),
        col("body_battery_value_descriptors", "JSON", "Descriptors of bodyBatteryValuesArray.", "bodyBatteryValueDescriptorsDTOList"),
        col("body_battery_values", "JSON", "[[timestamp, status, level, version], ...] during the event.", "bodyBatteryValuesArray"),
        fetched_at,
    ],
)

respiration_day_table = Table(
    name="respiration_day",
    comment="Daily respiration summary (GET wellness/daily/respiration); samples in respiration_sample.",
    primary_key=["calendar_date"],
    columns=[
        col("calendar_date", "DATE", "Calendar day.", "calendarDate"),
        col("user_profile_pk", "BIGINT", "User profile id.", "userProfilePK"),
        ts("start_timestamp_gmt", "Day start (UTC).", "startTimestampGMT"),
        ts("end_timestamp_gmt", "Day end (UTC).", "endTimestampGMT"),
        tsl("start_timestamp_local", "Day start, local.", "startTimestampLocal"),
        tsl("end_timestamp_local", "Day end, local.", "endTimestampLocal"),
        ts("sleep_start_timestamp_gmt", "Sleep start (UTC).", "sleepStartTimestampGMT"),
        ts("sleep_end_timestamp_gmt", "Sleep end (UTC).", "sleepEndTimestampGMT"),
        tsl("sleep_start_timestamp_local", "Sleep start, local.", "sleepStartTimestampLocal"),
        tsl("sleep_end_timestamp_local", "Sleep end, local.", "sleepEndTimestampLocal"),
        ts("tomorrow_sleep_start_timestamp_gmt", "Next sleep start (UTC).", "tomorrowSleepStartTimestampGMT"),
        ts("tomorrow_sleep_end_timestamp_gmt", "Next sleep end (UTC).", "tomorrowSleepEndTimestampGMT"),
        tsl("tomorrow_sleep_start_timestamp_local", "Next sleep start, local.", "tomorrowSleepStartTimestampLocal"),
        tsl("tomorrow_sleep_end_timestamp_local", "Next sleep end, local.", "tomorrowSleepEndTimestampLocal"),
        col("lowest_respiration_value", "DOUBLE", "Lowest respiration in breaths/min.", "lowestRespirationValue"),
        col("highest_respiration_value", "DOUBLE", "Highest respiration in breaths/min.", "highestRespirationValue"),
        col("avg_waking_respiration_value", "DOUBLE", "Average waking respiration.", "avgWakingRespirationValue"),
        col("avg_sleep_respiration_value", "DOUBLE", "Average sleep respiration.", "avgSleepRespirationValue"),
        col("avg_tomorrow_sleep_respiration_value", "DOUBLE", "Average next-sleep respiration.", "avgTomorrowSleepRespirationValue"),
        col("respiration_version", "INTEGER", "Algorithm version.", "respirationVersion"),
        expr("sample_count", "INTEGER", "Number of samples (0 when archived).", "coalesce(json_array_length(b->'respirationValuesArray'), 0)", "respirationValuesArray"),
        col("respiration_value_descriptors", "JSON", "Descriptors of the samples.", "respirationValueDescriptorsDTOList"),
        col("respiration_averages_value_descriptors", "JSON", "Descriptors of the hourly averages.", "respirationAveragesValueDescriptorDTOList"),
        col("respiration_averages_values", "JSON", "Hourly averages [[timestamp, avg, high, low], ...].", "respirationAveragesValuesArray"),
        fetched_at,
    ],
)

respiration_sample_table = Table(
    name="respiration_sample",
    comment="Intraday respiration rate, one row per 2-minute sample (GET wellness/daily/respiration). Negative values mean no reading.",
    primary_key=["calendar_date", "timestamp_gmt"],
    columns=[
        cal_date,
        expr("timestamp_gmt", "TIMESTAMPTZ", "Sample time (UTC).", "to_timestamp(TRY_CAST(src.v[1] AS BIGINT) / 1000.0)"),
        expr("respiration", "DOUBLE", "Breaths per minute.", "TRY_CAST(src.v[2] AS DOUBLE)"),
        fetched_at,
    ],
)

spo2_day_table = Table(
    name="spo2_day",
    comment="Daily pulse ox summary (GET wellness/daily/spo2); single readings and hourly averages stay as JSON columns.",
    primary_key=["calendar_date"],
    columns=[
        col("calendar_date", "DATE", "Calendar day.", "calendarDate"),
        col("user_profile_pk", "BIGINT", "User profile id.", "userProfilePK"),
        ts("start_timestamp_gmt", "Day start (UTC).", "startTimestampGMT"),
        ts("end_timestamp_gmt", "Day end (UTC).", "endTimestampGMT"),
        tsl("start_timestamp_local", "Day start, local.", "startTimestampLocal"),
        tsl("end_timestamp_local", "Day end, local.", "endTimestampLocal"),
        ts("sleep_start_timestamp_gmt", "Sleep start (UTC).", "sleepStartTimestampGMT"),
        ts("sleep_end_timestamp_gmt", "Sleep end (UTC).", "sleepEndTimestampGMT"),
        tsl("sleep_start_timestamp_local", "Sleep start, local.", "sleepStartTimestampLocal"),
        tsl("sleep_end_timestamp_local", "Sleep end, local.", "sleepEndTimestampLocal"),
        ts("tomorrow_sleep_start_timestamp_gmt", "Next sleep start (UTC).", "tomorrowSleepStartTimestampGMT"),
        ts("tomorrow_sleep_end_timestamp_gmt", "Next sleep end (UTC).", "tomorrowSleepEndTimestampGMT"),
        tsl("tomorrow_sleep_start_timestamp_local", "Next sleep start, local.", "tomorrowSleepStartTimestampLocal"),
        tsl("tomorrow_sleep_end_timestamp_local", "Next sleep end, local.", "tomorrowSleepEndTimestampLocal"),
        col("average_spo2", "DOUBLE", "Average SpO2 in %.", "averageSpO2"),
        col("lowest_spo2", "INTEGER", "Lowest SpO2 in %.", "lowestSpO2"),
        col("last_seven_days_avg_spo2", "DOUBLE", "7-day average SpO2.", "lastSevenDaysAvgSpO2"),
        col("latest_spo2", "INTEGER", "Latest SpO2 in %.", "latestSpO2"),
        ts("latest_spo2_timestamp_gmt", "Latest reading (UTC).", "latestSpO2TimestampGMT"),
        tsl("latest_spo2_timestamp_local", "Latest reading, local.", "latestSpO2TimestampLocal"),
        col("avg_sleep_spo2", "DOUBLE", "Average sleep SpO2.", "avgSleepSpO2"),
        col("avg_tomorrow_sleep_spo2", "DOUBLE", "Average next-sleep SpO2.", "avgTomorrowSleepSpO2"),
        col("spo2_value_descriptors", "JSON", "Descriptors of the single values.", "spO2ValueDescriptorsDTOList"),
        col("spo2_single_values", "JSON", "Spot-check readings.", "spO2SingleValues"),
        col("continuous_readings", "JSON", "Continuous readings.", "continuousReadingDTOList"),
        col("spo2_hourly_averages", "JSON", "Hourly averages.", "spO2HourlyAverages"),
        fetched_at,
    ],
)

floors_day_table = Table(
    name="floors_day",
    comment="Floors climbed per 15-minute interval (GET wellness/floorsChartData/daily); the intervals are in floors_interval.",
    primary_key=["calendar_date"],
    columns=[
        cal_date,
        ts("start_timestamp_gmt", "Day start (UTC).", "startTimestampGMT"),
        ts("end_timestamp_gmt", "Day end (UTC).", "endTimestampGMT"),
        tsl("start_timestamp_local", "Day start, local.", "startTimestampLocal"),
        tsl("end_timestamp_local", "Day end, local.", "endTimestampLocal"),
        col("floors_value_descriptors", "JSON", "Descriptors of the intervals.", "floorsValueDescriptorDTOList"),
        expr("interval_count", "INTEGER", "Number of intervals (0 when archived).", "coalesce(json_array_length(b->'floorValuesArray'), 0)", "floorValuesArray"),
        fetched_at,
    ],
)

floors_interval_table = Table(
    name="floors_interval",
    comment="Floors ascended/descended per 15-minute interval.",
    primary_key=["calendar_date", "start_time_gmt"],
    columns=[
        cal_date,
        expr("start_time_gmt", "TIMESTAMPTZ", "Interval start (UTC).", "TRY_CAST(left(src.v[1], 23) AS TIMESTAMP)::TIMESTAMPTZ"),
        expr("end_time_gmt", "TIMESTAMPTZ", "Interval end (UTC).", "TRY_CAST(left(src.v[2], 23) AS TIMESTAMP)::TIMESTAMPTZ"),
        expr("floors_ascended", "INTEGER", "Floors ascended.", "TRY_CAST(src.v[3] AS INTEGER)"),
        expr("floors_descended", "INTEGER", "Floors descended.", "TRY_CAST(src.v[4] AS INTEGER)"),
        fetched_at,
    ],
)

intensity_minutes_day_table = Table(
    name="intensity_minutes_day",
    comment="Intensity minutes per day and the running weekly totals (GET wellness/daily/im); hourly values stay as JSON.",
    primary_key=["calendar_date"],
    columns=[
        col("calendar_date", "DATE", "Calendar day.", "calendarDate"),
        col("user_profile_pk", "BIGINT", "User profile id.", "userProfilePK"),
        ts("start_timestamp_gmt", "Day start (UTC).", "startTimestampGMT"),
        ts("end_timestamp_gmt", "Day end (UTC).", "endTimestampGMT"),
        tsl("start_timestamp_local", "Day start, local.", "startTimestampLocal"),
        tsl("end_timestamp_local", "Day end, local.", "endTimestampLocal"),
        col("weekly_moderate", "INTEGER", "Week-to-date moderate minutes.", "weeklyModerate"),
        col("weekly_vigorous", "INTEGER", "Week-to-date vigorous minutes (counted double).", "weeklyVigorous"),
        col("weekly_total", "INTEGER", "Week-to-date total.", "weeklyTotal"),
        col("week_goal", "INTEGER", "Weekly goal.", "weekGoal"),
        col("day_of_goal_met", "VARCHAR", "Day the weekly goal was met.", "dayOfGoalMet"),
        col("start_day_minutes", "INTEGER", "Minutes at day start.", "startDayMinutes"),
        col("end_day_minutes", "INTEGER", "Minutes at day end.", "endDayMinutes"),
        col("moderate_minutes", "INTEGER", "Moderate minutes of the day.", "moderateMinutes"),
        col("vigorous_minutes", "INTEGER", "Vigorous minutes of the day.", "vigorousMinutes"),
        col("im_value_descriptors", "JSON", "Descriptors of imValuesArray.", "imValueDescriptorsDTOList"),
        col("im_values", "JSON", "[[timestamp, minutes], ...] per hour.", "imValuesArray"),
        fetched_at,
    ],
)

sleep_table = Table(
    name="sleep",
    comment="Sleep per night (GET wellness/dailySleepData, dailySleepDTO): stages, scores, sleep need; the intraday series are in sleep_level, sleep_movement, sleep_sample.",
    primary_key=["calendar_date"],
    columns=[
        col("calendar_date", "DATE", "Calendar day the sleep is attributed to (wake-up day).", "calendarDate"),
        col("sleep_id", "BIGINT", "Sleep record id.", "id"),
        col("user_profile_pk", "BIGINT", "User profile id.", "userProfilePK"),
        col("sleep_time_seconds", "INTEGER", "Total sleep in seconds.", "sleepTimeSeconds"),
        col("nap_time_seconds", "INTEGER", "Nap time in seconds.", "napTimeSeconds"),
        col("sleep_window_confirmed", "BOOLEAN", "Window confirmed.", "sleepWindowConfirmed"),
        col("sleep_window_confirmation_type", "VARCHAR", "How the window was confirmed (ENHANCED_CONFIRMED_FINAL, ...).", "sleepWindowConfirmationType"),
        ms("sleep_start_timestamp_gmt", "Sleep start (UTC).", "sleepStartTimestampGMT"),
        ms("sleep_end_timestamp_gmt", "Sleep end (UTC).", "sleepEndTimestampGMT"),
        local_ms("sleep_start_timestamp_local", "Sleep start, local.", "sleepStartTimestampLocal"),
        local_ms("sleep_end_timestamp_local", "Sleep end, local.", "sleepEndTimestampLocal"),
        ms("auto_sleep_start_timestamp_gmt", "Auto-detected start (UTC).", "autoSleepStartTimestampGMT"),
        ms("auto_sleep_end_timestamp_gmt", "Auto-detected end (UTC).", "autoSleepEndTimestampGMT"),
        col("sleep_quality_type_pk", "INTEGER", "Quality type pk.", "sleepQualityTypePK"),
        col("sleep_result_type_pk", "INTEGER", "Result type pk.", "sleepResultTypePK"),
        col("unmeasurable_sleep_seconds", "INTEGER", "Unmeasurable seconds.", "unmeasurableSleepSeconds"),
        col("deep_sleep_seconds", "INTEGER", "Deep sleep in seconds.", "deepSleepSeconds"),
        col("light_sleep_seconds", "INTEGER", "Light sleep in seconds.", "lightSleepSeconds"),
        col("rem_sleep_seconds", "INTEGER", "REM sleep in seconds.", "remSleepSeconds"),
        col("awake_sleep_seconds", "INTEGER", "Awake in seconds.", "awakeSleepSeconds"),
        col("device_rem_capable", "BOOLEAN", "Device detects REM.", "deviceRemCapable"),
        col("retro", "BOOLEAN", "Retroactively computed.", "retro"),
        col("sleep_from_device", "BOOLEAN", "From device (vs manual).", "sleepFromDevice"),
        col("average_spo2_value", "DOUBLE", "Average SpO2 during sleep in %.", "averageSpO2Value"),
        col("lowest_spo2_value", "INTEGER", "Lowest SpO2 in %.", "lowestSpO2Value"),
        col("highest_spo2_value", "INTEGER", "Highest SpO2 in %.", "highestSpO2Value"),
        col("average_spo2_hr_sleep", "DOUBLE", "Average HR during SpO2 sleep measurement.", "averageSpO2HRSleep"),
        col("average_respiration_value", "DOUBLE", "Average respiration in breaths/min.", "averageRespirationValue"),
        col("lowest_respiration_value", "DOUBLE", "Lowest respiration.", "lowestRespirationValue"),
        col("highest_respiration_value", "DOUBLE", "Highest respiration.", "highestRespirationValue"),
        col("awake_count", "INTEGER", "Awakenings.", "awakeCount"),
        col("avg_sleep_stress", "DOUBLE", "Average stress during sleep.", "avgSleepStress"),
        col("age_group", "VARCHAR", "Age group used for scoring.", "ageGroup"),
        col("avg_heart_rate", "DOUBLE", "Average HR during sleep in bpm.", "avgHeartRate"),
        col("sleep_score_feedback", "VARCHAR", "Score feedback key.", "sleepScoreFeedback"),
        col("sleep_score_insight", "VARCHAR", "Score insight key.", "sleepScoreInsight"),
        col("sleep_score_personalized_insight", "VARCHAR", "Personalized insight key.", "sleepScorePersonalizedInsight"),
        nested("sleep_score", "INTEGER", "Overall sleep score (0-100).", "sleepScores.overall.value", "sleepScores"),
        nested("sleep_score_qualifier", "VARCHAR", "Overall qualifier (POOR, FAIR, GOOD, EXCELLENT).", "sleepScores.overall.qualifierKey", "sleepScores"),
        col("sleep_scores", "JSON", "All score components with qualifiers and optimal ranges.", "sleepScores"),
        col("sleep_version", "INTEGER", "Algorithm version.", "sleepVersion"),
        nested("sleep_need_baseline", "INTEGER", "Sleep need baseline in minutes.", "sleepNeed.baseline", "sleepNeed"),
        nested("sleep_need_actual", "INTEGER", "Sleep need in minutes.", "sleepNeed.actual", "sleepNeed"),
        nested("sleep_need_feedback", "VARCHAR", "Sleep need feedback.", "sleepNeed.feedback", "sleepNeed"),
        col("sleep_need", "JSON", "Sleep need block.", "sleepNeed"),
        col("next_sleep_need", "JSON", "Next sleep need block.", "nextSleepNeed"),
        col("daily_naps", "JSON", "Naps of the day (dailyNapDTOS).", "dailyNapDTOS"),
        col("breathing_disruption_severity", "VARCHAR", "Breathing disruption severity.", "breathingDisruptionSeverity"),
        expr("rem_sleep_data", "BOOLEAN", "Payload has REM data.", "TRY_CAST(src.root->>'remSleepData' AS BOOLEAN)"),
        expr("restless_moments_count", "INTEGER", "Restless moments.", "TRY_CAST(src.root->>'restlessMomentsCount' AS INTEGER)"),
        expr("avg_overnight_hrv", "DOUBLE", "Average overnight HRV in ms.", "TRY_CAST(src.root->>'avgOvernightHrv' AS DOUBLE)"),
        expr("hrv_status", "VARCHAR", "HRV status (BALANCED, UNBALANCED, LOW, POOR).", "src.root->>'hrvStatus'"),
        expr("body_battery_change", "INTEGER", "Body Battery change overnight.", "TRY_CAST(src.root->>'bodyBatteryChange' AS INTEGER)"),
        expr("resting_heart_rate", "INTEGER", "Resting HR in bpm.", "TRY_CAST(src.root->>'restingHeartRate' AS INTEGER)"),
        expr("skin_temp_data_exists", "BOOLEAN", "Skin temperature recorded.", "TRY_CAST(src.root->>'skinTempDataExists' AS BOOLEAN)"),
        expr("respiration_version", "INTEGER", "Respiration algorithm version.", "TRY_CAST(src.root->>'respirationVersion' AS INTEGER)"),
        expr("spo2_sleep_summary", "JSON", "wellnessSpO2SleepSummaryDTO (average, lowest, HR).", "src.root->'wellnessSpO2SleepSummaryDTO'"),
        expr("respiration_averages", "JSON", "wellnessEpochRespirationAveragesList.", "src.root->'wellnessEpochRespirationAveragesList'"),
        expr("breathing_disruption_data", "JSON", "breathingDisruptionData events.", "src.root->'breathingDisruptionData'"),
        expr("level_count", "INTEGER", "Sleep level segments (0 when archived).", "coalesce(json_array_length(src.root->'sleepLevels'), 0)"),
        fetched_at,
    ],
)

sleep_level_table = Table(
    name="sleep_level",
    comment="Sleep stage segments per night (sleepLevels): activityLevel 0 deep, 1 light, 2 REM, 3 awake.",
    primary_key=["calendar_date", "start_gmt"],
    columns=[
        cal_date,
        ts("start_gmt", "Segment start (UTC).", "startGMT"),
        ts("end_gmt", "Segment end (UTC).", "endGMT"),
        col("activity_level", "DOUBLE", "Stage: 0 deep, 1 light, 2 REM, 3 awake.", "activityLevel"),
        fetched_at,
    ],
)

sleep_movement_table = Table(
    name="sleep_movement",
    comment="Movement level per minute during sleep (sleepMovement).",
    primary_key=["calendar_date", "start_gmt"],
    columns=[
        cal_date,
        ts("start_gmt", "Minute start (UTC).", "startGMT"),
        ts("end_gmt", "Minute end (UTC).", "endGMT"),
        col("activity_level", "DOUBLE", "Movement level.", "activityLevel"),
        fetched_at,
    ],
)

sleep_sample_table = Table(
    name="sleep_sample",
    comment="Overnight time series of the sleep payload in long form: heart_rate (sleepHeartRate), stress (sleepStress), body_battery (sleepBodyBattery), hrv (hrvData, ms), respiration (wellnessEpochRespirationDataDTOList, breaths/min), restless (sleepRestlessMoments).",
    primary_key=["calendar_date", "series", "timestamp_gmt"],
    columns=[
        cal_date,
        expr("series", "VARCHAR", "heart_rate, stress, body_battery, hrv, respiration or restless.", "src.series"),
        expr("timestamp_gmt", "TIMESTAMPTZ", "Sample time (UTC).", "to_timestamp(TRY_CAST(coalesce(b->>'startGMT', b->>'startTimeGMT') AS BIGINT) / 1000.0)"),
        expr("value", "DOUBLE", "Value in the series' unit.", "TRY_CAST(coalesce(b->>'value', b->>'respirationValue') AS DOUBLE)"),
        fetched_at,
    ],
)

hrv_table = Table(
    name="hrv",
    comment="Nightly HRV status (GET hrv-service/hrv/{date}; the range endpoint fills the summary for days without readings). Readings are in hrv_reading.",
    primary_key=["calendar_date"],
    columns=[
        cal_date,
        nested("weekly_avg", "INTEGER", "7-day average HRV in ms.", "hrvSummary.weeklyAvg", "hrvSummary"),
        nested("last_night_avg", "INTEGER", "Last night average HRV in ms.", "hrvSummary.lastNightAvg", "hrvSummary"),
        nested("last_night_5min_high", "INTEGER", "Highest 5-minute HRV in ms.", "hrvSummary.lastNight5MinHigh", "hrvSummary"),
        nested("baseline_low_upper", "INTEGER", "Baseline low upper bound in ms.", "hrvSummary.baseline.lowUpper", "hrvSummary"),
        nested("baseline_balanced_low", "INTEGER", "Baseline balanced lower bound in ms.", "hrvSummary.baseline.balancedLow", "hrvSummary"),
        nested("baseline_balanced_upper", "INTEGER", "Baseline balanced upper bound in ms.", "hrvSummary.baseline.balancedUpper", "hrvSummary"),
        nested("baseline_marker_value", "DOUBLE", "Baseline marker.", "hrvSummary.baseline.markerValue", "hrvSummary"),
        nested("status", "VARCHAR", "BALANCED, UNBALANCED, LOW, POOR, NONE.", "hrvSummary.status", "hrvSummary"),
        nested("feedback_phrase", "VARCHAR", "Feedback key.", "hrvSummary.feedbackPhrase", "hrvSummary"),
        nested("create_timestamp", "TIMESTAMPTZ", "Created (UTC).", "hrvSummary.createTimeStamp", "hrvSummary"),
        col("user_profile_pk", "BIGINT", "User profile id.", "userProfilePk"),
        ts("start_timestamp_gmt", "Measurement window start (UTC).", "startTimestampGMT"),
        ts("end_timestamp_gmt", "Measurement window end (UTC).", "endTimestampGMT"),
        tsl("start_timestamp_local", "Window start, local.", "startTimestampLocal"),
        tsl("end_timestamp_local", "Window end, local.", "endTimestampLocal"),
        ts("sleep_start_timestamp_gmt", "Sleep start (UTC).", "sleepStartTimestampGMT"),
        ts("sleep_end_timestamp_gmt", "Sleep end (UTC).", "sleepEndTimestampGMT"),
        tsl("sleep_start_timestamp_local", "Sleep start, local.", "sleepStartTimestampLocal"),
        tsl("sleep_end_timestamp_local", "Sleep end, local.", "sleepEndTimestampLocal"),
        expr("reading_count", "INTEGER", "Number of 5-minute readings (0 when archived).", "coalesce(json_array_length(b->'hrvReadings'), 0)", "hrvReadings"),
        fetched_at,
    ],
)

hrv_reading_table = Table(
    name="hrv_reading",
    comment="5-minute overnight HRV readings (hrvReadings).",
    primary_key=["calendar_date", "reading_time_gmt"],
    columns=[
        cal_date,
        ts("reading_time_gmt", "Reading time (UTC).", "readingTimeGMT"),
        tsl("reading_time_local", "Reading time, local.", "readingTimeLocal"),
        col("hrv_value", "INTEGER", "HRV (RMSSD) in ms.", "hrvValue"),
        fetched_at,
    ],
)

training_readiness_table = Table(
    name="training_readiness",
    comment="Training readiness scores (GET metrics/trainingreadiness; several per day as it updates).",
    primary_key=["calendar_date", "timestamp"],
    columns=[
        col("calendar_date", "DATE", "Calendar day.", "calendarDate"),
        ts("timestamp", "Computed at (UTC).", "timestamp"),
        tsl("timestamp_local", "Computed at, local.", "timestampLocal"),
        col("user_profile_pk", "BIGINT", "User profile id.", "userProfilePK"),
        col("device_id", "BIGINT", "Device id.", "deviceId"),
        col("level", "VARCHAR", "LOW, MODERATE, HIGH, PRIME ...", "level"),
        col("feedback_long", "VARCHAR", "Long feedback key.", "feedbackLong"),
        col("feedback_short", "VARCHAR", "Short feedback key.", "feedbackShort"),
        col("score", "INTEGER", "Readiness score (0-100).", "score"),
        col("sleep_score", "INTEGER", "Sleep score input.", "sleepScore"),
        col("sleep_score_factor_percent", "INTEGER", "Sleep factor %.", "sleepScoreFactorPercent"),
        col("sleep_score_factor_feedback", "VARCHAR", "Sleep factor feedback.", "sleepScoreFactorFeedback"),
        col("recovery_time", "INTEGER", "Recovery time remaining in minutes.", "recoveryTime"),
        col("recovery_time_factor_percent", "INTEGER", "Recovery factor %.", "recoveryTimeFactorPercent"),
        col("recovery_time_factor_feedback", "VARCHAR", "Recovery factor feedback.", "recoveryTimeFactorFeedback"),
        col("acwr_factor_percent", "INTEGER", "Acute/chronic load factor %.", "acwrFactorPercent"),
        col("acwr_factor_feedback", "VARCHAR", "ACWR feedback.", "acwrFactorFeedback"),
        col("acute_load", "INTEGER", "Acute load.", "acuteLoad"),
        col("stress_history_factor_percent", "INTEGER", "Stress history factor %.", "stressHistoryFactorPercent"),
        col("stress_history_factor_feedback", "VARCHAR", "Stress history feedback.", "stressHistoryFactorFeedback"),
        col("hrv_factor_percent", "INTEGER", "HRV factor %.", "hrvFactorPercent"),
        col("hrv_factor_feedback", "VARCHAR", "HRV feedback.", "hrvFactorFeedback"),
        col("hrv_weekly_average", "INTEGER", "7-day HRV average in ms.", "hrvWeeklyAverage"),
        col("sleep_history_factor_percent", "INTEGER", "Sleep history factor %.", "sleepHistoryFactorPercent"),
        col("sleep_history_factor_feedback", "VARCHAR", "Sleep history feedback.", "sleepHistoryFactorFeedback"),
        col("valid_sleep", "BOOLEAN", "Sleep input valid.", "validSleep"),
        col("input_context", "VARCHAR", "Input context (e.g. AFTER_SLEEP, AFTER_ACTIVITY).", "inputContext"),
        col("primary_activity_tracker", "BOOLEAN", "From the primary tracker.", "primaryActivityTracker"),
        col("recovery_time_change_phrase", "VARCHAR", "Recovery time change phrase.", "recoveryTimeChangePhrase"),
        fetched_at,
    ],
)

training_status_table = Table(
    name="training_status",
    comment="Daily aggregated training status (GET metrics/trainingstatus/aggregated/{date}): VO2 max, load balance and training status of the primary device, plus heat/altitude acclimation.",
    primary_key=["calendar_date"],
    columns=[
        cal_date,
        col("user_id", "BIGINT", "User id.", "userId"),
        nested("vo2max_date", "DATE", "Date of the VO2 max estimate.", "mostRecentVO2Max.generic.calendarDate", "mostRecentVO2Max"),
        nested("vo2max_precise", "DOUBLE", "Running VO2 max (precise) in ml/kg/min.", "mostRecentVO2Max.generic.vo2MaxPreciseValue", "mostRecentVO2Max"),
        nested("vo2max", "DOUBLE", "Running VO2 max in ml/kg/min.", "mostRecentVO2Max.generic.vo2MaxValue", "mostRecentVO2Max"),
        nested("vo2max_cycling_precise", "DOUBLE", "Cycling VO2 max (precise).", "mostRecentVO2Max.cycling.vo2MaxPreciseValue", "mostRecentVO2Max"),
        nested("vo2max_cycling", "DOUBLE", "Cycling VO2 max.", "mostRecentVO2Max.cycling.vo2MaxValue", "mostRecentVO2Max"),
        nested("fitness_age", "DOUBLE", "Fitness age.", "mostRecentVO2Max.generic.fitnessAge", "mostRecentVO2Max"),
        col("most_recent_vo2max", "JSON", "Full mostRecentVO2Max block (generic, cycling, heatAltitudeAcclimation).", "mostRecentVO2Max"),
        expr("load_balance", "JSON", "Training load balance of the primary device (monthly aerobic low/high and anaerobic loads with targets).", "(SELECT first(value) FROM json_each(b->'mostRecentTrainingLoadBalance'->'metricsTrainingLoadBalanceDTOMap'))", "mostRecentTrainingLoadBalance"),
        expr("status_data", "JSON", "Training status of the primary device (status, fitness trend, acute load, feedback).", "(SELECT first(value) FROM json_each(b->'mostRecentTrainingStatus'->'latestTrainingStatusData'))", "mostRecentTrainingStatus"),
        expr("training_status_code", "INTEGER", "Training status code (1 productive, 2 maintaining, 3 recovery, 4 unproductive, 5 detraining, 6 peaking, 7 overreaching, 8 no status, 9 strained, 10 paused ...).", "TRY_CAST((SELECT first(value) FROM json_each(b->'mostRecentTrainingStatus'->'latestTrainingStatusData'))->>'trainingStatus' AS INTEGER)", "mostRecentTrainingStatus"),
        expr("training_status_feedback_phrase", "VARCHAR", "Feedback phrase key.", "(SELECT first(value) FROM json_each(b->'mostRecentTrainingStatus'->'latestTrainingStatusData'))->>'trainingStatusFeedbackPhrase'", "mostRecentTrainingStatus"),
        expr("acute_training_load", "JSON", "acuteTrainingLoadDTO (ACWR, daily and chronic loads).", "(SELECT first(value) FROM json_each(b->'mostRecentTrainingStatus'->'latestTrainingStatusData'))->'acuteTrainingLoadDTO'", "mostRecentTrainingStatus"),
        expr("recorded_devices", "JSON", "Devices contributing.", "b->'mostRecentTrainingStatus'->'recordedDevices'", "mostRecentTrainingStatus"),
        col("heat_altitude_acclimation", "JSON", "heatAltitudeAcclimationDTO.", "heatAltitudeAcclimationDTO"),
        fetched_at,
    ],
)

max_metrics_table = Table(
    name="max_metrics",
    comment="VO2 max and heat/altitude acclimation per day (GET metrics/maxmet/daily/{start}/{end}); only days with a new estimate have a row.",
    primary_key=["calendar_date"],
    columns=[
        cal_date,
        col("user_id", "BIGINT", "User id.", "userId"),
        nested("vo2max_precise", "DOUBLE", "Running VO2 max (precise) in ml/kg/min.", "generic.vo2MaxPreciseValue", "generic"),
        nested("vo2max", "DOUBLE", "Running VO2 max.", "generic.vo2MaxValue", "generic"),
        nested("fitness_age", "DOUBLE", "Fitness age.", "generic.fitnessAge", "generic"),
        nested("fitness_age_description", "VARCHAR", "Fitness age description.", "generic.fitnessAgeDescription", "generic"),
        nested("max_met_category", "INTEGER", "Max MET category.", "generic.maxMetCategory", "generic"),
        nested("vo2max_cycling_precise", "DOUBLE", "Cycling VO2 max (precise).", "cycling.vo2MaxPreciseValue", "cycling"),
        nested("vo2max_cycling", "DOUBLE", "Cycling VO2 max.", "cycling.vo2MaxValue", "cycling"),
        nested("heat_acclimation_percentage", "INTEGER", "Heat acclimation in %.", "heatAltitudeAcclimation.heatAcclimationPercentage", "heatAltitudeAcclimation"),
        nested("altitude_acclimation", "INTEGER", "Altitude acclimation in meters.", "heatAltitudeAcclimation.altitudeAcclimation", "heatAltitudeAcclimation"),
        nested("current_altitude", "INTEGER", "Current altitude in meters.", "heatAltitudeAcclimation.currentAltitude", "heatAltitudeAcclimation"),
        nested("heat_trend", "VARCHAR", "Heat trend.", "heatAltitudeAcclimation.heatTrend", "heatAltitudeAcclimation"),
        nested("altitude_trend", "VARCHAR", "Altitude trend.", "heatAltitudeAcclimation.altitudeTrend", "heatAltitudeAcclimation"),
        col("heat_altitude_acclimation", "JSON", "Full acclimation block.", "heatAltitudeAcclimation"),
        fetched_at,
    ],
)

endurance_score_table = Table(
    name="endurance_score",
    comment="Endurance score per day (GET metrics/endurancescore/stats range, groupMap).",
    primary_key=["calendar_date"],
    columns=[
        cal_date,
        col("group_average", "INTEGER", "Average endurance score of the day.", "groupAverage"),
        col("group_max", "INTEGER", "Max endurance score of the day.", "groupMax"),
        col("contributors", "JSON", "Contribution by activity type/group in %.", "enduranceContributorDTOList"),
        fetched_at,
    ],
)

hill_score_table = Table(
    name="hill_score",
    comment="Hill score per day (GET metrics/hillscore/stats range).",
    primary_key=["calendar_date"],
    columns=[
        col("calendar_date", "DATE", "Calendar day.", "calendarDate"),
        col("user_profile_pk", "BIGINT", "User profile id.", "userProfilePK"),
        col("device_id", "BIGINT", "Device id.", "deviceId"),
        col("strength_score", "INTEGER", "Hill strength.", "strengthScore"),
        col("endurance_score", "INTEGER", "Hill endurance.", "enduranceScore"),
        col("overall_score", "INTEGER", "Overall hill score.", "overallScore"),
        col("hill_score_classification_id", "INTEGER", "Classification id.", "hillScoreClassificationId"),
        col("hill_score_feedback_phrase_id", "INTEGER", "Feedback phrase id.", "hillScoreFeedbackPhraseId"),
        col("vo2max", "DOUBLE", "VO2 max used.", "vo2Max"),
        col("vo2max_precise", "DOUBLE", "VO2 max precise.", "vo2MaxPreciseValue"),
        col("primary_training_device", "BOOLEAN", "From the primary training device.", "primaryTrainingDevice"),
        fetched_at,
    ],
)

fitness_age_table = Table(
    name="fitness_age",
    comment="Fitness age per day (GET fitnessage-service/stats/daily range).",
    primary_key=["calendar_date"],
    columns=[
        col("calendar_date", "DATE", "Calendar day.", "calendarDate"),
        nested("fitness_age", "DOUBLE", "Fitness age in years.", "values.fitnessAge", "values"),
        nested("achievable_fitness_age", "DOUBLE", "Achievable fitness age.", "values.achievableFitnessAge", "values"),
        nested("vigorous_days_avg", "DOUBLE", "Average vigorous days per week.", "values.vigorousDaysAvg", "values"),
        nested("rhr", "INTEGER", "Resting HR used in bpm.", "values.rhr", "values"),
        nested("bmi", "DOUBLE", "BMI used.", "values.bmi", "values"),
        fetched_at,
    ],
)

sleep_stats_table = Table(
    name="sleep_stats",
    comment="Compact sleep statistics per day from the range endpoint sleep-service/stats/sleep/daily (fills days whose full sleep payload is not fetched yet).",
    primary_key=["calendar_date"],
    columns=[
        col("calendar_date", "DATE", "Calendar day.", "calendarDate"),
        nested("sleep_score", "INTEGER", "Sleep score.", "values.sleepScore", "values"),
        nested("sleep_score_quality", "VARCHAR", "Score qualifier.", "values.sleepScoreQuality", "values"),
        nested("total_sleep_seconds", "INTEGER", "Total sleep in seconds.", "values.totalSleepTimeInSeconds", "values"),
        nested("deep_seconds", "INTEGER", "Deep in seconds.", "values.deepTime", "values"),
        nested("light_seconds", "INTEGER", "Light in seconds.", "values.lightTime", "values"),
        nested("rem_seconds", "INTEGER", "REM in seconds.", "values.remTime", "values"),
        nested("awake_seconds", "INTEGER", "Awake in seconds.", "values.awakeTime", "values"),
        nested("sleep_need", "INTEGER", "Sleep need in minutes.", "values.sleepNeed", "values"),
        nested("resting_heart_rate", "INTEGER", "Resting HR in bpm.", "values.restingHeartRate", "values"),
        nested("avg_heart_rate", "DOUBLE", "Average HR in bpm.", "values.avgHeartRate", "values"),
        nested("respiration", "DOUBLE", "Average respiration.", "values.respiration", "values"),
        nested("spo2", "DOUBLE", "Average SpO2.", "values.spO2", "values"),
        nested("body_battery_change", "INTEGER", "Body Battery change.", "values.bodyBatteryChange", "values"),
        nested("hrv_status", "VARCHAR", "HRV status.", "values.hrvStatus", "values"),
        nested("avg_overnight_hrv", "DOUBLE", "Average overnight HRV in ms.", "values.avgOvernightHrv", "values"),
        nested("hrv_7d_average", "DOUBLE", "7-day HRV average.", "values.hrv7dAverage", "values"),
        nested("skin_temp_c", "DOUBLE", "Skin temperature deviation in C.", "values.skinTempC", "values"),
        nested_ms("sleep_start_gmt", "Sleep start (UTC).", "values.gmtSleepStartTimeInMillis", "values"),
        nested_ms("sleep_end_gmt", "Sleep end (UTC).", "values.gmtSleepEndTimeInMillis", "values"),
        col("values", "JSON", "All values.", "values"),
        fetched_at,
    ],
)

steps_daily_table = Table(
    name="steps_daily",
    comment="Steps, distance and goal per day (GET usersummary-service/stats/steps/daily range).",
    primary_key=["calendar_date"],
    columns=[
        col("calendar_date", "DATE", "Calendar day.", "calendarDate"),
        col("total_steps", "INTEGER", "Steps.", "totalSteps"),
        col("total_distance", "INTEGER", "Distance in meters.", "totalDistance"),
        col("step_goal", "INTEGER", "Step goal.", "stepGoal"),
        fetched_at,
    ],
)

wellness_metric_table = Table(
    name="wellness_metric",
    comment="Daily wellness metrics from userstats-service in long form (metric key such as WELLNESS_RESTING_HEART_RATE, WELLNESS_BMR_CALORIES, WELLNESS_ACTIVE_CALORIES).",
    primary_key=["metric", "calendar_date"],
    columns=[
        expr("metric", "VARCHAR", "Metric key.", "src.metric"),
        col("calendar_date", "DATE", "Calendar day.", "calendarDate"),
        col("value", "DOUBLE", "Value (bpm or kcal).", "value"),
        fetched_at,
    ],
)

hydration_table = Table(
    name="hydration",
    comment="Hydration log per day (GET usersummary/hydration/daily/{date}).",
    primary_key=["calendar_date"],
    columns=[
        col("calendar_date", "DATE", "Calendar day.", "calendarDate"),
        col("user_id", "BIGINT", "User id.", "userId"),
        col("value_in_ml", "DOUBLE", "Intake in ml.", "valueInML"),
        col("goal_in_ml", "DOUBLE", "Goal in ml.", "goalInML"),
        col("daily_average_in_ml", "DOUBLE", "Daily average in ml.", "dailyAverageinML"),
        tsl("last_entry_timestamp_local", "Last entry, local.", "lastEntryTimestampLocal"),
        col("sweat_loss_in_ml", "DOUBLE", "Estimated sweat loss in ml.", "sweatLossInML"),
        col("activity_intake_in_ml", "DOUBLE", "Intake logged during activities in ml.", "activityIntakeInML"),
        fetched_at,
    ],
)

all_day_event_table = Table(
    name="all_day_event",
    comment="Auto-detected and recorded activity events of the day (GET wellness/dailyEvents).",
    primary_key=["calendar_date", "start_timestamp_gmt"],
    columns=[
        col("calendar_date", "DATE", "Calendar day.", "calendarDate"),
        col("user_profile_pk", "BIGINT", "User profile id.", "userProfilePk"),
        col("device_id", "BIGINT", "Device id.", "deviceId"),
        ts("start_timestamp_gmt", "Start (UTC).", "startTimestampGMT"),
        ts("end_timestamp_gmt", "End (UTC).", "endTimestampGMT"),
        tsl("start_timestamp_local", "Start, local.", "startTimestampLocal"),
        tsl("end_timestamp_local", "End, local.", "endTimestampLocal"),
        col("duration", "INTEGER", "Duration in minutes.", "duration"),
        col("activity_type", "VARCHAR", "Activity type.", "activityType"),
        col("activity_sub_type", "VARCHAR", "Activity subtype.", "activitySubType"),
        col("moderate_intensity_minutes", "INTEGER", "Moderate minutes.", "moderateIntensityMinutes"),
        col("vigorous_intensity_minutes", "INTEGER", "Vigorous minutes.", "vigorousIntensityMinutes"),
        fetched_at,
    ],
)

nap_table = Table(
    name="nap",
    comment="Naps per day (GET sleep-service/sleep/naps/{date}).",
    primary_key=["calendar_date", "nap_start_timestamp_gmt"],
    columns=[
        col("calendar_date", "DATE", "Calendar day.", "calendarDate"),
        col("user_profile_pk", "BIGINT", "User profile id.", "userProfilePK"),
        col("device_id", "BIGINT", "Device id.", "deviceId"),
        col("nap_time_sec", "INTEGER", "Nap duration in seconds.", "napTimeSec"),
        ts("nap_start_timestamp_gmt", "Nap start (UTC).", "napStartTimestampGMT"),
        ts("nap_end_timestamp_gmt", "Nap end (UTC).", "napEndTimestampGMT"),
        col("nap_feedback", "VARCHAR", "Feedback key.", "napFeedback"),
        col("nap_source", "INTEGER", "Source (0 device).", "napSource"),
        col("nap_start_time_offset", "INTEGER", "Offset from UTC in minutes at start.", "napStartTimeOffset"),
        col("nap_end_time_offset", "INTEGER", "Offset from UTC in minutes at end.", "napEndTimeOffset"),
        ts("create_timestamp_gmt", "Created (UTC).", "createTimestampGMT"),
        ts("update_timestamp_gmt", "Updated (UTC).", "updateTimestampGMT"),
        fetched_at,
    ],
)

lifestyle_logging_table = Table(
    name="lifestyle_logging",
    comment="Lifestyle logging (daily logs and completion stats) per day.",
    primary_key=["calendar_date"],
    columns=[
        col("calendar_date", "DATE", "Calendar day.", "calendarDate"),
        col("daily_logs_report", "JSON", "Logged lifestyle items.", "dailyLogsReport"),
        col("completion_stats", "JSON", "Completion statistics.", "completionStats"),
        fetched_at,
    ],
)

cycling_ability_table = Table(
    name="cycling_ability",
    comment="Cycling ability profile per day (GET metrics/cycling-ability/latest).",
    primary_key=["calendar_date"],
    columns=[
        col("calendar_date", "DATE", "Calendar day.", "calendarDate"),
        col("user_profile_pk", "BIGINT", "User profile id.", "userProfilePk"),
        col("device_id", "BIGINT", "Device id.", "deviceId"),
        ts("timestamp", "Computed at (UTC).", "timestamp"),
        col("primary_training_device", "BOOLEAN", "From primary device.", "primaryTrainingDevice"),
        col("aerobic_endurance", "INTEGER", "Aerobic endurance score.", "aerobicEndurance"),
        col("aerobic_capacity", "INTEGER", "Aerobic capacity score.", "aerobicCapacity"),
        col("anaerobic_capacity", "INTEGER", "Anaerobic capacity score.", "anaerobicCapacity"),
        col("profile_type", "VARCHAR", "Rider profile type.", "profileType"),
        col("profile_type_feedback", "VARCHAR", "Profile feedback.", "profileTypeFeedback"),
        col("aerobic_endurance_feedback", "VARCHAR", "Feedback.", "aerobicEnduranceFeedback"),
        col("aerobic_capacity_feedback", "VARCHAR", "Feedback.", "aerobicCapacityFeedback"),
        col("anaerobic_capacity_feedback", "VARCHAR", "Feedback.", "anaerobicCapacityFeedback"),
        fetched_at,
    ],
)

running_tolerance_table = Table(
    name="running_tolerance",
    comment="Running tolerance per day (GET metrics/runningtolerance/stats); raw rows as JSON because the shape varies by device.",
    primary_key=["calendar_date"],
    columns=[cal_date, expr("body", "JSON", "Payload for the day.", "b"), fetched_at],
)

weigh_in_table = Table(
    name="weigh_in",
    comment="Weight and body composition measurements (GET weight-service/weight/range, dailyWeightSummaries[].allWeightMetrics).",
    primary_key=["sample_pk"],
    columns=[
        col("sample_pk", "BIGINT", "Sample id.", "samplePk"),
        col("calendar_date", "DATE", "Calendar day.", "calendarDate"),
        ms("date", "Measurement time (UTC epoch ms as sent).", "date"),
        ts("timestamp_gmt", "Measurement time (UTC).", "timestampGMT"),
        col("weight", "DOUBLE", "Weight in grams.", "weight"),
        col("bmi", "DOUBLE", "BMI.", "bmi"),
        col("body_fat", "DOUBLE", "Body fat in %.", "bodyFat"),
        col("body_water", "DOUBLE", "Body water in %.", "bodyWater"),
        col("bone_mass", "DOUBLE", "Bone mass in grams.", "boneMass"),
        col("muscle_mass", "DOUBLE", "Muscle mass in grams.", "muscleMass"),
        col("physique_rating", "DOUBLE", "Physique rating.", "physiqueRating"),
        col("visceral_fat", "DOUBLE", "Visceral fat rating.", "visceralFat"),
        col("metabolic_age", "DOUBLE", "Metabolic age.", "metabolicAge"),
        col("source_type", "VARCHAR", "Source (MANUAL, INDEX_SCALE, ...).", "sourceType"),
        col("weight_delta", "DOUBLE", "Change from previous in grams.", "weightDelta"),
        extra_col("Other fields.", None),
        fetched_at,
    ],
)

blood_pressure_table = Table(
    name="blood_pressure",
    comment="Blood pressure measurements (GET bloodpressure-service/bloodpressure/range, measurementSummaries[].measurements).",
    primary_key=["version"],
    columns=[
        col("version", "BIGINT", "Measurement version id.", "version"),
        col("systolic", "INTEGER", "Systolic in mmHg.", "systolic"),
        col("diastolic", "INTEGER", "Diastolic in mmHg.", "diastolic"),
        col("pulse", "INTEGER", "Pulse in bpm.", "pulse"),
        ts("measurement_timestamp_gmt", "Measured (UTC).", "measurementTimestampGMT"),
        tsl("measurement_timestamp_local", "Measured, local.", "measurementTimestampLocal"),
        col("notes", "VARCHAR", "Notes.", "notes"),
        col("source_type", "VARCHAR", "Source.", "sourceType"),
        col("category", "VARCHAR", "Category.", "category"),
        col("category_name", "VARCHAR", "Category name.", "categoryName"),
        extra_col("Other fields.", None),
        fetched_at,
    ],
)

# ---------------------------------------------------------------------------------
# Profile, devices, gear, zones, records, badges, goals, workouts, courses, calendar
# ---------------------------------------------------------------------------------

user_settings_table = Table(
    name="user_settings",
    comment="The user's Garmin Connect settings (GET userprofile-service/userprofile/user-settings): biometrics, formats, sleep windows.",
    primary_key=["id"],
    columns=[
        col("id", "BIGINT", "User profile id.", "id"),
        nested("gender", "VARCHAR", "Gender.", "userData.gender", "userData"),
        nested("weight", "DOUBLE", "Weight in grams.", "userData.weight", "userData"),
        nested("height", "DOUBLE", "Height in cm.", "userData.height", "userData"),
        nested("birth_date", "DATE", "Birth date.", "userData.birthDate", "userData"),
        nested("measurement_system", "VARCHAR", "metric or statute_us.", "userData.measurementSystem", "userData"),
        nested("time_format", "VARCHAR", "Time format.", "userData.timeFormat", "userData"),
        nested("activity_level", "INTEGER", "Activity class (0-10).", "userData.activityLevel", "userData"),
        nested("handedness", "VARCHAR", "Handedness.", "userData.handedness", "userData"),
        nested("vo2max_running", "DOUBLE", "Running VO2 max.", "userData.vo2MaxRunning", "userData"),
        nested("vo2max_cycling", "DOUBLE", "Cycling VO2 max.", "userData.vo2MaxCycling", "userData"),
        nested("lactate_threshold_speed", "DOUBLE", "LT speed in m/s.", "userData.lactateThresholdSpeed", "userData"),
        nested("lactate_threshold_heart_rate", "INTEGER", "LT heart rate in bpm.", "userData.lactateThresholdHeartRate", "userData"),
        nested("intensity_minutes_calc_method", "VARCHAR", "Intensity minutes method.", "userData.intensityMinutesCalcMethod", "userData"),
        nested("moderate_intensity_minutes_hr_zone", "INTEGER", "HR zone counted as moderate.", "userData.moderateIntensityMinutesHrZone", "userData"),
        nested("vigorous_intensity_minutes_hr_zone", "INTEGER", "HR zone counted as vigorous.", "userData.vigorousIntensityMinutesHrZone", "userData"),
        nested("hydration_measurement_unit", "VARCHAR", "Hydration unit.", "userData.hydrationMeasurementUnit", "userData"),
        nested("hydration_auto_goal_enabled", "BOOLEAN", "Auto hydration goal.", "userData.hydrationAutoGoalEnabled", "userData"),
        nested("threshold_heart_rate_auto_detected", "BOOLEAN", "LT HR auto-detected.", "userData.thresholdHeartRateAutoDetected", "userData"),
        nested("ftp_auto_detected", "BOOLEAN", "FTP auto-detected.", "userData.ftpAutoDetected", "userData"),
        nested("available_training_days", "JSON", "Training days available.", "userData.availableTrainingDays", "userData"),
        nested("preferred_long_training_days", "JSON", "Preferred long-session days.", "userData.preferredLongTrainingDays", "userData"),
        col("user_data", "JSON", "Full userData block.", "userData"),
        nested("sleep_time", "INTEGER", "Usual bedtime in seconds from midnight.", "userSleep.sleepTime", "userSleep"),
        nested("wake_time", "INTEGER", "Usual wake time in seconds from midnight.", "userSleep.wakeTime", "userSleep"),
        col("user_sleep", "JSON", "userSleep block.", "userSleep"),
        col("user_sleep_windows", "JSON", "Sleep windows per weekday.", "userSleepWindows"),
        col("connect_date", "VARCHAR", "Connect date.", "connectDate"),
        col("source_type", "VARCHAR", "Source type.", "sourceType"),
        fetched_at,
    ],
)

user_profile_table = Table(
    name="user_profile",
    comment="The user's profile: display name, locale, units, time zone (merged from userprofile settings, userProfileBase and personal-information).",
    primary_key=["user_profile_pk"],
    columns=[
        expr("user_profile_pk", "BIGINT", "User profile id.", "TRY_CAST(src.base->>'userProfilePk' AS BIGINT)"),
        expr("display_name", "VARCHAR", "Display name (the uuid-like id used in URLs).", "coalesce(src.settings->>'displayName', src.base->>'displayName')"),
        expr("user_name", "VARCHAR", "User name.", "src.base->>'userName'"),
        expr("first_name", "VARCHAR", "First name.", "src.base->>'firstName'"),
        expr("last_name", "VARCHAR", "Last name.", "src.base->>'lastName'"),
        expr("email_address", "VARCHAR", "Email.", "src.base->>'emailAddress'"),
        expr("birth_date", "DATE", "Birth date.", "TRY_CAST(src.base->>'birthDate' AS DATE)"),
        expr("gender", "VARCHAR", "Gender.", "src.base->>'gender'"),
        expr("create_date", "TIMESTAMPTZ", "Account created (UTC).", "TRY_CAST(left(src.base->>'createDate', 23) AS TIMESTAMP)::TIMESTAMPTZ"),
        expr("country_code", "VARCHAR", "Country code.", "src.base->>'countryCode'"),
        expr("preferred_locale", "VARCHAR", "Locale.", "src.settings->>'preferredLocale'"),
        expr("measurement_system", "VARCHAR", "metric or statute_us.", "src.settings->>'measurementSystem'"),
        expr("time_zone", "VARCHAR", "IANA time zone (Garmin evaluates calendar days in it).", "coalesce(src.settings->>'timeZone', src.info->>'timeZone')"),
        expr("first_day_of_week", "VARCHAR", "First day of the week.", "src.settings->'firstDayOfWeek'->>'dayName'"),
        expr("number_format", "VARCHAR", "Number format.", "src.settings->>'numberFormat'"),
        expr("time_format", "VARCHAR", "Time format key.", "src.settings->'timeFormat'->>'formatKey'"),
        expr("date_format", "VARCHAR", "Date format key.", "src.settings->'dateFormat'->>'formatKey'"),
        expr("power_format", "VARCHAR", "Power format key.", "src.settings->'powerFormat'->>'formatKey'"),
        expr("heart_rate_format", "VARCHAR", "Heart rate format key.", "src.settings->'heartRateFormat'->>'formatKey'"),
        expr("hydration_measurement_unit", "VARCHAR", "Hydration unit.", "src.settings->>'hydrationMeasurementUnit'"),
        expr("settings", "JSON", "Full userprofile settings payload.", "src.settings"),
        expr("profile_base", "JSON", "Full userProfileBase payload.", "src.base"),
        expr("personal_information", "JSON", "Full personal-information payload (biometricProfile, userInfo).", "src.info"),
        expr("privacy_settings", "JSON", "Full privacy-settings payload.", "src.privacy"),
        fetched_at,
    ],
)

device_table = Table(
    name="device",
    comment="Registered devices (GET device-service/deviceregistration/devices and /historical). Capability flags stay in capabilities JSON; settings in device_settings.",
    primary_key=["device_id"],
    columns=[
        col("device_id", "BIGINT", "Device id.", "deviceId"),
        col("unit_id", "BIGINT", "Unit id (serial).", "unitId"),
        col("product_display_name", "VARCHAR", "Product name.", "productDisplayName"),
        col("display_name", "VARCHAR", "Display name.", "displayName"),
        col("device_type_pk", "INTEGER", "Device type pk.", "deviceTypePk"),
        col("device_type_simple_name", "VARCHAR", "Simple type name.", "deviceTypeSimpleName"),
        col("application_key", "VARCHAR", "Application key.", "applicationKey"),
        col("part_number", "VARCHAR", "Part number.", "partNumber"),
        col("product_sku", "VARCHAR", "SKU.", "productSku"),
        col("serial_number", "VARCHAR", "Serial number.", "serialNumber"),
        col("software_version", "VARCHAR", "Firmware version.", "currentFirmwareVersion"),
        col("image_url", "VARCHAR", "Image URL.", "imageUrl"),
        col("primary_activity_tracker_indicator", "BOOLEAN", "Primary activity tracker.", "primaryActivityTrackerIndicator"),
        col("primary_training_device", "BOOLEAN", "Primary training device.", "primaryTrainingDevice"),
        col("device_categories", "JSON", "Categories.", "deviceCategories"),
        col("wellness", "BOOLEAN", "Wellness device.", "wellness"),
        col("weight_scale", "BOOLEAN", "Weight scale.", "weightScale"),
        col("wifi", "BOOLEAN", "Has WiFi.", "wifi"),
        col("has_optical_heart_rate", "BOOLEAN", "Has optical HR.", "hasOpticalHeartRate"),
        col("device_tags", "JSON", "Tags.", "deviceTags"),
        expr("historical", "BOOLEAN", "Listed only among historical (no longer registered) devices.", "src.historical"),
        expr("capabilities", "JSON", "All *Capable flags and other fields of the registration object.", "b"),
        fetched_at,
    ],
)

device_settings_table = Table(
    name="device_settings",
    comment="Device settings (GET device-service/deviceservice/device-info/settings/{id}); scalar basics plus the full payload.",
    primary_key=["device_id"],
    columns=[
        col("device_id", "BIGINT", "Device id.", "deviceId"),
        col("time_format", "VARCHAR", "Time format.", "timeFormat"),
        col("date_format", "VARCHAR", "Date format.", "dateFormat"),
        col("measurement_units", "VARCHAR", "Units.", "measurementUnits"),
        col("language", "INTEGER", "Language id.", "language"),
        col("start_of_week", "VARCHAR", "Start of week.", "startOfWeek"),
        col("optical_heart_rate_enabled", "BOOLEAN", "OHR on.", "opticalHeartRateEnabled"),
        col("dnd_enabled", "BOOLEAN", "Do not disturb.", "dndEnabled"),
        col("alarms", "JSON", "Alarms.", "alarms"),
        col("activity_tracking", "JSON", "Activity tracking block (pulse ox sleep tracking, alerts).", "activityTracking"),
        col("auto_activity_detect", "JSON", "Auto activity detection block.", "autoActivityDetect"),
        expr("settings", "JSON", "Full settings payload.", "b"),
        fetched_at,
    ],
)

device_last_used_table = Table(
    name="device_last_used",
    comment="Last used device and its last upload time (GET device-service/deviceservice/mylastused); polled every 15 minutes to detect new syncs.",
    primary_key=["user_device_id"],
    columns=[
        col("user_device_id", "BIGINT", "Device id.", "userDeviceId"),
        col("user_profile_number", "BIGINT", "User profile id.", "userProfileNumber"),
        col("application_number", "BIGINT", "Application number.", "applicationNumber"),
        col("last_used_device_application_key", "VARCHAR", "Application key.", "lastUsedDeviceApplicationKey"),
        col("last_used_device_name", "VARCHAR", "Device name.", "lastUsedDeviceName"),
        ms("last_used_device_upload_time", "Last upload (UTC).", "lastUsedDeviceUploadTime"),
        col("image_url", "VARCHAR", "Image URL.", "imageUrl"),
        col("released", "BOOLEAN", "Released.", "released"),
        fetched_at,
    ],
)

sensor_table = Table(
    name="sensor",
    comment="Paired sensors (GET device-service/sensors).",
    primary_key=["serial_number", "sensor_type"],
    columns=[
        col("serial_number", "BIGINT", "Sensor serial.", "serialNumber"),
        col("sensor_type", "VARCHAR", "Sensor type.", "sensorType"),
        col("device_name", "VARCHAR", "Name.", "deviceName"),
        col("image_url", "VARCHAR", "Image URL.", "imageUrl"),
        col("battery_status", "VARCHAR", "Battery status.", "batteryStatus"),
        col("battery_level", "INTEGER", "Battery level.", "batteryLevel"),
        col("priority_sensor", "BOOLEAN", "Priority sensor.", "prioritySensor"),
        col("product_id", "INTEGER", "Product id.", "productId"),
        col("part_number", "VARCHAR", "Part number.", "partNumber"),
        col("software_version", "VARCHAR", "Software version.", "softwareVersion"),
        ts("last_connected", "Last connected (UTC).", "lastConnected"),
        ts("last_low_battery_notification", "Last low battery notification (UTC).", "lastLowBatteryNotification"),
        col("manufacturer", "VARCHAR", "Manufacturer.", "manufacturer"),
        col("rechargeable_sensor_capable", "BOOLEAN", "Rechargeable.", "rechargeableSensorCapable"),
        fetched_at,
    ],
)

gear_table = Table(
    name="gear",
    comment="Gear (shoes, bikes, ...) with usage stats (GET gear-service/gear/filterGear and /stats/{uuid}).",
    primary_key=["uuid"],
    columns=[
        col("uuid", "VARCHAR", "Gear uuid.", "uuid"),
        col("gear_pk", "BIGINT", "Gear pk.", "gearPk"),
        col("user_profile_pk", "BIGINT", "User profile id.", "userProfilePk"),
        col("gear_make_name", "VARCHAR", "Make.", "gearMakeName"),
        col("gear_model_name", "VARCHAR", "Model.", "gearModelName"),
        col("gear_type_name", "VARCHAR", "Type.", "gearTypeName"),
        col("gear_status_name", "VARCHAR", "active or retired.", "gearStatusName"),
        col("display_name", "VARCHAR", "Name.", "displayName"),
        col("custom_make_model", "VARCHAR", "Custom make/model.", "customMakeModel"),
        col("image_name_large", "VARCHAR", "Image.", "imageNameLarge"),
        col("image_name_medium", "VARCHAR", "Image.", "imageNameMedium"),
        col("image_name_small", "VARCHAR", "Image.", "imageNameSmall"),
        ts("date_begin", "In use since (UTC).", "dateBegin"),
        ts("date_end", "Retired (UTC).", "dateEnd"),
        col("maximum_meters", "DOUBLE", "Max distance in meters.", "maximumMeters"),
        col("notified", "BOOLEAN", "Max distance notified.", "notified"),
        ts("create_date", "Created (UTC).", "createDate"),
        ts("update_date", "Updated (UTC).", "updateDate"),
        extra_col("Other fields.", None),
        expr("stats", "JSON", "Usage stats payload (total distance, activities, ...).", "src.stats"),
        fetched_at,
    ],
)

heart_rate_zones_table = Table(
    name="heart_rate_zones",
    comment="Configured heart-rate zones per sport (GET biometric-service/heartRateZones).",
    primary_key=["sport"],
    columns=[
        col("sport", "VARCHAR", "DEFAULT, RUNNING, CYCLING, SWIMMING.", "sport"),
        col("training_method", "VARCHAR", "Method (HR_RESERVE, MAX_HR, LT_HR, ...).", "trainingMethod"),
        col("resting_heart_rate_used", "INTEGER", "Resting HR in bpm.", "restingHeartRateUsed"),
        col("lactate_threshold_heart_rate_used", "INTEGER", "LT HR in bpm.", "lactateThresholdHeartRateUsed"),
        col("max_heart_rate_used", "INTEGER", "Max HR in bpm.", "maxHeartRateUsed"),
        *[col(f"zone{i}_floor", "INTEGER", f"Zone {i} floor in bpm.", f"zone{i}Floor") for i in range(1, 6)],
        col("resting_hr_auto_update_used", "BOOLEAN", "Auto-update resting HR.", "restingHrAutoUpdateUsed"),
        col("change_state", "VARCHAR", "Change state.", "changeState"),
        fetched_at,
    ],
)

power_zones_table = Table(
    name="power_zones",
    comment="Configured power zones per sport (GET biometric-service/powerZones/sports/all).",
    primary_key=["sport"],
    columns=[
        col("sport", "VARCHAR", "Sport.", "sport"),
        col("functional_threshold_power", "DOUBLE", "FTP in watts.", "functionalThresholdPower"),
        *[col(f"zone{i}_floor", "DOUBLE", f"Zone {i} floor in watts.", f"zone{i}Floor") for i in range(1, 8)],
        col("user_local_time", "VARCHAR", "User local time.", "userLocalTime"),
        fetched_at,
    ],
)

personal_record_table = Table(
    name="personal_record",
    comment="Personal records (GET personalrecord-service/personalrecord/prs, includeHistory). typeId: 1 1K, 2 1mi, 3 5K, 4 10K, 7 longest run, 8 longest ride, 9 total ascent, 10 max avg power 20 min, 12 most steps day, 13 most steps week, 14 most steps month, 15 longest goal streak ...",
    primary_key=["id"],
    columns=[
        col("id", "BIGINT", "Record id.", "id"),
        col("type_id", "INTEGER", "Record type id.", "typeId"),
        col("status", "VARCHAR", "ACCEPTED or HISTORICAL.", "status"),
        col("activity_id", "BIGINT", "Activity that set it.", "activityId"),
        col("activity_name", "VARCHAR", "Activity name.", "activityName"),
        col("activity_type", "VARCHAR", "Activity type.", "activityType"),
        ms("activity_start_date_time_gmt", "Activity start (UTC).", "activityStartDateTimeInGMT"),
        col("activity_start_date_time_local_formatted", "TIMESTAMP", "Activity start, local.", "activityStartDateTimeLocalFormatted"),
        col("value", "DOUBLE", "Record value (seconds for times, meters for distances, steps, ...).", "value"),
        ms("pr_start_time_gmt", "Record start within the activity (UTC).", "prStartTimeGmt"),
        col("pr_type_label_key", "VARCHAR", "Label key.", "prTypeLabelKey"),
        col("pool_length_unit", "VARCHAR", "Pool length unit (swims).", "poolLengthUnit"),
        extra_col("Other fields.", None),
        fetched_at,
    ],
)

badge_table = Table(
    name="badge",
    comment="Badges: earned (GET badge-service/badge/earned) and available (badge/available).",
    primary_key=["badge_id"],
    columns=[
        col("badge_id", "BIGINT", "Badge id.", "badgeId"),
        col("badge_key", "VARCHAR", "Badge key.", "badgeKey"),
        col("badge_name", "VARCHAR", "Name.", "badgeName"),
        col("badge_uuid", "VARCHAR", "Badge uuid.", "badgeUuid"),
        col("badge_category_id", "INTEGER", "Category id.", "badgeCategoryId"),
        col("badge_difficulty_id", "INTEGER", "Difficulty id.", "badgeDifficultyId"),
        col("badge_points", "INTEGER", "Points.", "badgePoints"),
        col("badge_type_ids", "JSON", "Type ids.", "badgeTypeIds"),
        col("badge_series_id", "INTEGER", "Series id.", "badgeSeriesId"),
        col("badge_start_date", "DATE", "Available from.", "badgeStartDate"),
        col("badge_end_date", "DATE", "Available until.", "badgeEndDate"),
        ts("badge_earned_date", "Earned (UTC).", "badgeEarnedDate"),
        col("badge_earned_number", "INTEGER", "Times earned.", "badgeEarnedNumber"),
        col("badge_progress_value", "DOUBLE", "Progress value.", "badgeProgressValue"),
        col("badge_target_value", "DOUBLE", "Target value.", "badgeTargetValue"),
        col("badge_unit_id", "INTEGER", "Unit id.", "badgeUnitId"),
        col("badge_assoc_type", "VARCHAR", "Association type.", "badgeAssocType"),
        col("premium", "BOOLEAN", "Premium badge.", "premium"),
        col("earned_by_me", "BOOLEAN", "Earned by the user.", "earnedByMe"),
        expr("earned", "BOOLEAN", "Row comes from the earned list.", "src.earned"),
        extra_col("Other fields.", None),
        fetched_at,
    ],
)

challenge_table = Table(
    name="challenge",
    comment="Badge, virtual and ad-hoc challenges (badgechallenge-service and adhocchallenge-service lists). kind says which list it came from.",
    primary_key=["kind", "uuid"],
    columns=[
        expr("kind", "VARCHAR", "badge_completed, badge_available, badge_non_completed, virtual_in_progress or adhoc.", "src.kind"),
        col("uuid", "VARCHAR", "Challenge uuid.", "uuid"),
        col("name", "VARCHAR", "Name.", "badgeChallengeName"),
        col("start_date", "DATE", "Start.", "startDate"),
        col("end_date", "DATE", "End.", "endDate"),
        col("badge_id", "BIGINT", "Badge id.", "badgeId"),
        col("badge_key", "VARCHAR", "Badge key.", "badgeKey"),
        col("badge_progress_value", "DOUBLE", "Progress.", "badgeProgressValue"),
        col("badge_target_value", "DOUBLE", "Target.", "badgeTargetValue"),
        ts("badge_earned_date", "Earned (UTC).", "badgeEarnedDate"),
        col("user_joined", "BOOLEAN", "Joined.", "userJoined"),
        col("joinable", "BOOLEAN", "Joinable.", "joinable"),
        extra_col("Other fields.", None),
        fetched_at,
    ],
)

goal_table = Table(
    name="goal",
    comment="Goals: user goals (goal-service/goal/user) and goal list entries (goal-service/goal/goals active and past).",
    primary_key=["kind", "goal_pk"],
    columns=[
        expr("kind", "VARCHAR", "user, active or past.", "src.kind"),
        expr("goal_pk", "BIGINT", "Goal id.", "TRY_CAST(coalesce(b->>'userGoalPk', b->>'id', b->>'goalPk') AS BIGINT)", "userGoalPk"),
        col("user_profile_pk", "BIGINT", "User profile id.", "userProfilePk"),
        col("user_goal_category", "VARCHAR", "Category.", "userGoalCategory"),
        col("user_goal_type", "VARCHAR", "Type (STEPS, FLOORS, INTENSITY_MINUTES, WEIGHT, ...).", "userGoalType"),
        col("start_date", "DATE", "Start.", "startDate"),
        col("end_date", "DATE", "End.", "endDate"),
        col("goal_name", "VARCHAR", "Name.", "goalName"),
        col("goal_value", "DOUBLE", "Target value.", "goalValue"),
        col("tracking_period_type", "VARCHAR", "DAILY, WEEKLY, ...", "trackingPeriodType"),
        ts("create_date", "Created (UTC).", "createDate"),
        ts("update_date", "Updated (UTC).", "updateDate"),
        extra_col("Other fields.", None),
        fetched_at,
    ],
)

workout_table = Table(
    name="workout",
    comment="Workouts (GET workout-service/workouts list; detail from workout-service/workout/{id} fills workout_segments).",
    primary_key=["workout_id"],
    columns=[
        col("workout_id", "BIGINT", "Workout id.", "workoutId"),
        col("owner_id", "BIGINT", "Owner id.", "ownerId"),
        col("workout_name", "VARCHAR", "Name.", "workoutName"),
        col("description", "VARCHAR", "Description.", "description"),
        nested("sport_type", "VARCHAR", "Sport type key.", "sportType.sportTypeKey", "sportType"),
        nested("sub_sport_type", "VARCHAR", "Sub sport type key.", "subSportType.subSportTypeKey", "subSportType"),
        ts("created_date", "Created (UTC).", "createdDate"),
        expr("updated_date", "TIMESTAMPTZ", "Updated (UTC).", "TRY_CAST(left(coalesce(b->>'updatedDate', b->>'updateDate'), 23) AS TIMESTAMP)::TIMESTAMPTZ", "updateDate"),
        col("training_plan_id", "BIGINT", "Training plan id.", "trainingPlanId"),
        col("estimated_duration_in_secs", "INTEGER", "Estimated duration in seconds.", "estimatedDurationInSecs"),
        col("estimated_distance_in_meters", "DOUBLE", "Estimated distance in meters.", "estimatedDistanceInMeters"),
        col("workout_provider", "VARCHAR", "Provider.", "workoutProvider"),
        col("workout_source_id", "VARCHAR", "Source id.", "workoutSourceId"),
        col("consumer", "VARCHAR", "Consumer.", "consumer"),
        col("shared", "BOOLEAN", "Shared.", "shared"),
        col("author", "JSON", "Author.", "author"),
        expr("workout_segments", "JSON", "Segments and steps (detail only).", "src.detail->'workoutSegments'"),
        expr("detail", "JSON", "Full workout detail payload (null until fetched).", "src.detail"),
        extra_col("Other fields of the list element.", None),
        fetched_at,
    ],
)

scheduled_workout_table = Table(
    name="calendar_item",
    comment="Calendar items per month (GET calendar-service/year/{y}/month/{m}, calendarItems): scheduled workouts, activities, events, badges, sleep, notes.",
    primary_key=["month", "idx"],
    columns=[
        expr("month", "VARCHAR", "Month key YYYY-MM.", "src.month"),
        expr("idx", "INTEGER", "Position in the month's list.", "src.idx"),
        col("id", "BIGINT", "Item id.", "id"),
        col("item_type", "VARCHAR", "activity, workout, event, badge, sleep, note, goal ...", "itemType"),
        col("date", "DATE", "Date.", "date"),
        col("title", "VARCHAR", "Title.", "title"),
        col("workout_id", "BIGINT", "Workout id (scheduled workouts).", "workoutId"),
        col("sport_type_key", "VARCHAR", "Sport type.", "sportTypeKey"),
        col("activity_type_id", "INTEGER", "Activity type id.", "activityTypeId"),
        col("duration", "DOUBLE", "Duration in seconds.", "duration"),
        col("distance", "DOUBLE", "Distance in meters.", "distance"),
        col("training_plan_id", "BIGINT", "Training plan id.", "trainingPlanId"),
        col("is_race", "BOOLEAN", "Race flag.", "isRace"),
        col("protected_workout_schedule", "BOOLEAN", "Protected schedule.", "protectedWorkoutSchedule"),
        extra_col("Other fields.", None),
        fetched_at,
    ],
)

course_table = Table(
    name="course",
    comment="Courses owned by the user (GET course-service/course/owner/{displayName}); the detail (course-service/course/{id}) adds geometry as JSON.",
    primary_key=["course_id"],
    columns=[
        col("course_id", "BIGINT", "Course id.", "courseId"),
        col("course_name", "VARCHAR", "Name.", "courseName"),
        col("course_description", "VARCHAR", "Description.", "courseDescription"),
        nested("activity_type", "VARCHAR", "Activity type key.", "activityType.typeKey", "activityType"),
        col("distance_in_meters", "DOUBLE", "Length in meters.", "distanceInMeters"),
        col("elevation_gain_in_meters", "DOUBLE", "Elevation gain in meters.", "elevationGainInMeters"),
        col("elevation_loss_in_meters", "DOUBLE", "Elevation loss in meters.", "elevationLossInMeters"),
        col("start_latitude", "DOUBLE", "Start latitude.", "startLatitude"),
        col("start_longitude", "DOUBLE", "Start longitude.", "startLongitude"),
        col("speed_in_meters_per_second", "DOUBLE", "Planned speed in m/s.", "speedInMetersPerSecond"),
        ms("created_date", "Created (UTC).", "createdDate"),
        ms("updated_date", "Updated (UTC).", "updatedDate"),
        col("favorite", "BOOLEAN", "Favorite.", "favorite"),
        col("public", "BOOLEAN", "Public.", "public"),
        col("has_pace_band", "BOOLEAN", "Has pace band.", "hasPaceBand"),
        col("has_power_guide", "BOOLEAN", "Has power guide.", "hasPowerGuide"),
        extra_col("Other fields of the list element.", None),
        expr("detail", "JSON", "Full course detail (geoPoints, coursePoints, boundingBox); null until fetched.", "src.detail"),
        expr("detail_fetched_at", "TIMESTAMPTZ", "When the detail was fetched.", "src.detail_fetched_at"),
        fetched_at,
    ],
)

training_plan_table = Table(
    name="training_plan",
    comment="Training plans (GET trainingplan-service/trainingplan/plans, trainingPlanList).",
    primary_key=["training_plan_id"],
    columns=[
        expr("training_plan_id", "BIGINT", "Plan id.", "TRY_CAST(coalesce(b->>'trainingPlanId', b->>'planId', b->>'id') AS BIGINT)", "trainingPlanId"),
        expr("name", "VARCHAR", "Name.", "coalesce(b->>'name', b->>'trainingPlanName', b->>'planName')", "name"),
        col("start_date", "DATE", "Start.", "startDate"),
        col("end_date", "DATE", "End.", "endDate"),
        col("training_status", "VARCHAR", "Status.", "trainingStatus"),
        col("training_type", "VARCHAR", "Type.", "trainingType"),
        expr("body", "JSON", "Full plan object.", "b"),
        fetched_at,
    ],
)

calendar_event_table = Table(
    name="calendar_event",
    comment="Calendar events such as races (GET calendar-service/events).",
    primary_key=["id"],
    columns=[
        col("id", "BIGINT", "Event id.", "id"),
        col("event_name", "VARCHAR", "Name.", "eventName"),
        col("date", "DATE", "Date.", "date"),
        col("event_type", "VARCHAR", "Type.", "eventType"),
        col("location", "VARCHAR", "Location.", "location"),
        expr("body", "JSON", "Full event object.", "b"),
        fetched_at,
    ],
)

snapshot_table = Table(
    name="snapshot",
    comment=(
        "Small singleton payloads kept whole (one row per kind, newest version): activity_types, activity_event_types, "
        "cycling_ftp, current_biometrics, lifetime_totals, user_statistics, race_predictions, workout_count, "
        "activity_count, nutrition_status, sync_timestamp, primary_training_device, jetlag_trips, golf_summary, "
        "active_training_plans, gear_defaults. raw_payload keeps their history."
    ),
    primary_key=["kind"],
    columns=[
        expr("kind", "VARCHAR", "Payload kind (object_type).", "src.object_type"),
        expr("body", "JSON", "The payload.", "src.body"),
        fetched_at,
    ],
)

race_prediction_table = Table(
    name="race_prediction",
    comment="History of race time predictions (GET metrics/racepredictions/latest): a row per version seen.",
    primary_key=["fetched_at"],
    columns=[
        fetched_at,
        col("calendar_date", "DATE", "Date of the prediction.", "calendarDate"),
        col("time_5k", "INTEGER", "Predicted 5K in seconds.", "time5K"),
        col("time_10k", "INTEGER", "Predicted 10K in seconds.", "time10K"),
        col("time_half_marathon", "INTEGER", "Predicted half marathon in seconds.", "timeHalfMarathon"),
        col("time_marathon", "INTEGER", "Predicted marathon in seconds.", "timeMarathon"),
    ],
)

TABLES: list[Table] = [
    activity_table,
    activity_lap_table,
    activity_lap_event_table,
    activity_typed_split_table,
    activity_split_summary_table,
    activity_weather_table,
    activity_time_in_zone_table,
    activity_gear_table,
    activity_exercise_set_table,
    activity_polyline_table,
    activity_detail_metric_table,
    daily_summary_table,
    daily_summary_v2_table,
    steps_interval_table,
    heart_rate_day_table,
    heart_rate_sample_table,
    stress_day_table,
    stress_sample_table,
    body_battery_day_table,
    body_battery_sample_table,
    body_battery_event_table,
    respiration_day_table,
    respiration_sample_table,
    spo2_day_table,
    floors_day_table,
    floors_interval_table,
    intensity_minutes_day_table,
    sleep_table,
    sleep_level_table,
    sleep_movement_table,
    sleep_sample_table,
    hrv_table,
    hrv_reading_table,
    training_readiness_table,
    training_status_table,
    max_metrics_table,
    endurance_score_table,
    hill_score_table,
    fitness_age_table,
    sleep_stats_table,
    steps_daily_table,
    wellness_metric_table,
    hydration_table,
    all_day_event_table,
    nap_table,
    lifestyle_logging_table,
    cycling_ability_table,
    running_tolerance_table,
    weigh_in_table,
    blood_pressure_table,
    user_settings_table,
    user_profile_table,
    device_table,
    device_settings_table,
    device_last_used_table,
    sensor_table,
    gear_table,
    heart_rate_zones_table,
    power_zones_table,
    personal_record_table,
    badge_table,
    challenge_table,
    goal_table,
    workout_table,
    scheduled_workout_table,
    course_table,
    training_plan_table,
    calendar_event_table,
    snapshot_table,
    race_prediction_table,
]

for _t in TABLES:
    fill_extra(_t)

_BY_NAME = {t.name: t for t in TABLES}


def table(name: str) -> Table:
    return _BY_NAME[name]

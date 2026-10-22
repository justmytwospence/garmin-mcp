"""Typed tables for decoded FIT files (original activity files and daily wellness
files). Rows are produced by fit/decode.py, which lands every decoded message as JSON
in a temp table and projects it with the expressions here. Fields the schema does not
name land in the ``extra`` JSON column of their typed table; messages without a typed
table land whole in fit_message. Nothing from a FIT file is dropped.

Column naming follows the FIT SDK (snake_case) and, where it fits, the garmin-health-data
project's tables.ddl. Units are the FIT SDK's after scale/offset: meters, m/s, seconds,
bpm, rpm, watts, degrees Celsius, mm (vertical oscillation, step length), ms (stance time).
Positions are converted from semicircles to degrees.
"""

from __future__ import annotations

from ..db.tabledef import Column, Table, expr

__all__ = ["FIT_TABLES", "FIT_MESSAGE_TABLES", "fit_table"]

DEG = "(180.0 / 2147483648.0)"


def f(name: str, type_: str, comment: str, key: str | None = None) -> Column:
    k = key or name
    if type_ == "JSON":
        return expr(name, type_, comment, f"b->'{k}'", k)
    if type_ == "VARCHAR":
        return expr(name, type_, comment, f"b->>'{k}'", k)
    if type_ == "TIMESTAMPTZ":
        return expr(name, type_, comment, f"TRY_CAST(b->>'{k}' AS TIMESTAMPTZ)", k)
    if type_ == "TIMESTAMP":
        return expr(name, type_, comment, f"TRY_CAST(left(b->>'{k}', 19) AS TIMESTAMP)", k)
    if type_.endswith("[]"):
        return expr(name, type_, comment, f"TRY_CAST(b->'{k}' AS {type_})", k)
    return expr(name, type_, comment, f"TRY_CAST(b->>'{k}' AS {type_})", k)


def pos(name: str, comment: str, key: str) -> Column:
    return expr(name, "DOUBLE", comment, f"TRY_CAST(b->>'{key}' AS DOUBLE) * {DEG}", key)


def extra() -> Column:
    return Column("extra", "JSON", "Fields of the message not mapped to a column (unknown field numbers, developer fields, rarely used fields).", "__EXTRA__", None)


file_ctx = [
    expr("fit_file_id", "VARCHAR", "References fit_file.fit_file_id.", "src.fit_file_id"),
]
activity_ctx = [
    expr("activity_id", "BIGINT", "Garmin activity id (null for wellness files).", "src.activity_id"),
    *file_ctx,
]
day_ctx = [
    expr("calendar_date", "DATE", "Day of the wellness file the message came from.", "src.calendar_date"),
    *file_ctx,
]
idx_col = expr("idx", "INTEGER", "Position of the message within its file.", "src.idx")

fit_file_table = Table(
    name="fit_file",
    comment="Every FIT file decoded from the stored zips (raw_file): one activity file per activity; several wellness, metrics, sleep, HRV and nap files per day.",
    primary_key=["fit_file_id"],
    columns=[
        expr("fit_file_id", "VARCHAR", "kind/key/file name.", "src.fit_file_id"),
        expr("kind", "VARCHAR", "activity_fit or wellness_fit.", "src.kind"),
        expr("key", "VARCHAR", "Activity id or calendar date.", "src.key"),
        expr("file_name", "VARCHAR", "File name inside the zip.", "src.file_name"),
        f("type", "VARCHAR", "FIT file type (activity, monitoring_b, sleep, hrv_status, metrics, nap ...)."),
        f("time_created", "TIMESTAMPTZ", "File creation time (UTC)."),
        f("manufacturer", "VARCHAR", "Manufacturer."),
        f("product", "INTEGER", "Product id."),
        f("garmin_product", "VARCHAR", "Garmin product name."),
        f("serial_number", "BIGINT", "Device serial."),
        f("number", "INTEGER", "File number."),
        expr("message_counts", "JSON", "Message name -> count for the file.", "src.message_counts"),
        expr("parser_version", "INTEGER", "Decoder version that produced the rows.", "src.parser_version"),
        expr("parsed_at", "TIMESTAMPTZ", "When decoded.", "src.parsed_at"),
    ],
)

_session_lap_common: list[Column] = [
    f("timestamp", "TIMESTAMPTZ", "End time (UTC)."),
    f("start_time", "TIMESTAMPTZ", "Start time (UTC)."),
    pos("start_position_lat", "Start latitude in degrees.", "start_position_lat"),
    pos("start_position_long", "Start longitude in degrees.", "start_position_long"),
    pos("end_position_lat", "End latitude in degrees.", "end_position_lat"),
    pos("end_position_long", "End longitude in degrees.", "end_position_long"),
    f("sport", "VARCHAR", "Sport."),
    f("sub_sport", "VARCHAR", "Sub sport."),
    f("event", "VARCHAR", "Event."),
    f("event_type", "VARCHAR", "Event type."),
    f("total_elapsed_time", "DOUBLE", "Elapsed time in seconds."),
    f("total_timer_time", "DOUBLE", "Timer time in seconds."),
    f("total_moving_time", "DOUBLE", "Moving time in seconds."),
    f("total_distance", "DOUBLE", "Distance in meters."),
    f("total_cycles", "INTEGER", "Cycles (strides, strokes, revolutions)."),
    f("total_strides", "INTEGER", "Strides."),
    f("total_strokes", "INTEGER", "Strokes."),
    f("total_calories", "INTEGER", "Calories in kcal."),
    f("total_fat_calories", "INTEGER", "Fat calories in kcal."),
    f("avg_speed", "DOUBLE", "Average speed in m/s (enhanced)."),
    f("max_speed", "DOUBLE", "Max speed in m/s (enhanced)."),
    f("avg_heart_rate", "INTEGER", "Average HR in bpm."),
    f("max_heart_rate", "INTEGER", "Max HR in bpm."),
    f("min_heart_rate", "INTEGER", "Min HR in bpm."),
    f("avg_cadence", "INTEGER", "Average cadence (rpm or strides/min)."),
    f("max_cadence", "INTEGER", "Max cadence."),
    f("avg_running_cadence", "INTEGER", "Average running cadence in strides/min."),
    f("max_running_cadence", "INTEGER", "Max running cadence in strides/min."),
    f("avg_fractional_cadence", "DOUBLE", "Fractional part of average cadence."),
    f("max_fractional_cadence", "DOUBLE", "Fractional part of max cadence."),
    f("avg_power", "INTEGER", "Average power in watts."),
    f("max_power", "INTEGER", "Max power in watts."),
    f("normalized_power", "INTEGER", "Normalized power in watts."),
    f("total_work", "BIGINT", "Work in J."),
    f("avg_temperature", "INTEGER", "Average temperature in degrees Celsius."),
    f("max_temperature", "INTEGER", "Max temperature in degrees Celsius."),
    f("min_temperature", "INTEGER", "Min temperature in degrees Celsius."),
    f("total_ascent", "INTEGER", "Ascent in meters."),
    f("total_descent", "INTEGER", "Descent in meters."),
    f("avg_altitude", "DOUBLE", "Average altitude in meters (enhanced)."),
    f("max_altitude", "DOUBLE", "Max altitude in meters (enhanced)."),
    f("min_altitude", "DOUBLE", "Min altitude in meters (enhanced)."),
    f("avg_grade", "DOUBLE", "Average grade in %."),
    f("avg_pos_grade", "DOUBLE", "Average positive grade in %."),
    f("avg_neg_grade", "DOUBLE", "Average negative grade in %."),
    f("max_pos_grade", "DOUBLE", "Max positive grade in %."),
    f("max_neg_grade", "DOUBLE", "Max negative grade in %."),
    f("avg_pos_vertical_speed", "DOUBLE", "Average positive vertical speed in m/s."),
    f("avg_neg_vertical_speed", "DOUBLE", "Average negative vertical speed in m/s."),
    f("max_pos_vertical_speed", "DOUBLE", "Max positive vertical speed in m/s."),
    f("max_neg_vertical_speed", "DOUBLE", "Max negative vertical speed in m/s."),
    f("avg_vam", "DOUBLE", "Average ascent speed in m/s."),
    f("avg_vertical_oscillation", "DOUBLE", "Average vertical oscillation in mm."),
    f("avg_stance_time", "DOUBLE", "Average ground contact time in ms."),
    f("avg_stance_time_percent", "DOUBLE", "Average stance time in %."),
    f("avg_stance_time_balance", "DOUBLE", "Average stance time balance in %."),
    f("avg_step_length", "DOUBLE", "Average step length in mm."),
    f("avg_vertical_ratio", "DOUBLE", "Average vertical ratio in %."),
    f("avg_grade_adjusted_speed", "DOUBLE", "Grade-adjusted average speed in m/s."),
    f("avg_flow", "DOUBLE", "Flow score."),
    f("avg_grit", "DOUBLE", "Grit score."),
    f("total_grit", "DOUBLE", "Total grit."),
    f("total_flow", "DOUBLE", "Total flow."),
    f("jump_count", "INTEGER", "Jumps."),
    f("avg_left_torque_effectiveness", "DOUBLE", "Left torque effectiveness in %."),
    f("avg_right_torque_effectiveness", "DOUBLE", "Right torque effectiveness in %."),
    f("avg_left_pedal_smoothness", "DOUBLE", "Left pedal smoothness in %."),
    f("avg_right_pedal_smoothness", "DOUBLE", "Right pedal smoothness in %."),
    f("avg_combined_pedal_smoothness", "DOUBLE", "Combined pedal smoothness in %."),
    f("left_right_balance", "DOUBLE", "Left/right balance (raw FIT encoding)."),
    f("avg_left_pco", "INTEGER", "Average left platform center offset in mm."),
    f("avg_right_pco", "INTEGER", "Average right platform center offset in mm."),
    f("avg_left_power_phase", "DOUBLE[]", "Average left power phase angles in degrees."),
    f("avg_left_power_phase_peak", "DOUBLE[]", "Average left peak power phase in degrees."),
    f("avg_right_power_phase", "DOUBLE[]", "Average right power phase angles in degrees."),
    f("avg_right_power_phase_peak", "DOUBLE[]", "Average right peak power phase in degrees."),
    f("avg_power_position", "INTEGER[]", "Average power seated/standing in watts."),
    f("max_power_position", "INTEGER[]", "Max power seated/standing in watts."),
    f("avg_cadence_position", "INTEGER[]", "Average cadence seated/standing."),
    f("max_cadence_position", "INTEGER[]", "Max cadence seated/standing."),
    f("time_standing", "DOUBLE", "Time standing in seconds."),
    f("stand_count", "INTEGER", "Number of stands."),
    f("avg_respiration_rate", "DOUBLE", "Average respiration in breaths/min (enhanced)."),
    f("max_respiration_rate", "DOUBLE", "Max respiration in breaths/min (enhanced)."),
    f("min_respiration_rate", "DOUBLE", "Min respiration in breaths/min (enhanced)."),
    f("total_training_effect", "DOUBLE", "Aerobic training effect."),
    f("total_anaerobic_training_effect", "DOUBLE", "Anaerobic training effect."),
    f("training_stress_score", "DOUBLE", "TSS."),
    f("intensity_factor", "DOUBLE", "Intensity factor."),
    f("threshold_power", "INTEGER", "Threshold power in watts."),
    f("pool_length", "DOUBLE", "Pool length in meters."),
    f("pool_length_unit", "VARCHAR", "Pool length unit."),
    f("num_lengths", "INTEGER", "Lengths (swimming)."),
    f("num_active_lengths", "INTEGER", "Active lengths."),
    f("avg_stroke_distance", "DOUBLE", "Average stroke distance in meters."),
    f("avg_stroke_count", "DOUBLE", "Average strokes per length."),
    f("swim_stroke", "VARCHAR", "Swim stroke."),
    f("avg_swolf", "INTEGER", "Average SWOLF."),
    f("first_lap_index", "INTEGER", "First lap index (session)."),
    f("num_laps", "INTEGER", "Laps (session)."),
    f("lap_trigger", "VARCHAR", "Lap trigger (lap)."),
    f("intensity", "VARCHAR", "Intensity (lap)."),
    f("wkt_step_index", "INTEGER", "Workout step index (lap)."),
    f("sport_profile_name", "VARCHAR", "Sport profile name (session)."),
    f("trigger", "VARCHAR", "Session trigger."),
    f("nec_lat", "DOUBLE", "Bounding box NE latitude (semicircles)."),
    f("nec_long", "DOUBLE", "Bounding box NE longitude (semicircles)."),
    f("swc_lat", "DOUBLE", "Bounding box SW latitude (semicircles)."),
    f("swc_long", "DOUBLE", "Bounding box SW longitude (semicircles)."),
    f("sport_index", "INTEGER", "Sport index (multisport)."),
    f("metabolic_calories", "INTEGER", "Metabolic calories."),
    f("enhanced_avg_speed", "DOUBLE", "Enhanced average speed in m/s."),
    f("enhanced_max_speed", "DOUBLE", "Enhanced max speed in m/s."),
    f("enhanced_avg_altitude", "DOUBLE", "Enhanced average altitude in m."),
    f("enhanced_max_altitude", "DOUBLE", "Enhanced max altitude in m."),
    f("enhanced_min_altitude", "DOUBLE", "Enhanced min altitude in m."),
    f("enhanced_avg_respiration_rate", "DOUBLE", "Enhanced average respiration."),
    f("enhanced_max_respiration_rate", "DOUBLE", "Enhanced max respiration."),
    f("enhanced_min_respiration_rate", "DOUBLE", "Enhanced min respiration."),
    f("developer_fields", "JSON", "Developer (Connect IQ) fields keyed by field number."),
]

fit_session_table = Table(
    name="fit_session",
    comment="Session messages of activity FIT files: one per sport session (multisport activities have several).",
    primary_key=["fit_file_id", "message_index"],
    columns=[*activity_ctx, f("message_index", "INTEGER", "Session index."), *_session_lap_common, extra()],
)

fit_lap_table = Table(
    name="fit_lap",
    comment="Lap messages of activity FIT files.",
    primary_key=["fit_file_id", "message_index"],
    columns=[*activity_ctx, f("message_index", "INTEGER", "Lap index."), *_session_lap_common, extra()],
)

fit_split_table = Table(
    name="fit_split",
    comment="Split messages of activity FIT files (intervals, climbs, run/walk/idle, ...).",
    primary_key=["fit_file_id", "message_index"],
    columns=[
        *activity_ctx,
        f("message_index", "INTEGER", "Split index."),
        f("split_type", "VARCHAR", "Split type."),
        f("timestamp", "TIMESTAMPTZ", "End time (UTC)."),
        f("start_time", "TIMESTAMPTZ", "Start time (UTC)."),
        f("end_time", "TIMESTAMPTZ", "End time (UTC)."),
        f("total_elapsed_time", "DOUBLE", "Elapsed seconds."),
        f("total_timer_time", "DOUBLE", "Timer seconds."),
        f("total_moving_time", "DOUBLE", "Moving seconds."),
        f("total_distance", "DOUBLE", "Distance in meters."),
        f("avg_speed", "DOUBLE", "Average speed in m/s."),
        f("max_speed", "DOUBLE", "Max speed in m/s."),
        f("avg_heart_rate", "INTEGER", "Average HR."),
        f("max_heart_rate", "INTEGER", "Max HR."),
        f("avg_cadence", "INTEGER", "Average cadence."),
        f("max_cadence", "INTEGER", "Max cadence."),
        f("avg_power", "INTEGER", "Average power in watts."),
        f("max_power", "INTEGER", "Max power in watts."),
        f("total_ascent", "INTEGER", "Ascent in meters."),
        f("total_descent", "INTEGER", "Descent in meters."),
        f("total_calories", "INTEGER", "Calories."),
        f("start_elevation", "DOUBLE", "Start elevation in meters."),
        f("avg_vert_speed", "DOUBLE", "Average vertical speed in m/s."),
        f("avg_grade", "DOUBLE", "Average grade in %."),
        f("max_grade", "DOUBLE", "Max grade in %."),
        pos("start_position_lat", "Start latitude.", "start_position_lat"),
        pos("start_position_long", "Start longitude.", "start_position_long"),
        pos("end_position_lat", "End latitude.", "end_position_lat"),
        pos("end_position_long", "End longitude.", "end_position_long"),
        f("first_lap_index", "INTEGER", "First lap index."),
        f("num_laps", "INTEGER", "Laps covered."),
        f("sport", "VARCHAR", "Sport."),
        f("sub_sport", "VARCHAR", "Sub sport."),
        extra(),
    ],
)

fit_record_table = Table(
    name="fit_record",
    comment="Per-second (or per-sample) records of activity FIT files: the full-resolution time series. Positions in degrees; unknown fields in extra.",
    primary_key=["fit_file_id", "idx"],
    columns=[
        *activity_ctx,
        idx_col,
        f("timestamp", "TIMESTAMPTZ", "Sample time (UTC)."),
        pos("position_lat", "Latitude in degrees.", "position_lat"),
        pos("position_long", "Longitude in degrees.", "position_long"),
        f("distance", "DOUBLE", "Cumulative distance in meters."),
        expr("speed", "DOUBLE", "Speed in m/s (enhanced_speed, else speed).", "TRY_CAST(coalesce(b->>'enhanced_speed', b->>'speed') AS DOUBLE)", "enhanced_speed"),
        expr("altitude", "DOUBLE", "Altitude in meters (enhanced_altitude, else altitude).", "TRY_CAST(coalesce(b->>'enhanced_altitude', b->>'altitude') AS DOUBLE)", "enhanced_altitude"),
        f("heart_rate", "INTEGER", "Heart rate in bpm."),
        f("cadence", "INTEGER", "Cadence (rpm; running: strides/min of one foot)."),
        f("fractional_cadence", "DOUBLE", "Fractional cadence."),
        f("power", "INTEGER", "Power in watts."),
        f("accumulated_power", "BIGINT", "Accumulated power in watts."),
        f("temperature", "INTEGER", "Temperature in degrees Celsius."),
        expr("respiration_rate", "DOUBLE", "Respiration in breaths/min.", "TRY_CAST(coalesce(b->>'enhanced_respiration_rate', b->>'respiration_rate') AS DOUBLE)", "enhanced_respiration_rate"),
        f("grade", "DOUBLE", "Grade in %."),
        f("calories", "INTEGER", "Calories."),
        f("vertical_oscillation", "DOUBLE", "Vertical oscillation in mm."),
        f("stance_time", "DOUBLE", "Ground contact time in ms."),
        f("stance_time_percent", "DOUBLE", "Stance time in %."),
        f("stance_time_balance", "DOUBLE", "Stance time balance in %."),
        f("step_length", "DOUBLE", "Step length in mm."),
        f("vertical_ratio", "DOUBLE", "Vertical ratio in %."),
        f("activity_type", "VARCHAR", "Activity type of the sample."),
        f("left_right_balance", "INTEGER", "Left/right balance (raw FIT encoding: value & 0x7F is right %)."),
        f("left_torque_effectiveness", "DOUBLE", "Left torque effectiveness in %."),
        f("right_torque_effectiveness", "DOUBLE", "Right torque effectiveness in %."),
        f("left_pedal_smoothness", "DOUBLE", "Left pedal smoothness in %."),
        f("right_pedal_smoothness", "DOUBLE", "Right pedal smoothness in %."),
        f("combined_pedal_smoothness", "DOUBLE", "Combined pedal smoothness in %."),
        f("left_pco", "INTEGER", "Left platform center offset in mm."),
        f("right_pco", "INTEGER", "Right platform center offset in mm."),
        f("left_power_phase", "DOUBLE[]", "Left power phase angles in degrees."),
        f("left_power_phase_peak", "DOUBLE[]", "Left peak power phase in degrees."),
        f("right_power_phase", "DOUBLE[]", "Right power phase angles in degrees."),
        f("right_power_phase_peak", "DOUBLE[]", "Right peak power phase in degrees."),
        f("cycle_length16", "DOUBLE", "Cycle length in meters."),
        f("gps_accuracy", "INTEGER", "GPS accuracy in meters."),
        f("battery_soc", "DOUBLE", "Battery state of charge in %."),
        f("core_temperature", "DOUBLE", "Core temperature in degrees Celsius."),
        f("stroke_type", "VARCHAR", "Stroke type (swimming)."),
        f("absolute_pressure", "INTEGER", "Absolute pressure in Pa."),
        f("depth", "DOUBLE", "Depth in meters (diving)."),
        f("flow", "DOUBLE", "Flow."),
        f("grit", "DOUBLE", "Grit."),
        f("developer_fields", "JSON", "Developer (Connect IQ) fields keyed by field number."),
        extra(),
    ],
)

fit_length_table = Table(
    name="fit_length",
    comment="Length messages of swimming FIT files (one per pool length).",
    primary_key=["fit_file_id", "message_index"],
    columns=[
        *activity_ctx,
        f("message_index", "INTEGER", "Length index."),
        f("timestamp", "TIMESTAMPTZ", "End (UTC)."),
        f("start_time", "TIMESTAMPTZ", "Start (UTC)."),
        f("total_elapsed_time", "DOUBLE", "Elapsed seconds."),
        f("total_timer_time", "DOUBLE", "Timer seconds."),
        f("total_strokes", "INTEGER", "Strokes."),
        f("avg_speed", "DOUBLE", "Average speed in m/s."),
        f("swim_stroke", "VARCHAR", "Stroke."),
        f("avg_swimming_cadence", "INTEGER", "Strokes per minute."),
        f("length_type", "VARCHAR", "active or idle."),
        f("total_calories", "INTEGER", "Calories."),
        f("event", "VARCHAR", "Event."),
        f("event_type", "VARCHAR", "Event type."),
        extra(),
    ],
)

fit_event_table = Table(
    name="fit_event",
    comment="Event messages of activity and wellness FIT files (timer start/stop, laps, recovery HR, auto-detected events ...).",
    primary_key=["fit_file_id", "idx"],
    columns=[
        *activity_ctx,
        expr("calendar_date", "DATE", "Day (wellness files).", "src.calendar_date"),
        idx_col,
        f("timestamp", "TIMESTAMPTZ", "Event time (UTC)."),
        f("event", "VARCHAR", "Event (timer, lap, recovery_hr, rider_position_change, ... or a number when unknown)."),
        f("event_type", "VARCHAR", "Event type (start, stop_all, marker ...)."),
        f("event_group", "INTEGER", "Event group."),
        f("data", "BIGINT", "Event data."),
        f("data16", "INTEGER", "16-bit event data."),
        f("timer_trigger", "VARCHAR", "Timer trigger (manual, auto)."),
        f("rider_position", "VARCHAR", "Rider position (seated, standing)."),
        extra(),
    ],
)

fit_hrv_table = Table(
    name="fit_hrv",
    comment="Beat-to-beat RR intervals from activity FIT hrv messages (each message carries up to 5 intervals; one row per interval).",
    primary_key=["fit_file_id", "idx", "pos"],
    columns=[
        *activity_ctx,
        idx_col,
        expr("pos", "INTEGER", "Position within the message (0-4).", "src.pos"),
        expr("rr_interval", "DOUBLE", "RR interval in seconds.", "src.rr"),
    ],
)

fit_time_in_zone_table = Table(
    name="fit_time_in_zone",
    comment="Time-in-zone messages of activity FIT files (per session/lap: HR and power zone boundaries and time in each).",
    primary_key=["fit_file_id", "idx"],
    columns=[
        *activity_ctx,
        idx_col,
        f("timestamp", "TIMESTAMPTZ", "Time (UTC)."),
        f("reference_mesg", "VARCHAR", "session or lap."),
        f("reference_index", "INTEGER", "Index of the referenced message."),
        f("time_in_hr_zone", "DOUBLE[]", "Seconds per HR zone."),
        f("time_in_power_zone", "DOUBLE[]", "Seconds per power zone."),
        f("hr_zone_high_boundary", "INTEGER[]", "HR zone upper bounds in bpm."),
        f("power_zone_high_boundary", "INTEGER[]", "Power zone upper bounds in watts."),
        f("hr_calc_type", "VARCHAR", "HR zone calculation type."),
        f("pwr_calc_type", "VARCHAR", "Power zone calculation type."),
        f("max_heart_rate", "INTEGER", "Max HR used."),
        f("resting_heart_rate", "INTEGER", "Resting HR used."),
        f("threshold_heart_rate", "INTEGER", "Threshold HR used."),
        f("functional_threshold_power", "INTEGER", "FTP used."),
        extra(),
    ],
)

fit_device_info_table = Table(
    name="fit_device_info",
    comment="Device info messages (watch, sensors, their firmware and battery) of every FIT file.",
    primary_key=["fit_file_id", "idx"],
    columns=[
        *activity_ctx,
        expr("calendar_date", "DATE", "Day (wellness files).", "src.calendar_date"),
        idx_col,
        f("timestamp", "TIMESTAMPTZ", "Time (UTC)."),
        f("device_index", "VARCHAR", "Device index (creator = the recording device)."),
        f("device_type", "VARCHAR", "Device type."),
        f("antplus_device_type", "VARCHAR", "ANT+ device type."),
        f("local_device_type", "VARCHAR", "Local device type."),
        f("manufacturer", "VARCHAR", "Manufacturer."),
        f("product", "INTEGER", "Product id."),
        f("garmin_product", "VARCHAR", "Garmin product."),
        f("product_name", "VARCHAR", "Product name."),
        f("serial_number", "BIGINT", "Serial number."),
        f("software_version", "DOUBLE", "Software version."),
        f("hardware_version", "INTEGER", "Hardware version."),
        f("battery_status", "VARCHAR", "Battery status."),
        f("battery_voltage", "DOUBLE", "Battery voltage in V."),
        f("battery_level", "INTEGER", "Battery level in %."),
        f("cum_operating_time", "BIGINT", "Cumulative operating time in seconds."),
        f("source_type", "VARCHAR", "Source type."),
        f("ant_network", "VARCHAR", "ANT network."),
        f("ant_device_number", "INTEGER", "ANT device number."),
        extra(),
    ],
)

fit_monitoring_table = Table(
    name="fit_monitoring",
    comment="Monitoring messages of wellness FIT files: steps, distance, calories, heart rate and intensity over the day (several files per day; rows are cumulative within their file).",
    primary_key=["fit_file_id", "idx"],
    columns=[
        *day_ctx,
        idx_col,
        f("timestamp", "TIMESTAMPTZ", "Time (UTC); null for timestamp_16-only rows (resolved by the decoder into timestamp when possible)."),
        f("timestamp_16", "INTEGER", "16-bit timestamp."),
        f("activity_type", "VARCHAR", "Activity type (walking, running, generic ...)."),
        f("intensity", "INTEGER", "Intensity."),
        f("current_activity_type_intensity", "INTEGER", "Current activity type intensity."),
        f("steps", "INTEGER", "Cumulative steps for the activity type."),
        f("cycles", "DOUBLE", "Cumulative cycles."),
        f("distance", "DOUBLE", "Cumulative distance in meters."),
        f("active_time", "DOUBLE", "Cumulative active time in seconds."),
        f("active_calories", "INTEGER", "Cumulative active calories."),
        f("calories", "INTEGER", "Cumulative calories."),
        f("duration_min", "INTEGER", "Minutes since day start."),
        f("duration", "INTEGER", "Duration in seconds."),
        f("heart_rate", "INTEGER", "Heart rate in bpm."),
        f("moderate_activity_minutes", "INTEGER", "Moderate activity minutes."),
        f("vigorous_activity_minutes", "INTEGER", "Vigorous activity minutes."),
        f("ascent", "DOUBLE", "Ascent in meters."),
        f("descent", "DOUBLE", "Descent in meters."),
        f("local_timestamp", "TIMESTAMP", "Local time."),
        extra(),
    ],
)

fit_monitoring_info_table = Table(
    name="fit_monitoring_info",
    comment="Monitoring info messages of wellness FIT files (day start, resting metabolic rate, cycle conversion factors).",
    primary_key=["fit_file_id", "idx"],
    columns=[
        *day_ctx,
        idx_col,
        f("timestamp", "TIMESTAMPTZ", "Time (UTC)."),
        f("local_timestamp", "TIMESTAMP", "Local time."),
        f("activity_type", "JSON", "Activity types covered."),
        f("cycles_to_distance", "JSON", "Cycles to distance factors."),
        f("cycles_to_calories", "JSON", "Cycles to calories factors."),
        f("resting_metabolic_rate", "INTEGER", "Resting metabolic rate in kcal/day."),
        extra(),
    ],
)

fit_monitoring_hr_table = Table(
    name="fit_monitoring_hr",
    comment="monitoring_hr_data messages: resting heart rate figures of the day.",
    primary_key=["fit_file_id", "idx"],
    columns=[
        *day_ctx,
        idx_col,
        f("timestamp", "TIMESTAMPTZ", "Time (UTC)."),
        f("resting_heart_rate", "INTEGER", "Resting HR in bpm."),
        f("current_day_resting_heart_rate", "INTEGER", "Current day resting HR in bpm."),
        extra(),
    ],
)

fit_stress_level_table = Table(
    name="fit_stress_level",
    comment="stress_level messages of wellness FIT files: stress every minute (negative = unmeasurable).",
    primary_key=["fit_file_id", "idx"],
    columns=[
        *day_ctx,
        idx_col,
        f("stress_level_time", "TIMESTAMPTZ", "Time (UTC)."),
        f("stress_level_value", "INTEGER", "Stress (0-100; negative when unmeasurable)."),
        extra(),
    ],
)

fit_respiration_rate_table = Table(
    name="fit_respiration_rate",
    comment="respiration_rate messages of wellness FIT files (breaths per minute).",
    primary_key=["fit_file_id", "idx"],
    columns=[
        *day_ctx,
        idx_col,
        f("timestamp", "TIMESTAMPTZ", "Time (UTC)."),
        f("respiration_rate", "DOUBLE", "Breaths per minute (negative when unmeasurable)."),
        extra(),
    ],
)

fit_spo2_table = Table(
    name="fit_spo2",
    comment="spo2_data messages of wellness FIT files (pulse ox readings).",
    primary_key=["fit_file_id", "idx"],
    columns=[
        *day_ctx,
        idx_col,
        f("timestamp", "TIMESTAMPTZ", "Time (UTC)."),
        f("reading_spo2", "INTEGER", "SpO2 in %."),
        f("reading_confidence", "INTEGER", "Confidence."),
        f("mode", "VARCHAR", "Mode (continuous, spot_check ...)."),
        extra(),
    ],
)

fit_hrv_value_table = Table(
    name="fit_hrv_value",
    comment="hrv_value messages of HRV status FIT files: 5-minute overnight HRV (ms).",
    primary_key=["fit_file_id", "idx"],
    columns=[
        *day_ctx,
        idx_col,
        f("timestamp", "TIMESTAMPTZ", "Time (UTC)."),
        f("value", "DOUBLE", "HRV in ms."),
        extra(),
    ],
)

fit_hrv_status_summary_table = Table(
    name="fit_hrv_status_summary",
    comment="hrv_status_summary messages of HRV status FIT files.",
    primary_key=["fit_file_id", "idx"],
    columns=[
        *day_ctx,
        idx_col,
        f("timestamp", "TIMESTAMPTZ", "Time (UTC)."),
        f("weekly_average", "DOUBLE", "Weekly average in ms."),
        f("last_night_average", "DOUBLE", "Last night average in ms."),
        f("last_night_5_min_high", "DOUBLE", "Last night 5-minute high in ms."),
        f("baseline_low_upper", "DOUBLE", "Baseline low upper in ms."),
        f("baseline_balanced_lower", "DOUBLE", "Baseline balanced lower in ms."),
        f("baseline_balanced_upper", "DOUBLE", "Baseline balanced upper in ms."),
        f("status", "VARCHAR", "Status."),
        extra(),
    ],
)

fit_sleep_level_table = Table(
    name="fit_sleep_level",
    comment="sleep_level messages of sleep FIT files (stage changes).",
    primary_key=["fit_file_id", "idx"],
    columns=[
        *day_ctx,
        idx_col,
        f("timestamp", "TIMESTAMPTZ", "Time (UTC)."),
        f("sleep_level", "VARCHAR", "awake, light, deep, rem ..."),
        extra(),
    ],
)

fit_sleep_assessment_table = Table(
    name="fit_sleep_assessment",
    comment="sleep_assessment messages of sleep FIT files (score components).",
    primary_key=["fit_file_id", "idx"],
    columns=[
        *day_ctx,
        idx_col,
        f("overall_sleep_score", "INTEGER", "Overall score."),
        f("sleep_quality_score", "INTEGER", "Quality score."),
        f("sleep_recovery_score", "INTEGER", "Recovery score."),
        f("sleep_duration_score", "INTEGER", "Duration score."),
        f("deep_sleep_score", "INTEGER", "Deep score."),
        f("light_sleep_score", "INTEGER", "Light score."),
        f("rem_sleep_score", "INTEGER", "REM score."),
        f("awake_time_score", "INTEGER", "Awake time score."),
        f("awakenings_count", "INTEGER", "Awakenings."),
        f("awakenings_count_score", "INTEGER", "Awakenings score."),
        f("combined_awake_score", "INTEGER", "Combined awake score."),
        f("interruptions_score", "INTEGER", "Interruptions score."),
        f("sleep_restlessness_score", "INTEGER", "Restlessness score."),
        f("average_stress_during_sleep", "DOUBLE", "Average stress during sleep."),
        extra(),
    ],
)

fit_nap_event_table = Table(
    name="fit_nap_event",
    comment="nap_event messages of nap FIT files.",
    primary_key=["fit_file_id", "idx"],
    columns=[
        *day_ctx,
        idx_col,
        f("timestamp", "TIMESTAMPTZ", "Time (UTC)."),
        f("start_time", "TIMESTAMPTZ", "Nap start (UTC)."),
        f("end_time", "TIMESTAMPTZ", "Nap end (UTC)."),
        f("start_timezone_offset", "INTEGER", "Offset at start."),
        f("end_timezone_offset", "INTEGER", "Offset at end."),
        f("feedback", "VARCHAR", "Feedback."),
        f("source", "VARCHAR", "Source."),
        f("update_timestamp", "TIMESTAMPTZ", "Updated (UTC)."),
        extra(),
    ],
)

fit_message_table = Table(
    name="fit_message",
    comment="Every decoded FIT message that has no typed table above (file_id, file_creator, activity, sport, zones_target, user_profile, device_settings, training_file, climb_pro, battery, gps_metadata, unknown message numbers ...), stored whole as JSON so nothing is lost.",
    primary_key=["fit_file_id", "mesg", "idx"],
    columns=[
        *activity_ctx,
        expr("calendar_date", "DATE", "Day (wellness files).", "src.calendar_date"),
        expr("mesg", "VARCHAR", "Message name (e.g. sport_mesgs) or number for unknown messages.", "src.mesg"),
        idx_col,
        expr("fields", "JSON", "All fields of the message.", "b"),
    ],
)

# message name (as the SDK names it) -> typed table
FIT_MESSAGE_TABLES: dict[str, Table] = {
    "session_mesgs": fit_session_table,
    "lap_mesgs": fit_lap_table,
    "split_mesgs": fit_split_table,
    "record_mesgs": fit_record_table,
    "length_mesgs": fit_length_table,
    "event_mesgs": fit_event_table,
    "time_in_zone_mesgs": fit_time_in_zone_table,
    "device_info_mesgs": fit_device_info_table,
    "monitoring_mesgs": fit_monitoring_table,
    "monitoring_info_mesgs": fit_monitoring_info_table,
    "monitoring_hr_data_mesgs": fit_monitoring_hr_table,
    "stress_level_mesgs": fit_stress_level_table,
    "respiration_rate_mesgs": fit_respiration_rate_table,
    "spo2_data_mesgs": fit_spo2_table,
    "hrv_value_mesgs": fit_hrv_value_table,
    "hrv_status_summary_mesgs": fit_hrv_status_summary_table,
    "sleep_level_mesgs": fit_sleep_level_table,
    "sleep_assessment_mesgs": fit_sleep_assessment_table,
    "nap_event_mesgs": fit_nap_event_table,
}

FIT_TABLES: list[Table] = [
    fit_file_table,
    fit_session_table,
    fit_lap_table,
    fit_split_table,
    fit_record_table,
    fit_length_table,
    fit_event_table,
    fit_hrv_table,
    fit_time_in_zone_table,
    fit_device_info_table,
    fit_monitoring_table,
    fit_monitoring_info_table,
    fit_monitoring_hr_table,
    fit_stress_level_table,
    fit_respiration_rate_table,
    fit_spo2_table,
    fit_hrv_value_table,
    fit_hrv_status_summary_table,
    fit_sleep_level_table,
    fit_sleep_assessment_table,
    fit_nap_event_table,
    fit_message_table,
]


def _fill_extra(t: Table) -> None:
    for i, c in enumerate(t.columns):
        if c.expr == "__EXTRA__":
            consumed = sorted({x.key for x in t.columns if x.key is not None})
            # The composite speed/altitude/respiration columns consume both the enhanced and plain field.
            for k in ("enhanced_speed", "enhanced_altitude", "enhanced_respiration_rate"):
                if k in consumed:
                    consumed.append(k.removeprefix("enhanced_"))
            patch = "{" + ",".join(f'"{k}":null' for k in sorted(set(consumed))) + "}"
            t.columns[i] = Column(c.name, c.type, c.comment, f"json_merge_patch(b, '{patch}')", None)


for _t in FIT_TABLES:
    _fill_extra(_t)

_BY_NAME = {t.name: t for t in FIT_TABLES}


def fit_table(name: str) -> Table:
    return _BY_NAME[name]

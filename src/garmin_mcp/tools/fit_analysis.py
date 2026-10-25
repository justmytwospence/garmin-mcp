"""Analyses of an activity's FIT records: power duration curve, grade derivation and
grade-correlated stats, climb detection, HR drift, temperature correlation, Di2 shift
quality, HRV from RR intervals. Vendored from Taxuspt/garmin_mcp (MIT),
src/garmin_mcp/activity_analysis.py; they take plain record dicts (power_w,
cadence_rpm, heart_rate_bpm, speed_mps, altitude_m, distance_m, grade_pct,
temperature_c, timestamp), which fit/analysis builds from the mirror's fit_record rows.
"""

import datetime
from typing import Any, Dict, List, Optional


def _decode_gear_change(data: int) -> dict:
    """Decode a packed gear_change_data uint32 from a Di2 shift event.

    The FIT profile packs gear information as:
      bits 0-7:   rear gear number (1 = smallest/hardest cog)
      bits 8-15:  rear gear teeth count
      bits 16-23: front gear number (1 = inner/small ring)
      bits 24-31: front gear teeth count
    """
    rear_gear_num = data & 0xFF
    rear_teeth = (data >> 8) & 0xFF
    front_gear_num = (data >> 16) & 0xFF
    front_teeth = (data >> 24) & 0xFF
    return {
        "rear_gear_num": rear_gear_num,
        "front_gear_num": front_gear_num,
        "rear_teeth": rear_teeth if rear_teeth > 0 else None,
        "front_teeth": front_teeth if front_teeth > 0 else None,
    }


def _decode_left_right_balance(value, *, per_record: bool = False) -> Optional[float]:
    """Decode a FIT left_right_balance value to left power percentage.

    Session and lap messages use the uint16 left_right_balance_100 type
    (bit 15 = right, low 14 bits = percent x 100). Record messages use the
    uint8 left_right_balance type (bit 7 = right, low 7 bits = whole percent).
    When the right bit is set the percentage is the right leg's share.
    """
    if value is None:
        return None
    try:
        int_val = int(value)
    except (TypeError, ValueError):
        return None
    if per_record:
        right_dominant = bool(int_val & 0x80)
        pct = float(int_val & 0x7F)
    else:
        right_dominant = bool(int_val & 0x8000)
        pct = (int_val & 0x3FFF) / 100.0
    if pct > 100:
        return None
    if right_dominant:
        return round(100.0 - pct, 1)
    return round(pct, 1)


def _safe_avg(values: list) -> Optional[float]:
    """Return mean of a list of numbers, or None if empty."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _safe_round(value, ndigits: int = 1) -> Optional[float]:
    if value is None:
        return None
    return round(value, ndigits)


def _parse_fit_timestamp(value) -> Optional[datetime.datetime]:
    """Parse a FIT timestamp (datetime or its str()) as naive UTC, or None."""
    if isinstance(value, datetime.datetime):
        ts = value
    else:
        if not value:
            return None
        try:
            ts = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return ts


# ---------------------------------------------------------------------------
# Power Duration Curve (Mean Maximal Power)
# ---------------------------------------------------------------------------

_PDC_DURATIONS_S = [5, 30, 60, 300, 600, 1200, 3600]
_PDC_LABELS = ["5s", "30s", "1min", "5min", "10min", "20min", "60min"]


def _compute_power_duration_curve(records: List[Dict]) -> Optional[Dict]:
    """Compute best mean maximal power for standard durations.

    Uses a sliding window over power resampled to 1 Hz from the record
    timestamps (records without timestamps are taken as 1 s each).
    O(n * num_durations) — efficient for rides up to ~8 hours.
    """
    powers = [r.get("power_w") for r in records]
    if not any(p is not None for p in powers):
        return None

    # Fill None gaps with 0 (coasting) for window computation. Each sample is
    # held until the next record; a pause counts as 0 W rather than inventing
    # power, and zeros beyond the longest window cannot change any result.
    filled: List[float] = []
    for p, gap in zip(powers, _record_gaps_s(records)):
        held = _held_seconds(gap)
        filled.extend([p if p is not None else 0] * held)
        filled.extend([0] * min(gap - held, _PDC_DURATIONS_S[-1]))
    n = len(filled)
    if n == 0:
        return None

    # Precompute prefix sums for O(1) window queries
    prefix = [0] * (n + 1)
    for i, p in enumerate(filled):
        prefix[i + 1] = prefix[i] + p

    curve: Dict[str, Any] = {}
    for duration_s, label in zip(_PDC_DURATIONS_S, _PDC_LABELS):
        if n < duration_s:
            continue
        best = 0
        for i in range(n - duration_s + 1):
            window_sum = prefix[i + duration_s] - prefix[i]
            if window_sum > best:
                best = window_sum
        best_avg = best / duration_s
        if best_avg > 0:
            curve[label] = round(best_avg)

    return curve if curve else None


# ---------------------------------------------------------------------------
# Record timing (smart recording, pauses)
# ---------------------------------------------------------------------------

# Smart recording writes a record every few seconds (up to ~8 s); a longer gap
# between records is a pause (auto-pause, a stop), not slow sampling.
_MAX_RECORD_GAP_S = 10


def _record_gaps_s(records: List[Dict]) -> List[int]:
    """Seconds from each record to the next, from the record timestamps.

    The last record gets the preceding sampling interval. Falls back to 1 s
    per record (the 1 Hz assumption) when a timestamp is missing or
    unparseable or the timestamps go backwards.
    """
    times = [_parse_fit_timestamp(r.get("timestamp")) for r in records]
    if any(t is None for t in times):
        return [1] * len(records)
    gaps = [int((b - a).total_seconds()) for a, b in zip(times, times[1:])]
    if any(g < 0 for g in gaps):
        return [1] * len(records)
    if not records:
        return []
    return gaps + [_held_seconds(gaps[-1]) if gaps else 1]


def _held_seconds(gap_s: int) -> int:
    """Seconds a record's values hold for: its gap to the next record, or only
    1 s when that gap is a pause, so paused time is not attributed to it."""
    return gap_s if gap_s <= _MAX_RECORD_GAP_S else 1


def _record_seconds(records: List[Dict]) -> List[int]:
    """Seconds each record covers (1 per record for 1 Hz files)."""
    return [_held_seconds(gap) for gap in _record_gaps_s(records)]


# ---------------------------------------------------------------------------
# Grade derivation (devices that don't write the record `grade` field)
# ---------------------------------------------------------------------------

# Derive grade over a distance window centred on each record: long enough to
# smooth barometric/GPS altitude noise, short enough to follow real terrain.
_GRADE_WINDOW_M = 100.0
# Skip records whose window covers less distance than this (stopped, or the
# very start of the ride) -- a tiny distance turns altitude noise into huge grades.
_GRADE_MIN_SPAN_M = 20.0
_GRADE_CLAMP_PCT = 40.0
# Below this share of records carrying a device grade, treat it as absent.
_MIN_DEVICE_GRADE_SHARE = 0.05


def _derive_grades(records: List[Dict]) -> Optional[str]:
    """Fill missing record grade_pct from altitude and cumulative distance.

    Device-provided grade is kept when present on enough records. Otherwise
    each record without a grade gets delta-altitude / delta-distance over a
    _GRADE_WINDOW_M window centred on it, clamped to +/-_GRADE_CLAMP_PCT.

    Returns "device" or "derived" for where record grades came from, or None
    when no record ends up with a grade (e.g. no altitude or distance).
    """
    device_count = sum(1 for r in records if r.get("grade_pct") is not None)
    if records and device_count >= _MIN_DEVICE_GRADE_SHARE * len(records):
        return "device"

    points = [
        (i, float(r["distance_m"]), float(r["altitude_m"]))
        for i, r in enumerate(records)
        if r.get("distance_m") is not None and r.get("altitude_m") is not None
    ]
    half = _GRADE_WINDOW_M / 2
    derived = 0
    lo = hi = 0
    for idx, dist, _alt in points:
        while lo < len(points) - 1 and points[lo][1] < dist - half:
            lo += 1
        hi = max(hi, lo)
        while hi + 1 < len(points) and points[hi + 1][1] <= dist + half:
            hi += 1
        span = points[hi][1] - points[lo][1]
        if span < _GRADE_MIN_SPAN_M or records[idx].get("grade_pct") is not None:
            continue
        grade = (points[hi][2] - points[lo][2]) / span * 100
        records[idx]["grade_pct"] = round(
            max(-_GRADE_CLAMP_PCT, min(_GRADE_CLAMP_PCT, grade)), 2
        )
        derived += 1

    if derived:
        return "derived"
    return "device" if device_count else None


def _fill_shift_grades(shift_record_counts: List[tuple], records: List[Dict]) -> None:
    """Set grade_at_shift_pct from the nearest preceding record with a grade.

    shift_record_counts pairs each shift with how many records had been
    parsed when it occurred. Shifts that already carry a grade are kept.
    """
    last_grades: List[Optional[float]] = []
    last: Optional[float] = None
    for rec in records:
        if rec.get("grade_pct") is not None:
            last = rec["grade_pct"]
        last_grades.append(last)
    for shift, n_records in shift_record_counts:
        if "grade_at_shift_pct" in shift or n_records == 0:
            continue
        grade = last_grades[n_records - 1]
        if grade is not None:
            shift["grade_at_shift_pct"] = round(grade, 1)


# ---------------------------------------------------------------------------
# Climb detection + VAM
# ---------------------------------------------------------------------------

def _detect_climbs(
    records: List[Dict],
    min_elevation_gain_m: float = 50.0,
    min_avg_grade_pct: float = 3.0,
    min_duration_s: int = 120,
) -> List[Dict]:
    """Segment the ride into climbs based on sustained positive grade.

    A climb starts when grade rises above min_avg_grade_pct and ends when it
    drops back below it for more than 30 seconds.
    """
    if not records:
        return []

    climbs = []
    in_climb = False
    climb_start_idx = 0
    flat_count = 0
    flat_s = 0
    FLAT_TOLERANCE_S = 30
    held_s = _record_seconds(records)

    for i, rec in enumerate(records):
        grade = rec.get("grade_pct")
        if grade is None:
            continue

        if not in_climb:
            if grade >= min_avg_grade_pct:
                in_climb = True
                climb_start_idx = i
                flat_count = 0
                flat_s = 0
        else:
            if grade < min_avg_grade_pct:
                flat_count += 1
                flat_s += held_s[i]
                if flat_s > FLAT_TOLERANCE_S:
                    # End the climb at where the flat started
                    end_idx = i - flat_count
                    climb = _summarize_climb(records, climb_start_idx, end_idx, held_s)
                    if (climb.get("elevation_gain_m", 0) >= min_elevation_gain_m and
                            climb.get("duration_s", 0) >= min_duration_s):
                        climbs.append(climb)
                    in_climb = False
                    flat_count = 0
                    flat_s = 0
            else:
                flat_count = 0
                flat_s = 0

    # Close any open climb at end of ride
    if in_climb:
        climb = _summarize_climb(records, climb_start_idx, len(records) - 1, held_s)
        if (climb.get("elevation_gain_m", 0) >= min_elevation_gain_m and
                climb.get("duration_s", 0) >= min_duration_s):
            climbs.append(climb)

    return climbs


def _summarize_climb(
    records: List[Dict], start_idx: int, end_idx: int, held_s: List[int]
) -> Dict:
    """Compute aggregate stats for a climb segment.

    held_s gives the seconds each record covers (see _record_gaps_s).
    """
    segment = records[start_idx:end_idx + 1]
    if not segment:
        return {}

    segment_held_s = held_s[start_idx:end_idx + 1]
    duration_s = sum(segment_held_s)
    start_alt = segment[0].get("altitude_m")
    end_alt = segment[-1].get("altitude_m")
    elevation_gain = (end_alt - start_alt) if (start_alt is not None and end_alt is not None) else None

    powers = [r["power_w"] for r in segment if r.get("power_w") is not None]
    cadences = [r["cadence_rpm"] for r in segment if r.get("cadence_rpm") is not None]
    hrs = [r["heart_rate_bpm"] for r in segment if r.get("heart_rate_bpm") is not None]
    grades = [r["grade_pct"] for r in segment if r.get("grade_pct") is not None]
    speeds = [
        (r["speed_mps"], held) for r, held in zip(segment, segment_held_s)
        if r.get("speed_mps") is not None
    ]
    start_dist = segment[0].get("distance_m")
    end_dist = segment[-1].get("distance_m")

    result: Dict[str, Any] = {
        "start_time": segment[0].get("timestamp", ""),
        "end_time": segment[-1].get("timestamp", ""),
        "duration_s": duration_s,
    }

    if elevation_gain is not None:
        result["elevation_gain_m"] = round(elevation_gain, 1)
        if duration_s > 0:
            result["vam_m_per_hr"] = round((elevation_gain / duration_s) * 3600)

    if start_dist is not None and end_dist is not None:
        result["distance_m"] = round(end_dist - start_dist)  # cumulative odometer
    elif speeds:
        total_dist = sum(s * held for s, held in speeds)  # m/s * s = m per record
        result["distance_m"] = round(total_dist)

    if grades:
        result["avg_grade_pct"] = round(_safe_avg(grades), 1)
        result["max_grade_pct"] = round(max(grades), 1)

    if powers:
        result["avg_power_w"] = round(_safe_avg(powers))

    if cadences:
        result["avg_cadence_rpm"] = round(_safe_avg(cadences))

    if hrs:
        result["avg_hr_bpm"] = round(_safe_avg(hrs))

    return result


# ---------------------------------------------------------------------------
# Grade analysis
# ---------------------------------------------------------------------------

def _grade_analysis(records: List[Dict]) -> Optional[Dict]:
    """Bin per-second records by grade and report avg power/cadence/HR per bin."""
    bins = {
        "descending": {"label": "descending (<-3%)", "records": [], "time_s": 0},
        "flat": {"label": "flat (-3% to 3%)", "records": [], "time_s": 0},
        "gentle": {"label": "gentle (3-6%)", "records": [], "time_s": 0},
        "moderate": {"label": "moderate (6-9%)", "records": [], "time_s": 0},
        "steep": {"label": "steep (>9%)", "records": [], "time_s": 0},
    }

    has_grade = False
    for rec, held in zip(records, _record_seconds(records)):
        grade = rec.get("grade_pct")
        if grade is None:
            continue
        has_grade = True
        if grade < -3:
            bucket = "descending"
        elif grade < 3:
            bucket = "flat"
        elif grade < 6:
            bucket = "gentle"
        elif grade < 9:
            bucket = "moderate"
        else:
            bucket = "steep"
        bins[bucket]["records"].append(rec)
        bins[bucket]["time_s"] += held

    if not has_grade:
        return None

    result = {}
    for key, data in bins.items():
        recs = data["records"]
        if not recs:
            continue
        entry: Dict[str, Any] = {"time_s": data["time_s"]}
        powers = [r["power_w"] for r in recs if r.get("power_w") is not None]
        cadences = [r["cadence_rpm"] for r in recs if r.get("cadence_rpm") is not None]
        hrs = [r["heart_rate_bpm"] for r in recs if r.get("heart_rate_bpm") is not None]
        if powers:
            entry["avg_power_w"] = round(_safe_avg(powers))
        if cadences:
            entry["avg_cadence_rpm"] = round(_safe_avg(cadences))
        if hrs:
            entry["avg_hr_bpm"] = round(_safe_avg(hrs))
        result[key] = entry

    return result if result else None


# ---------------------------------------------------------------------------
# HR drift / cardiac drift (aerobic decoupling)
# ---------------------------------------------------------------------------

def _compute_hr_drift(records: List[Dict]) -> Optional[Dict]:
    """Compute aerobic decoupling (HR drift vs. power over the ride).

    Splits the ride into first and second halves by record count.
    Computes power:HR ratio for each half.
    Drift % = change in ratio from first to second half.
    Negative drift = HR increased relative to power (decoupling = less efficient).
    """
    MIN_DURATION_S = 3600  # require ≥60 min of data (by timestamps, not record count)

    filtered = []
    filtered_s = 0
    for r, held in zip(records, _record_seconds(records)):
        if (r.get("power_w") is not None and r.get("heart_rate_bpm") is not None
                and r["heart_rate_bpm"] > 0):
            filtered.append(r)
            filtered_s += held

    if filtered_s < MIN_DURATION_S:
        return None

    mid = len(filtered) // 2
    first_half = filtered[:mid]
    second_half = filtered[mid:]

    def power_hr_ratio(recs):
        avg_p = _safe_avg([r["power_w"] for r in recs])
        avg_hr = _safe_avg([r["heart_rate_bpm"] for r in recs])
        if avg_p is None or avg_hr is None or avg_hr == 0:
            return None
        return avg_p / avg_hr

    r1 = power_hr_ratio(first_half)
    r2 = power_hr_ratio(second_half)

    if r1 is None or r2 is None or r1 == 0:
        return None

    drift_pct = ((r2 - r1) / r1) * 100

    # Only negative drift (HR rising relative to power) is decoupling.
    if abs(drift_pct) < 5:
        interpretation = "well_coupled"
    elif drift_pct > 0:
        interpretation = "power_increased_vs_hr"
    elif drift_pct > -10:
        interpretation = "moderate_drift"
    else:
        interpretation = "significant_decoupling"

    return {
        "hr_drift_pct": round(drift_pct, 1),
        "first_half_power_hr_ratio": round(r1, 3),
        "second_half_power_hr_ratio": round(r2, 3),
        "interpretation": interpretation,
        "note": (
            "Negative drift = HR increased vs power (decoupling); below -10% suggests "
            "aerobic base is limiting factor. Positive drift = power increased vs HR "
            "(not decoupling)."
        ),
    }


# ---------------------------------------------------------------------------
# Temperature correlation
# ---------------------------------------------------------------------------

def _compute_temperature_stats(records: List[Dict]) -> Optional[Dict]:
    """Summarize temperature and its relationship to HR and power."""
    temps = [r["temperature_c"] for r in records if r.get("temperature_c") is not None]
    if not temps:
        return None

    avg_temp = _safe_avg(temps)
    temp_min = min(temps)
    temp_max = max(temps)

    # Compare HR and power in coolest vs hottest third of records
    sorted_by_temp = sorted(
        [r for r in records if r.get("temperature_c") is not None],
        key=lambda r: r["temperature_c"]
    )
    third = max(1, len(sorted_by_temp) // 3)
    coolest = sorted_by_temp[:third]
    hottest = sorted_by_temp[-third:]

    cool_hr = _safe_round(_safe_avg([r["heart_rate_bpm"] for r in coolest if r.get("heart_rate_bpm")]))
    hot_hr = _safe_round(_safe_avg([r["heart_rate_bpm"] for r in hottest if r.get("heart_rate_bpm")]))
    cool_power = _safe_round(_safe_avg([r["power_w"] for r in coolest if r.get("power_w") is not None]))
    hot_power = _safe_round(_safe_avg([r["power_w"] for r in hottest if r.get("power_w") is not None]))

    result: Dict[str, Any] = {
        "avg_temp_c": _safe_round(avg_temp),
        "min_temp_c": temp_min,
        "max_temp_c": temp_max,
        "temp_range_c": round(temp_max - temp_min, 1),
    }
    if cool_hr is not None:
        result["avg_hr_coolest_third_bpm"] = cool_hr
    if hot_hr is not None:
        result["avg_hr_hottest_third_bpm"] = hot_hr
    if cool_power is not None:
        result["avg_power_coolest_third_w"] = cool_power
    if hot_power is not None:
        result["avg_power_hottest_third_w"] = hot_power

    return result


# ---------------------------------------------------------------------------
# Weight / W/kg lookup
# ---------------------------------------------------------------------------

# Look this far back for a weigh-in: most people don't weigh in on ride days.
_WEIGHT_LOOKBACK_DAYS = 90


def _compute_shift_summary(shifts: list) -> dict:
    """Compute aggregate statistics over all shift events."""
    if not shifts:
        return {"total_shifts": 0, "note": "No DI2 shift events found in FIT file"}

    quality_counts: Dict[str, int] = {
        "proactive": 0, "reactive": 0, "coasting": 0, "spun_out": 0, "unknown": 0
    }
    terrain_quality: Dict[str, Dict[str, int]] = {
        "climbing": {"proactive": 0, "reactive": 0, "coasting": 0, "spun_out": 0, "unknown": 0},
        "flat": {"proactive": 0, "reactive": 0, "coasting": 0, "spun_out": 0, "unknown": 0},
        "descending": {"proactive": 0, "reactive": 0, "coasting": 0, "spun_out": 0, "unknown": 0},
    }
    front_shifts = 0
    rear_shifts = 0
    gear_usage: dict = {}
    cadences_at_shift = []

    # Burst detection: 3+ shifts within 10 seconds count as one episode, then
    # skip past that burst. Shifts without a parseable timestamp are ignored.
    BURST_MIN_SHIFTS = 3
    BURST_WINDOW_S = 10
    shift_times = sorted(
        t for t in (_parse_fit_timestamp(s.get("timestamp")) for s in shifts) if t is not None
    )
    panic_bursts = 0
    burst_i = 0
    while burst_i < len(shift_times):
        burst_end = burst_i
        while (burst_end + 1 < len(shift_times) and
               (shift_times[burst_end + 1] - shift_times[burst_i]).total_seconds() <= BURST_WINDOW_S):
            burst_end += 1
        if burst_end - burst_i + 1 >= BURST_MIN_SHIFTS:
            panic_bursts += 1
            burst_i = burst_end + 1
        else:
            burst_i += 1

    for s in shifts:
        quality = s.get("quality", "unknown")
        quality_counts[quality] = quality_counts.get(quality, 0) + 1

        # Terrain classification
        grade = s.get("grade_at_shift_pct")
        if grade is not None:
            if grade >= 3:
                terrain = "climbing"
            elif grade <= -3:
                terrain = "descending"
            else:
                terrain = "flat"
        else:
            terrain = "flat"  # default
        terrain_quality[terrain][quality] = terrain_quality[terrain].get(quality, 0) + 1

        event = s.get("event", "")
        if "front" in event:
            front_shifts += 1
        else:
            rear_shifts += 1

        combo = s.get("gear_combo")
        if combo:
            gear_usage[combo] = gear_usage.get(combo, 0) + 1

        cad = s.get("cadence_at_shift_rpm")
        if cad is not None:
            cadences_at_shift.append(cad)

    total = len(shifts)
    proactive = quality_counts.get("proactive", 0)
    reactive = quality_counts.get("reactive", 0)

    summary: Dict[str, Any] = {
        "total_shifts": total,
        "front_shifts": front_shifts,
        "rear_shifts": rear_shifts,
        "proactive_shifts": proactive,
        "reactive_shifts": reactive,
        "coasting_shifts": quality_counts.get("coasting", 0),
        "spun_out_shifts": quality_counts.get("spun_out", 0),
        "proactive_pct": round(proactive / total * 100, 1) if total else 0,
        "reactive_pct": round(reactive / total * 100, 1) if total else 0,
        "panic_burst_episodes": panic_bursts,
        "gear_usage": dict(sorted(gear_usage.items(), key=lambda x: -x[1])),
    }

    # Include terrain breakdown only if grade data was present
    has_terrain_data = any(s.get("grade_at_shift_pct") is not None for s in shifts)
    if has_terrain_data:
        terrain_summary = {}
        for terrain, counts in terrain_quality.items():
            t_total = sum(counts.values())
            if t_total > 0:
                terrain_summary[terrain] = {
                    "total": t_total,
                    "proactive": counts.get("proactive", 0),
                    "reactive": counts.get("reactive", 0),
                    "reactive_pct": round(counts.get("reactive", 0) / t_total * 100, 1),
                }
        if terrain_summary:
            summary["by_terrain"] = terrain_summary

    if cadences_at_shift:
        summary["avg_cadence_at_shift_rpm"] = round(
            sum(cadences_at_shift) / len(cadences_at_shift), 1
        )
        summary["min_cadence_at_shift_rpm"] = min(cadences_at_shift)
        summary["max_cadence_at_shift_rpm"] = max(cadences_at_shift)

    return summary


# ---------------------------------------------------------------------------
# Main FIT parsing logic
# ---------------------------------------------------------------------------

def _compute_hrv_metrics(rr_intervals_s: List[float]) -> Optional[Dict]:
    """Compute standard time-domain HRV metrics from R-R intervals (seconds).

    Returns RMSSD, SDNN, pNN50, mean R-R, and count. These are the standard
    intra-workout HRV metrics used in sports science and HRV-guided training
    platforms (HRV4Training, Elite HRV, Polar's recovery metrics, etc.).

    Requires at least 10 R-R intervals to produce stable results.
    """
    if len(rr_intervals_s) < 10:
        return None

    rr_ms = [r * 1000.0 for r in rr_intervals_s]
    diffs = [rr_ms[i + 1] - rr_ms[i] for i in range(len(rr_ms) - 1)]

    # RMSSD: root mean square of successive differences
    squared = [d * d for d in diffs]
    rmssd = (sum(squared) / len(squared)) ** 0.5 if squared else 0.0

    # SDNN: standard deviation of all N-N intervals
    mean_rr = sum(rr_ms) / len(rr_ms)
    sdnn = (sum((r - mean_rr) ** 2 for r in rr_ms) / (len(rr_ms) - 1)) ** 0.5 if len(rr_ms) > 1 else 0.0

    # pNN50: percentage of pairs differing by more than 50 ms
    nn50 = sum(1 for d in diffs if abs(d) > 50.0)
    pnn50 = 100.0 * nn50 / len(diffs) if diffs else 0.0

    return {
        "rmssd_ms": round(rmssd, 1),
        "sdnn_ms": round(sdnn, 1),
        "pnn50_pct": round(pnn50, 2),
        "mean_rr_ms": round(mean_rr, 1),
        "mean_hr_bpm": round(60000.0 / mean_rr, 1) if mean_rr > 0 else None,
        "rr_count": len(rr_ms),
    }


def _primary_session_index(sessions: List[Dict]) -> int:
    """Leg of a multisport file to analyse: the only cycling leg, else the longest."""
    cycling = [i for i, s in enumerate(sessions) if s.get("sport") == "cycling"]
    if len(cycling) == 1:
        return cycling[0]
    return max(
        range(len(sessions)),
        key=lambda i: sessions[i].get("total_elapsed_time_s") or 0,
    )


def _records_in_session(records: List[Dict], session: Dict) -> Optional[List[Dict]]:
    """Records within the session's [start_time, start_time + elapsed) window.

    Returns None when the window or any record timestamp cannot be determined.
    """
    start = _parse_fit_timestamp(session.get("start_time"))
    elapsed = session.get("total_elapsed_time_s")
    if start is None or not elapsed:
        return None
    end = start + datetime.timedelta(seconds=float(elapsed))
    times = [_parse_fit_timestamp(r.get("timestamp")) for r in records]
    if any(t is None for t in times):
        return None
    return [r for r, t in zip(records, times) if start <= t < end]



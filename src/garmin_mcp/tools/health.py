"""Health and wellness tools (upstream health_wellness): per-day datasets from the
mirror, ranges and weekly aggregates computed from its typed tables."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from ..db.raw import read_current
from ..mirror import GarminNotFound, Provenance, note_provenance
from .base import READ, NoData, Registry, drop_none, iso_days, parse_date, parse_range


def _hours_from_recovery_minutes(minutes: Any, phrase: Any = None) -> float | None:
    if phrase == "REACHED_ZERO":
        return 0.0
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes < 0:
        return None
    return round(float(minutes) / 60.0, 1)


def _recovery_state(remaining_hours: float | None, phrase: Any) -> str:
    if phrase == "REACHED_ZERO" or remaining_hours == 0:
        return "recovered"
    if remaining_hours is None:
        return "unknown"
    if remaining_hours <= 6:
        return "nearly_recovered"
    if remaining_hours <= 24:
        return "recovering"
    return "not_recovered"


def curate_training_readiness(r: dict[str, Any]) -> dict[str, Any]:
    return drop_none({
        "date": r.get("calendarDate"), "timestamp": r.get("timestampLocal"), "context": r.get("inputContext"),
        "level": r.get("level"), "score": r.get("score"), "feedback": r.get("feedbackShort"),
        "sleep_score": r.get("sleepScore"), "sleep_factor_percent": r.get("sleepScoreFactorPercent"), "sleep_factor_feedback": r.get("sleepScoreFactorFeedback"),
        "recovery_time_hours": _hours_from_recovery_minutes(r.get("recoveryTime"), r.get("recoveryTimeChangePhrase")),
        "recovery_factor_percent": r.get("recoveryTimeFactorPercent"), "recovery_factor_feedback": r.get("recoveryTimeFactorFeedback"),
        "training_load_factor_percent": r.get("acwrFactorPercent"), "training_load_feedback": r.get("acwrFactorFeedback"), "acute_load": r.get("acuteLoad"),
        "hrv_factor_percent": r.get("hrvFactorPercent"), "hrv_factor_feedback": r.get("hrvFactorFeedback"), "hrv_weekly_avg": r.get("hrvWeeklyAverage"),
        "stress_history_factor_percent": r.get("stressHistoryFactorPercent"), "stress_history_feedback": r.get("stressHistoryFactorFeedback"),
        "sleep_history_factor_percent": r.get("sleepHistoryFactorPercent"), "sleep_history_feedback": r.get("sleepHistoryFactorFeedback"),
    })


def extract_sleep_summary(sleep_data: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    daily = sleep_data.get("dailySleepDTO") or {}
    if daily:
        summary["sleep_seconds"] = daily.get("sleepTimeSeconds")
        summary["nap_seconds"] = daily.get("napTimeSeconds")
        summary["sleep_start"] = daily.get("sleepStartTimestampGMT")
        summary["sleep_end"] = daily.get("sleepEndTimestampGMT")
        overall = (daily.get("sleepScores") or {}).get("overall") or {}
        summary["sleep_score"] = overall.get("value")
        summary["sleep_score_qualifier"] = overall.get("qualifierKey")
        summary["deep_sleep_seconds"] = daily.get("deepSleepSeconds")
        summary["light_sleep_seconds"] = daily.get("lightSleepSeconds")
        summary["rem_sleep_seconds"] = daily.get("remSleepSeconds")
        summary["awake_seconds"] = daily.get("awakeSleepSeconds")
        summary["awake_count"] = daily.get("awakeCount")
        summary["avg_sleep_stress"] = daily.get("avgSleepStress")
    summary["restless_moments_count"] = sleep_data.get("restlessMomentsCount", daily.get("restlessMomentsCount"))
    summary["resting_heart_rate_bpm"] = sleep_data.get("restingHeartRate", daily.get("restingHeartRate"))
    spo2 = sleep_data.get("wellnessSpO2SleepSummaryDTO") or {}
    if spo2:
        summary["avg_spo2_percent"] = spo2.get("averageSPO2", spo2.get("averageSpo2"))
        summary["lowest_spo2_percent"] = spo2.get("lowestSPO2", spo2.get("lowestSpo2"))
    if "avgOvernightHrv" in sleep_data:
        summary["avg_overnight_hrv"] = sleep_data.get("avgOvernightHrv")
    total = summary.get("sleep_seconds")
    if total and total > 0:
        for pk, pct in (("deep_sleep_seconds", "deep_sleep_percent"), ("light_sleep_seconds", "light_sleep_percent"), ("rem_sleep_seconds", "rem_sleep_percent")):
            if summary.get(pk) is not None:
                summary[pct] = round(summary[pk] / total * 100, 1)
        summary["sleep_hours"] = round(total / 3600, 2)
    return drop_none(summary)


def _linear_fit(xs: list[float], ys: list[float]) -> dict[str, Any] | None:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    intercept = my - slope * mx
    se = None
    if n > 2:
        sse = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys, strict=True))
        se = (sse / (n - 2) / sxx) ** 0.5
    return {"slope": slope, "intercept": intercept, "se_slope": se, "n": n}


def _body_comp_gate_failure(xs: list[float]) -> str | None:
    n = len(xs)
    span = max(xs) - min(xs)
    if span < 21:
        return f"readings span only {span} days; need at least 21 to separate a real trend from water-weight noise"
    mx = sum(xs) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return "all readings fall on the same day"
    lev = [1 / n + (x - mx) ** 2 / sxx for x in xs]
    total = sum(lev)
    cluster = max(sum(lv for x, lv in zip(xs, lev, strict=True) if 0 <= x - a <= 2) for a in xs)
    if cluster > total / 2:
        return f"a single cluster of readings within 48h of each other accounts for {cluster / total:.0%} of the fit's statistical leverage -- the trend across the full window is not reliably distinguishable from noise in that cluster alone"
    return None


def _week_start(d: date, first_day: str = "monday") -> date:
    offset = {"monday": 0, "sunday": 6, "saturday": 5}.get(first_day.lower(), 0)
    return d - timedelta(days=(d.weekday() - offset) % 7)


def register(reg: Registry) -> None:
    m = reg.mirror

    def day_payload(name: str, day: str, label: str) -> Any:
        try:
            return m.day(name, day).data
        except GarminNotFound as err:
            raise NoData(f"No {label} found for {day} ({err})") from err

    def weigh_ins_between(s: date, e: date) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        chunks = m.chunks("weigh_ins", s.isoformat(), e.isoformat()).data
        entries: list[dict[str, Any]] = []
        for c in chunks:
            for day in (c or {}).get("dailyWeightSummaries") or []:
                for w in day.get("allWeightMetrics") or []:
                    d = w.get("calendarDate")
                    if d and s.isoformat() <= d <= e.isoformat():
                        entries.append(w)
        entries.sort(key=lambda w: (w.get("calendarDate") or "", w.get("timestampGMT") or 0))
        avg: dict[str, Any] = {}
        if entries:
            for k in ("weight", "bmi", "bodyFat", "bodyWater", "boneMass", "muscleMass", "physiqueRating", "visceralFat", "metabolicAge"):
                vals = [w[k] for w in entries if w.get(k) is not None]
                avg[k] = round(sum(vals) / len(vals), 2) if vals else None
        return entries, avg

    @reg.tool(annotations=READ)
    def get_stats(date: str) -> Any:
        """Get daily activity stats with curated essential metrics: steps, calories, heart rate, stress, body battery, SpO2 and respiration.

        Note: bmr_calories is Garmin's bmrKilocalories field (RMR, not true basal metabolic rate). For the current date, calorie and
        duration fields are a partial-day accumulator.

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        stats = day_payload("user_summary", date, "stats")
        r1 = lambda v: round(v, 1) if v is not None else None  # noqa: E731
        return drop_none({
            "date": stats.get("calendarDate"), "total_steps": stats.get("totalSteps"), "daily_step_goal": stats.get("dailyStepGoal"), "distance_meters": stats.get("totalDistanceMeters"),
            "floors_ascended": r1(stats.get("floorsAscended")), "floors_descended": r1(stats.get("floorsDescended")),
            "total_calories": stats.get("totalKilocalories"), "active_calories": stats.get("activeKilocalories"), "bmr_calories": stats.get("bmrKilocalories"),
            "highly_active_seconds": stats.get("highlyActiveSeconds"), "active_seconds": stats.get("activeSeconds"), "sedentary_seconds": stats.get("sedentarySeconds"), "sleeping_seconds": stats.get("sleepingSeconds"),
            "moderate_intensity_minutes": stats.get("moderateIntensityMinutes"), "vigorous_intensity_minutes": stats.get("vigorousIntensityMinutes"), "intensity_minutes_goal": stats.get("intensityMinutesGoal"),
            "min_heart_rate_bpm": stats.get("minHeartRate"), "max_heart_rate_bpm": stats.get("maxHeartRate"), "resting_heart_rate_bpm": stats.get("restingHeartRate"), "last_7_days_avg_resting_hr": stats.get("lastSevenDaysAvgRestingHeartRate"),
            "avg_stress_level": stats.get("averageStressLevel"), "max_stress_level": stats.get("maxStressLevel"), "stress_qualifier": stats.get("stressQualifier"),
            "body_battery_charged": stats.get("bodyBatteryChargedValue"), "body_battery_drained": stats.get("bodyBatteryDrainedValue"), "body_battery_highest": stats.get("bodyBatteryHighestValue"),
            "body_battery_lowest": stats.get("bodyBatteryLowestValue"), "body_battery_current": stats.get("bodyBatteryMostRecentValue"),
            "avg_spo2_percent": stats.get("averageSpo2"), "lowest_spo2_percent": stats.get("lowestSpo2"),
            "avg_waking_respiration": stats.get("avgWakingRespirationValue"), "highest_respiration": stats.get("highestRespirationValue"), "lowest_respiration": stats.get("lowestRespirationValue"),
        })

    @reg.tool(annotations=READ)
    def get_stats_range(start_date: str, end_date: str) -> Any:
        """Get lightweight per-day calorie and step totals for a date range (total/active/resting calories and steps per day).

        resting_calories is Garmin's bmrKilocalories (RMR). is_partial is true for the current date. Days with no device data return
        has_data: false and null values rather than zeros. Maximum range: 28 days per call.

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
        """
        s, e = parse_range(start_date, end_date, 28)
        m.range_days("daily_summary_v2", start_date, end_date)
        rows = {r["calendar_date"]: r for r in m.rows("SELECT calendar_date, calories_total, calories_active, calories_resting, steps FROM daily_summary_v2 WHERE calendar_date BETWEEN ? AND ?", [s, e])}
        today = m.today().isoformat()
        daily = []
        for d in iso_days(s, e):
            r = rows.get(d)
            daily.append({"date": d, "total_calories": (r or {}).get("calories_total"), "active_calories": (r or {}).get("calories_active"), "resting_calories": (r or {}).get("calories_resting"), "steps": (r or {}).get("steps"), "has_data": r is not None, "is_partial": d == today})
        if not any(d["has_data"] for d in daily):
            return f"No stats found between {start_date} and {end_date}."
        return {"start_date": start_date, "end_date": end_date, "days": daily}

    @reg.tool(annotations=READ)
    def get_energy_balance(start_date: str, end_date: str) -> Any:
        """Estimate a composition-derived TDEE from logged intake, Garmin's claimed expenditure, and body-composition change over a date range.

        composition_derived_tdee = mean logged intake + implied deficit, where the deficit comes from a least-squares fit of fat and
        fat-free mass change across every qualifying smart-scale reading (7700 kcal/kg fat, 1800 kcal/kg fat-free mass; literature
        approximations, treat as a rough estimate). difference_kcal_per_day (Garmin's claim minus this estimate) cannot distinguish
        Garmin overestimating from under-logged intake. The derived TDEE is suppressed (gate_note explains) when readings span under
        three weeks or cluster within 48 hours. Days without logged food are excluded from the intake mean (low_item_count_dates lists
        low-confidence days); the current partial day and days without device data are excluded from expenditure. Needs at least 2
        readings with both weight and body fat. Maximum range: 61 days.

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
        """
        s, e = parse_range(start_date, end_date, 61)
        days_requested = (e - s).days + 1
        try:
            intake = m.report("nutrition_food_logs_range", "/nutrition-service/food/logs/range", {"startDate": start_date, "endDate": end_date}, f"{start_date}_{end_date}").data
        except GarminNotFound:
            intake = None
        intake_by_date: dict[str, dict[str, Any]] = {}
        for day in (intake or {}).get("dailyNutritionSummaries") or []:
            ds = day.get("mealDate")
            if ds:
                content = day.get("dailyNutritionContent") or {}
                intake_by_date[ds] = {"calories": content.get("calories"), "item_count": sum(len(meal.get("loggedFoods") or []) for meal in (day.get("mealDetails") or []))}
        m.range_days("daily_summary_v2", start_date, end_date)
        today = m.today().isoformat()
        exp = [r["calories_total"] for r in m.rows("SELECT calendar_date, calories_total FROM daily_summary_v2 WHERE calendar_date BETWEEN ? AND ?", [s, e]) if r["calendar_date"] != today and r["calories_total"] is not None]
        entries, _ = weigh_ins_between(s, e)
        logged = [(d, v["calories"]) for d, v in intake_by_date.items() if v.get("item_count", 0) > 0 and v.get("calories") is not None]
        mean_intake = sum(c for _, c in logged) / len(logged) if logged else None
        mean_exp = sum(exp) / len(exp) if exp else None
        readings = sorted((w["calendarDate"], w["weight"], w["bodyFat"]) for w in entries if w.get("calendarDate") and w.get("weight") is not None and w.get("bodyFat") is not None)
        result: dict[str, Any] = {
            "start_date": start_date, "end_date": end_date, "days_requested": days_requested,
            "intake": {"mean_calories_per_day": round(mean_intake, 1) if mean_intake is not None else None, "days_included": len(logged), "days_excluded_unlogged": days_requested - len(logged), "low_item_count_dates": sorted(d for d, v in intake_by_date.items() if 0 < v.get("item_count", 0) < 3)},
            "expenditure_garmin": {"mean_total_calories_per_day": round(mean_exp, 1) if mean_exp is not None else None, "days_included": len(exp), "days_excluded": days_requested - len(exp)},
        }
        if len(readings) >= 2:
            day_zero = date.fromisoformat(readings[0][0])
            xs = [float((date.fromisoformat(d) - day_zero).days) for d, _, _ in readings]
            w_kg = [w / 1000 for _, w, _ in readings]
            bf = [b for _, _, b in readings]
            fat_kg = [w * b / 100 for w, b in zip(w_kg, bf, strict=True)]
            span = xs[-1] - xs[0]
            wfit, ffit = _linear_fit(xs, w_kg), _linear_fit(xs, fat_kg)
            result["body_composition"] = {"readings_used": len(readings), "first_reading_date": readings[0][0], "last_reading_date": readings[-1][0], "days_between_readings": span,
                                          "observed_first_weight_kg": round(w_kg[0], 2), "observed_last_weight_kg": round(w_kg[-1], 2), "observed_first_body_fat_percent": bf[0], "observed_last_body_fat_percent": bf[-1]}
            gate = _body_comp_gate_failure(xs)
            if wfit and ffit and span >= 1:
                fat_change, weight_change = ffit["slope"] * span, wfit["slope"] * span
                result["body_composition"].update({"fitted_first_weight_kg": round(wfit["intercept"] + wfit["slope"] * xs[0], 2), "fitted_last_weight_kg": round(wfit["intercept"] + wfit["slope"] * xs[-1], 2),
                                                   "weight_change_kg": round(weight_change, 2), "fat_mass_change_kg": round(fat_change, 3), "lean_mass_change_kg": round(weight_change - fat_change, 3)})
                if gate:
                    result["gate_note"] = f"Derived TDEE suppressed: {gate}"
                elif mean_intake is not None:
                    deficit_series = [-(f * 7700 + (w - f) * 1800) for w, f in zip(w_kg, fat_kg, strict=True)]
                    dfit = _linear_fit(xs, deficit_series) or {"slope": 0.0, "se_slope": None}
                    tdee = mean_intake + dfit["slope"]
                    derived: dict[str, Any] = {"implied_deficit_kcal_per_day": round(dfit["slope"], 1), "implied_deficit_uncertainty_kcal_per_day": round(dfit["se_slope"], 1) if dfit["se_slope"] is not None else None,
                                               "composition_derived_tdee_kcal_per_day": round(tdee, 1), "garmin_tdee_kcal_per_day": round(mean_exp, 1) if mean_exp is not None else None,
                                               "difference_kcal_per_day": round(mean_exp - tdee, 1) if mean_exp is not None else None}
                    if dfit["se_slope"] is None:
                        derived["uncertainty_note"] = "Only 2 readings -- no residual variance to estimate uncertainty from."
                    derived["note"] = "composition_derived_tdee is a model estimate, not ground truth; difference_kcal_per_day is not attributable to Garmin's model or to under-logged intake without independent validation."
                    result["derived"] = derived
                    result["assumptions"] = {"fat_kcal_per_kg": 7700, "lean_mass_kcal_per_kg": 1800, "note": "Standard energy-density approximations from body-composition literature; fat-free mass change over short windows is often water/glycogen."}
                else:
                    result["derived_note"] = "Body composition trend fit but no qualifying intake days to derive a TDEE from."
            else:
                result["derived_note"] = "Body composition readings found but a trend could not be fit (readings span zero days)."
        elif readings:
            result["body_composition_note"] = f"Only 1 qualifying body-composition reading (weight + body fat %) in range, on {readings[0][0]}. Need at least 2 to fit a trend."
        else:
            result["body_composition_note"] = "No body-composition readings with both weight and body fat % in range."
        if not logged and not exp and not readings:
            return f"No usable data found between {start_date} and {end_date}."
        return result

    @reg.tool(annotations=READ)
    def get_user_summary(date: str) -> Any:
        """Get user summary data (compatible with garminconnect-ha)

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        return day_payload("user_summary", date, "user summary")

    @reg.tool(annotations=READ)
    def get_body_composition(start_date: str, end_date: str | None = None) -> Any:
        """Get body composition data for a single date or date range

        Args:
            start_date: Date in YYYY-MM-DD format or start date if end_date provided
            end_date: Optional end date in YYYY-MM-DD format for date range
        """
        s = parse_date(start_date, "start_date")
        e = parse_date(end_date, "end_date") if end_date else s
        entries, avg = weigh_ins_between(s, e)
        if not entries:
            return f"No body composition data found between {start_date} and {end_date}" if end_date else f"No body composition data found for {start_date}"
        return {"startDate": s.isoformat(), "endDate": e.isoformat(), "dateWeightList": entries, "totalAverage": avg}

    @reg.tool(annotations=READ)
    def get_stats_and_body(date: str) -> Any:
        """Get stats and body composition data

        Args:
            date: Date in YYYY-MM-DD format
        """
        d = parse_date(date)
        stats = day_payload("user_summary", date, "stats")
        _, avg = weigh_ins_between(d, d)
        return {**stats, **avg}

    @reg.tool(annotations=READ)
    def get_steps_data(date: str) -> Any:
        """Get detailed steps data with 15-minute intervals (full interval data; for a compact summary use get_stats).

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        return day_payload("steps_chart", date, "steps data")

    @reg.tool(annotations=READ)
    def get_daily_steps(start_date: str, end_date: str) -> Any:
        """Get steps data for a date range

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
        """
        parse_range(start_date, end_date, 366)
        data = m.range_days("steps_daily", start_date, end_date).data
        if not data:
            return f"No daily steps data found between {start_date} and {end_date}"
        return data

    @reg.tool(annotations=READ)
    def get_training_readiness(date: str) -> Any:
        """Get training readiness data with curated metrics (score and contributing factors; one entry per snapshot of the day).

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        try:
            data = m.range_day("training_readiness", date).data
        except GarminNotFound as err:
            raise NoData(f"No training readiness data found for {date} ({err})") from err
        items = data if isinstance(data, list) else [data]
        return [curate_training_readiness(r) for r in items if isinstance(r, dict)]

    @reg.tool(annotations=READ)
    def get_body_battery(start_date: str, end_date: str) -> Any:
        """Get body battery data with events

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
        """
        parse_range(start_date, end_date, 366)
        days = m.range_days("body_battery", start_date, end_date).data
        if not days:
            return f"No body battery data found between {start_date} and {end_date}"
        out = []
        for day in days:
            entry: dict[str, Any] = {"date": day.get("date"), "charged": day.get("charged"), "drained": day.get("drained"), "events": []}
            for ev in day.get("bodyBatteryActivityEvent") or []:
                ce = {"type": ev.get("eventType"), "start_time": ev.get("eventStartTimeGmt"), "body_battery_impact": ev.get("bodyBatteryImpact"), "feedback": ev.get("shortFeedback")}
                if ev.get("durationInMilliseconds") is not None:
                    ce["duration_minutes"] = round(ev["durationInMilliseconds"] / 60000, 1)
                entry["events"].append(ce)
            fb = day.get("bodyBatteryDynamicFeedbackEvent") or {}
            if fb:
                entry["current_feedback"] = fb.get("feedbackShortType")
                entry["body_battery_level"] = fb.get("bodyBatteryLevel")
            out.append(entry)
        return out

    @reg.tool(annotations=READ)
    def get_body_battery_events(date: str) -> Any:
        """Get body battery events data

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        return day_payload("body_battery_events", date, "body battery events")

    @reg.tool(annotations=READ)
    def get_blood_pressure(start_date: str, end_date: str) -> Any:
        """Get blood pressure data

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
        """
        s, e = parse_range(start_date, end_date, 366)
        chunks = m.chunks("blood_pressure", start_date, end_date).data
        summaries = []
        for c in chunks:
            for day in (c or {}).get("measurementSummaries") or []:
                d = day.get("measurementDate") or day.get("calendarDate")
                if d and s.isoformat() <= str(d)[:10] <= e.isoformat():
                    summaries.append(day)
        if not summaries:
            return f"No blood pressure data found between {start_date} and {end_date}"
        return {"from": start_date, "until": end_date, "measurementSummaries": summaries}

    @reg.tool(annotations=READ)
    def get_floors(date: str) -> Any:
        """Get floors climbed data

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        return day_payload("floors", date, "floors data")

    @reg.tool(annotations=READ)
    def get_rhr_day(date: str) -> Any:
        """Get resting heart rate data

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        chunks = m.chunks("wellness_metrics", date, date).data
        values = []
        for c in chunks:
            for v in (((c or {}).get("allMetrics") or {}).get("metricsMap") or {}).get("WELLNESS_RESTING_HEART_RATE") or []:
                if v.get("calendarDate") == date:
                    values.append(v)
        if not values:
            raise NoData(f"No resting heart rate data found for {date}")
        return {"statisticsStartDate": date, "statisticsEndDate": date, "allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": values}}}

    @reg.tool(annotations=READ)
    def get_heart_rates(date: str) -> Any:
        """Get full heart rate time-series data (2-minute intervals, ~25KB; for a compact summary use get_heart_rates_summary).

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        return day_payload("heart_rates", date, "heart rate data")

    @reg.tool(annotations=READ)
    def get_heart_rates_summary(date: str) -> Any:
        """Get heart rate summary with essential metrics (lightweight version, ~500 bytes).

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        hr = day_payload("heart_rates", date, "heart rate data")
        summary = {"date": hr.get("calendarDate"), "max_heart_rate_bpm": hr.get("maxHeartRate"), "min_heart_rate_bpm": hr.get("minHeartRate"), "resting_heart_rate_bpm": hr.get("restingHeartRate"), "last_7_days_avg_resting_hr": hr.get("lastSevenDaysAvgRestingHeartRate")}
        valid = [v[1] for v in hr.get("heartRateValues") or [] if v and len(v) > 1 and v[1] and v[1] > 0]
        if valid:
            summary["avg_heart_rate_bpm"] = round(sum(valid) / len(valid), 1)
            summary["data_points_count"] = len(valid)
        elif _archived(m, "heart_rate_day", date):
            summary["note"] = "Intraday samples for this day are archived by Garmin; an archive reload has been requested or is queued."
        return drop_none(summary)

    @reg.tool(annotations=READ)
    def get_hydration_data(date: str) -> Any:
        """Get hydration data

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        return day_payload("hydration", date, "hydration data")

    @reg.tool(annotations=READ)
    def get_sleep_data(date: str) -> Any:
        """Get full sleep data with all details (~50KB; for a compact summary use get_sleep_summary).

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        return day_payload("sleep", date, "sleep data")

    @reg.tool(annotations=READ)
    def get_sleep_summary(date: str) -> Any:
        """Get sleep summary with only essential metrics (lightweight version, ~350 bytes).

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        return extract_sleep_summary(day_payload("sleep", date, "sleep summary"))

    @reg.tool(annotations=READ)
    def get_sleep_summary_range(start_date: str, end_date: str) -> Any:
        """Get lightweight sleep summaries for every night in a date range (same curated metrics as get_sleep_summary). Maximum: 90 nights per call.

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
        """
        s, e = parse_range(start_date, end_date, 90)
        nights = []
        for d, payload in m.days("sleep", start_date, end_date).data.items():
            entry = {"date": d, **extract_sleep_summary(payload)}
            if len(entry) > 1:
                nights.append(entry)
        if not nights:
            return f"No sleep data found between {start_date} and {end_date}."
        return {"start_date": start_date, "end_date": end_date, "nights_requested": (e - s).days + 1, "nights_returned": len(nights), "nights": nights}

    @reg.tool(annotations=READ)
    def get_stress_data(date: str) -> Any:
        """Get full stress time-series data (~35KB, includes body battery; for a compact summary use get_stress_summary).

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        return day_payload("stress", date, "stress data")

    @reg.tool(annotations=READ)
    def get_stress_summary(date: str) -> Any:
        """Get stress summary with essential metrics (lightweight version, ~400 bytes).

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        sd = day_payload("stress", date, "stress data")
        summary: dict[str, Any] = {"date": sd.get("calendarDate"), "max_stress_level": sd.get("maxStressLevel"), "avg_stress_level": sd.get("avgStressLevel")}
        valid = [v[1] for v in sd.get("stressValuesArray") or [] if v and len(v) > 1 and v[1] and v[1] > 0]
        if valid:
            total = len(valid)
            summary["rest_percent"] = round(sum(1 for v in valid if v < 26) / total * 100, 1)
            summary["low_stress_percent"] = round(sum(1 for v in valid if 26 <= v < 51) / total * 100, 1)
            summary["medium_stress_percent"] = round(sum(1 for v in valid if 51 <= v < 76) / total * 100, 1)
            summary["high_stress_percent"] = round(sum(1 for v in valid if v >= 76) / total * 100, 1)
            summary["data_points_count"] = total
        return drop_none(summary)

    @reg.tool(annotations=READ)
    def get_respiration_data(date: str) -> Any:
        """Get full respiration time-series data (~20KB; for a compact summary use get_respiration_summary).

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        return day_payload("respiration", date, "respiration data")

    @reg.tool(annotations=READ)
    def get_respiration_summary(date: str) -> Any:
        """Get respiration summary with essential metrics (lightweight version, ~300 bytes).

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        rd = day_payload("respiration", date, "respiration data")
        return drop_none({"date": rd.get("calendarDate"), "lowest_breaths_per_min": rd.get("lowestRespirationValue"), "highest_breaths_per_min": rd.get("highestRespirationValue"), "avg_waking_breaths_per_min": rd.get("avgWakingRespirationValue"), "avg_sleep_breaths_per_min": rd.get("avgSleepRespirationValue")})

    @reg.tool(annotations=READ)
    def get_spo2_data(date: str) -> Any:
        """Get SpO2 (blood oxygen) data

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        sp = day_payload("spo2", date, "SpO2 data")
        summary = {"date": sp.get("calendarDate"), "avg_spo2_percent": sp.get("averageSpO2"), "lowest_spo2_percent": sp.get("lowestSpO2"), "latest_spo2_percent": sp.get("latestSpO2"), "latest_reading_time": sp.get("latestSpO2TimestampLocal"), "last_7_days_avg_spo2": sp.get("lastSevenDaysAvgSpO2"), "avg_sleep_spo2_percent": sp.get("avgSleepSpO2")}
        if sp.get("spO2HourlyAverages"):
            summary["hourly_averages"] = sp["spO2HourlyAverages"]
        return drop_none(summary)

    @reg.tool(annotations=READ)
    def get_all_day_stress(date: str) -> Any:
        """Get all-day stress data

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        return day_payload("stress", date, "all-day stress data")

    @reg.tool(annotations=READ)
    def get_all_day_events(date: str) -> Any:
        """Get daily wellness events data

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        return day_payload("all_day_events", date, "daily wellness events")

    @reg.tool(annotations=READ)
    def get_lifestyle_logging_data(date: str) -> Any:
        """Get lifestyle logging data for a specific date (behaviours tracked and their impact on health metrics).

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        return day_payload("lifestyle_logging", date, "lifestyle logging data")

    def weeks_window(end_date: str, weeks: int) -> tuple[date, date, int]:
        e = parse_date(end_date, "end_date")
        weeks = max(1, min(int(weeks), 52))
        first_day = (m.row("SELECT first_day_of_week FROM user_profile LIMIT 1") or {}).get("first_day_of_week") or "monday"
        ws_end = _week_start(e, first_day)
        return ws_end - timedelta(weeks=weeks - 1), e, weeks

    @reg.tool(annotations=READ)
    def get_weekly_steps(end_date: str, weeks: int = 4) -> Any:
        """Get weekly step data aggregates: weekly step totals for the specified number of weeks ending at end_date (computed from the mirror's daily steps).

        Args:
            end_date: End date in YYYY-MM-DD format
            weeks: Number of weeks to fetch (default 4, max 52)
        """
        s, e, weeks = weeks_window(end_date, weeks)
        m.range_days("steps_daily", s.isoformat(), e.isoformat())
        first_day = (m.row("SELECT first_day_of_week FROM user_profile LIMIT 1") or {}).get("first_day_of_week") or "monday"
        rows = m.rows("SELECT calendar_date, total_steps, total_distance FROM steps_daily WHERE calendar_date BETWEEN ? AND ?", [s, e])
        by_week: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_week.setdefault(_week_start(date.fromisoformat(r["calendar_date"]), first_day).isoformat(), []).append(r)
        if not by_week:
            return f"No weekly steps data found for {weeks} weeks ending {end_date}"
        out = []
        for ws, rs in sorted(by_week.items(), reverse=True):
            steps = [r["total_steps"] for r in rs if r["total_steps"] is not None]
            dist = [r["total_distance"] for r in rs if r["total_distance"] is not None]
            out.append(drop_none({"week_start": ws, "total_steps": sum(steps) if steps else None, "average_steps": round(sum(steps) / len(steps), 1) if steps else None,
                                  "total_distance_meters": sum(dist) if dist else None, "average_distance_meters": round(sum(dist) / len(dist), 1) if dist else None, "days_with_data": len(steps)}))
        note_provenance(Provenance("mirror", None, notes=["weekly totals computed from the mirror's daily steps"]))
        return {"end_date": end_date, "weeks_requested": weeks, "weeks_returned": len(out), "weekly_data": out}

    @reg.tool(annotations=READ)
    def get_weekly_stress(end_date: str, weeks: int = 4) -> Any:
        """Get weekly stress data aggregates: weekly average stress for the specified number of weeks ending at end_date (computed from the mirror's daily summaries).

        Args:
            end_date: End date in YYYY-MM-DD format
            weeks: Number of weeks to fetch (default 4, max 52)
        """
        s, e, weeks = weeks_window(end_date, weeks)
        m.range_days("daily_summary_v2", s.isoformat(), e.isoformat())
        first_day = (m.row("SELECT first_day_of_week FROM user_profile LIMIT 1") or {}).get("first_day_of_week") or "monday"
        rows = m.rows("SELECT calendar_date, stress_avg FROM daily_summary_v2 WHERE calendar_date BETWEEN ? AND ? AND stress_avg IS NOT NULL", [s, e])
        by_week: dict[str, list[int]] = {}
        for r in rows:
            by_week.setdefault(_week_start(date.fromisoformat(r["calendar_date"]), first_day).isoformat(), []).append(r["stress_avg"])
        if not by_week:
            return f"No weekly stress data found for {weeks} weeks ending {end_date}"
        out = [{"week_start": ws, "stress_value": round(sum(v) / len(v)), "days_with_data": len(v)} for ws, v in sorted(by_week.items(), reverse=True)]
        note_provenance(Provenance("mirror", None, notes=["weekly averages computed from the mirror's daily summaries"]))
        return {"end_date": end_date, "weeks_requested": weeks, "weeks_returned": len(out), "weekly_data": out}

    @reg.tool(annotations=READ)
    def get_weekly_intensity_minutes(end_date: str, weeks: int = 4) -> Any:
        """Get weekly intensity minutes aggregates (moderate and vigorous) for the specified number of weeks ending at end_date.

        total_minutes counts each vigorous minute twice (moderate + 2 x vigorous), as Garmin does, so it is the figure to compare
        against weekly_goal; moderate_minutes and vigorous_minutes are the raw minutes. Computed from the mirror's daily summaries.

        Args:
            end_date: End date in YYYY-MM-DD format
            weeks: Number of weeks to fetch (default 4, max 52)
        """
        s, e, weeks = weeks_window(end_date, weeks)
        m.range_days("daily_summary_v2", s.isoformat(), e.isoformat())
        first_day = (m.row("SELECT first_day_of_week FROM user_profile LIMIT 1") or {}).get("first_day_of_week") or "monday"
        rows = m.rows("SELECT calendar_date, intensity_minutes_moderate, intensity_minutes_vigorous, intensity_minutes_goal FROM daily_summary_v2 WHERE calendar_date BETWEEN ? AND ?", [s, e])
        by_week: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_week.setdefault(_week_start(date.fromisoformat(r["calendar_date"]), first_day).isoformat(), []).append(r)
        if not by_week:
            return f"No weekly intensity minutes data found for {weeks} weeks ending {end_date}"
        out = []
        for ws, rs in sorted(by_week.items(), reverse=True):
            mod = sum(r["intensity_minutes_moderate"] or 0 for r in rs)
            vig = sum(r["intensity_minutes_vigorous"] or 0 for r in rs)
            goal = next((r["intensity_minutes_goal"] for r in sorted(rs, key=lambda r: r["calendar_date"], reverse=True) if r["intensity_minutes_goal"]), None)
            out.append(drop_none({"week_start": ws, "weekly_goal": goal, "moderate_minutes": mod, "vigorous_minutes": vig, "total_minutes": mod + 2 * vig, "days_with_data": len(rs)}))
        note_provenance(Provenance("mirror", None, notes=["weekly totals computed from the mirror's daily summaries"]))
        return {"end_date": end_date, "weeks_requested": weeks, "weeks_returned": len(out), "weekly_data": out}

    @reg.tool(annotations=READ)
    def get_morning_training_readiness(date: str) -> Any:
        """Get morning training readiness score (the AFTER_WAKEUP_RESET snapshot; fields match one get_training_readiness entry).

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        try:
            data = m.range_day("training_readiness", date).data
        except GarminNotFound as err:
            raise NoData(f"No morning training readiness data found for {date} ({err})") from err
        items = [r for r in (data if isinstance(data, list) else [data]) if isinstance(r, dict)]
        if not items:
            raise NoData(f"No morning training readiness data found for {date}")
        entry = next((r for r in items if r.get("inputContext") == "AFTER_WAKEUP_RESET"), items[0])
        out = curate_training_readiness(entry)
        out.setdefault("date", date)
        return out

    @reg.tool(annotations=READ)
    def get_recovery_time_remaining(date: str = "") -> Any:
        """Get remaining recovery time in hours for a day (the Firstbeat recovery clock shown on-device).

        Training Readiness values are snapshots: remaining_hours and state describe the snapshot at as_of, not a live countdown.
        For today only, when snapshots are absent, the tool estimates remaining recovery by decaying the recoveryTime of recent activities.

        Args:
            date: Date in YYYY-MM-DD format. Defaults to today.
        """
        d = (date or "").strip() or m.today().isoformat()
        parse_date(d)
        snapshots: list[dict[str, Any]] = []
        try:
            data = m.range_day("training_readiness", d).data
            snapshots = [r for r in (data if isinstance(data, list) else [data]) if isinstance(r, dict)]
        except GarminNotFound:
            pass
        if snapshots:
            latest = max(snapshots, key=lambda r: str(r.get("timestampLocal") or r.get("timestamp") or ""))
            phrase = latest.get("recoveryTimeChangePhrase")
            hours = _hours_from_recovery_minutes(latest.get("recoveryTime"), phrase)
            if hours is not None:
                return drop_none({"remaining_hours": hours, "remaining_hours_at_snapshot": hours, "value_type": "snapshot", "as_of": latest.get("timestampLocal") or latest.get("timestamp"), "recovery_score": latest.get("score"),
                                  "state": _recovery_state(hours, phrase), "source": "training_readiness", "date": latest.get("calendarDate") or d, "timestamp": latest.get("timestampLocal") or latest.get("timestamp"), "change_phrase": phrase, "level": latest.get("level")})
        is_today = d == m.today().isoformat()
        if is_today:
            m.fresh_activity_list()
            now = datetime.now().astimezone()
            rows = m.rows("SELECT activity_id, activity_name, start_time_gmt, duration FROM activity WHERE deleted_at IS NULL ORDER BY start_time_gmt DESC LIMIT 20")
            # recoveryTime is not a typed column: read it from each summary by hash (read_current).
            bodies = {r["object_key"]: r["body"] for r in read_current(m.db, "activity_summary", [str(r["activity_id"]) for r in rows])}
            for r in rows:
                body = bodies.get(str(r["activity_id"]))
                value = json.loads(body).get("recoveryTime") if body else None
                r["recovery"] = float(value) if isinstance(value, (int, float)) else None
            best = None
            for r in rows:
                if r["recovery"] is None or not r["start_time_gmt"]:
                    continue
                end = datetime.fromisoformat(r["start_time_gmt"]) + timedelta(seconds=float(r["duration"] or 0))
                elapsed = max(0.0, (now - end).total_seconds() / 60)
                remaining = round(max(0.0, float(r["recovery"]) - elapsed) / 60, 1)
                best = {"remaining_hours": remaining, "state": _recovery_state(remaining, None), "source": "recent_activity", "value_type": "estimate", "as_of": now.isoformat(), "activity_id": r["activity_id"], "activity_name": r["activity_name"], "date": d}
                break
            if best:
                return best
        return {"remaining_hours": None, "recovery_score": None, "state": "unavailable", "date": d,
                "message": "No Training Readiness recovery snapshot found for this date. The activity-based recovery estimate is only available for today." if not is_today
                else "No recovery time remaining found for this date. Training Readiness snapshots were empty and recent activities did not include recoveryTime."}


def _archived(m, table: str, day: str) -> bool:  # noqa: ANN001
    row = m.db.one(f"SELECT sample_count FROM {table} WHERE calendar_date = ?::DATE", [day])
    return row is not None and (row["sample_count"] or 0) == 0

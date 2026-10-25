"""Training tools (upstream training): status, scores and trends. Trends are
computed from the mirror's typed tables after the days involved are ensured fresh."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from ..mirror import GarminNotFound, Provenance, note_provenance
from .base import READ, WRITE, NoData, Registry, ToolFailure, WriteResult, activity_key, as_dict, drop_none, parse_date, parse_range

PROGRESS_CALORIES_FACTOR = 4.19
CM_PER_MS_TO_MPS = 10
ENDURANCE_CLASSES = {1: "recreational", 2: "intermediate", 3: "trained", 4: "well_trained", 5: "expert", 6: "superior", 7: "elite"}


def _select_device(by_device: dict[str, Any]) -> tuple[dict[str, Any], str | None, bool | None]:
    """The primary training device's record from a device-keyed map (else the first)."""
    items = [(k, v) for k, v in (by_device or {}).items() if isinstance(v, dict) and v]
    if not items:
        return {}, None, None
    for k, v in items:
        if v.get("primaryTrainingDevice"):
            return v, str(v.get("deviceId") or k), True
    k, v = items[0]
    return v, str(v.get("deviceId") or k), bool(v.get("primaryTrainingDevice")) if "primaryTrainingDevice" in v else None


def _map_contributor(c: dict[str, Any], names: dict[int, str]) -> dict[str, Any]:
    out: dict[str, Any] = {"contribution_percent": c.get("contribution")}
    if c.get("activityTypeId") is not None:
        out["activity_type"] = names.get(c["activityTypeId"], f"type_{c['activityTypeId']}")
    if c.get("group") is not None:
        out["group"] = c["group"]
    return drop_none(out)


def register(reg: Registry) -> None:
    m = reg.mirror

    def device_names() -> dict[str, str]:
        return {str(r["device_id"]): r["display_name"] or r["product_display_name"] for r in m.rows("SELECT device_id, display_name, product_display_name FROM device")}

    def activity_type_names() -> dict[int, str]:
        try:
            return {t["typeId"]: t["typeKey"] for t in m.optional("activity_types", default=[]) or [] if "typeId" in t}
        except Exception:  # noqa: BLE001
            return {}

    @reg.tool(annotations=READ)
    def get_progress_summary_between_dates(start_date: str, end_date: str, metric: str) -> Any:
        """Get progress summary for a metric between dates, by activity type.

        Note: "calories" values are returned in kcal (Garmin's endpoint stores this metric pre-scaled by ~4.19; this tool undoes that).

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
            metric: Metric to get progress for (e.g., "elevationGain", "duration", "distance", "movingDuration", "calories")
        """
        parse_range(start_date, end_date)
        data = m.report("progress_summary", "/fitnessstats-service/activity", {"startDate": start_date, "endDate": end_date, "aggregation": "lifetime", "groupByParentActivityType": "false", "metric": metric}, f"{start_date}_{end_date}_{metric}").data
        item = data[0] if isinstance(data, list) and data else data
        if not isinstance(item, dict):
            raise NoData(f"No progress summary found for {metric} between {start_date} and {end_date}.")

        def conv(v: Any) -> Any:
            return v / PROGRESS_CALORIES_FACTOR if (v is not None and metric == "calories") else v

        stats_by_type: dict[str, Any] = {}
        for t, st in (item.get("stats") or {}).items():
            md = (st or {}).get(metric)
            if md and md.get("count", 0) > 0:
                stats_by_type[t] = {"count": md.get("count"), "sum": conv(md.get("sum")), "avg": conv(md.get("avg")), "min": conv(md.get("min")), "max": conv(md.get("max"))}
        return {"metric": metric, "start_date": start_date, "end_date": end_date, "stats_by_activity_type": stats_by_type, "count_of_activities": sum(v["count"] for v in stats_by_type.values())}

    @reg.tool(annotations=READ)
    def get_hill_score(start_date: str, end_date: str) -> Any:
        """Get hill score data between dates

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
        """
        parse_range(start_date, end_date, 366)
        days = [d for d in m.range_days("hill_score", start_date, end_date).data if isinstance(d, dict)]
        if not days:
            return f"No hill score data found between {start_date} and {end_date}."
        latest = max(days, key=lambda s: str(s.get("calendarDate") or ""))
        scores = [s["overallScore"] for s in days if s.get("overallScore") is not None]
        return drop_none({
            "start_date": start_date, "end_date": end_date, "period_avg_score": round(sum(scores) / len(scores), 1) if scores else None, "max_score": max(scores) if scores else None,
            "latest_date": latest.get("calendarDate"), "latest_overall_score": latest.get("overallScore"), "latest_strength_score": latest.get("strengthScore"), "latest_endurance_score": latest.get("enduranceScore"),
            "latest_classification_id": latest.get("hillScoreClassificationId"),
            "daily_scores": [drop_none({"date": s.get("calendarDate"), "overall": s.get("overallScore"), "strength": s.get("strengthScore"), "endurance": s.get("enduranceScore")}) for s in sorted(days, key=lambda s: str(s.get("calendarDate")))],
        })

    @reg.tool(annotations=READ)
    def get_endurance_score(start_date: str, end_date: str) -> Any:
        """Get endurance score data between dates

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
        """
        parse_range(start_date, end_date, 366)
        days = m.range_days("endurance_score", start_date, end_date).data
        if not days:
            return f"No endurance score data found between {start_date} and {end_date}."
        names = activity_type_names()
        weekly = []
        rows = m.rows("SELECT calendar_date, group_average, group_max, contributors FROM endurance_score WHERE calendar_date BETWEEN ?::DATE AND ?::DATE ORDER BY calendar_date", [start_date, end_date])
        import json

        for r in rows:
            contribs = json.loads(r["contributors"]) if isinstance(r["contributors"], str) else (r["contributors"] or [])
            weekly.append(drop_none({"week_start": r["calendar_date"], "avg_score": r["group_average"], "max_score": r["group_max"], "contributors": [_map_contributor(c, names) for c in contribs] or None}))
        avgs = [w["avg_score"] for w in weekly if w.get("avg_score") is not None]
        latest = weekly[-1] if weekly else {}
        return drop_none({"start_date": start_date, "end_date": end_date, "period_avg_score": round(sum(avgs) / len(avgs)) if avgs else None, "period_max_score": max(w["max_score"] for w in weekly if w.get("max_score") is not None) if any(w.get("max_score") is not None for w in weekly) else None,
                          "current_score": latest.get("avg_score"), "current_date": latest.get("week_start"), "weekly_breakdown": weekly or None})

    @reg.tool(annotations=READ)
    def get_training_effect(activity_id: int) -> Any:
        """Get training effect data for a specific activity.

        Args:
            activity_id: ID of the activity to retrieve training effect for
        """
        key = activity_key(activity_id)
        a = m.activity("activity", key).data
        s = a.get("summaryDTO") or {}
        return {"activity_id": int(key), "source": "activity_details", "training_effect": drop_none({
            "aerobic_training_effect": s.get("trainingEffect"), "anaerobic_training_effect": s.get("anaerobicTrainingEffect"), "training_effect_label": s.get("trainingEffectLabel"),
            "aerobic_training_effect_message": s.get("aerobicTrainingEffectMessage"), "anaerobic_training_effect_message": s.get("anaerobicTrainingEffectMessage"), "training_load": s.get("activityTrainingLoad"),
        })}

    @reg.tool(annotations=READ)
    def get_hrv_data(date: str, return_timeseries: bool = False) -> Any:
        """Get Heart Rate Variability (HRV) data

        Args:
            date: Date in YYYY-MM-DD format
            return_timeseries: If True, include detailed 5-minute HRV readings (can be large)
        """
        parse_date(date)
        try:
            hrv = m.day("hrv", date).data
        except GarminNotFound as err:
            raise NoData(f"No HRV data found for {date}. ({err})") from err
        summary = hrv.get("hrvSummary") or {}
        baseline = summary.get("baseline") or {}
        out = {
            "date": summary.get("calendarDate") or date, "last_night_avg_hrv_ms": summary.get("lastNightAvg"), "last_night_5min_high_hrv_ms": summary.get("lastNight5MinHigh"), "weekly_avg_hrv_ms": summary.get("weeklyAvg"),
            "baseline_balanced_low_ms": baseline.get("balancedLow"), "baseline_balanced_upper_ms": baseline.get("balancedUpper"), "baseline_low_upper_ms": baseline.get("lowUpper"),
            "status": summary.get("status"), "feedback": summary.get("feedbackPhrase"), "sleep_start": hrv.get("sleepStartTimestampLocal"), "sleep_end": hrv.get("sleepEndTimestampLocal"),
        }
        if return_timeseries:
            readings = [r for r in hrv.get("hrvReadings") or [] if isinstance(r, dict)]
            out["hrv_readings"] = [{"time": r.get("readingTimeLocal"), "hrv_ms": r.get("hrvValue")} for r in readings]
            out["readings_count"] = len(readings)
        return drop_none(out)

    @reg.tool(annotations=READ)
    def get_fitnessage_data(date: str, details: bool = False) -> Any:
        """Get fitness age data

        Args:
            date: Date in YYYY-MM-DD format
            details: If True, include component breakdown (BMI, RHR, vigorous activity) with targets and improvement suggestions
        """
        parse_date(date)
        fa = m.report("fitness_age_detail", f"/fitnessage-service/fitnessage/{date}", None, date).data
        if not isinstance(fa, dict):
            raise NoData(f"No fitness age data found for {date}.")
        chrono, fit = fa.get("chronologicalAge"), fa.get("fitnessAge")
        out = {"date": date, "fitness_age_years": round(fit, 1) if fit else None, "chronological_age_years": chrono, "age_difference_years": round(chrono - fit, 1) if chrono is not None and fit is not None else None,
               "achievable_fitness_age_years": round(fa["achievableFitnessAge"], 1) if fa.get("achievableFitnessAge") else None, "previous_fitness_age_years": round(fa["previousFitnessAge"], 1) if fa.get("previousFitnessAge") else None, "last_updated": fa.get("lastUpdated")}
        if details:
            comps = {}
            for name, c in (fa.get("components") or {}).items():
                if not isinstance(c, dict):
                    continue
                info: dict[str, Any] = {"value": c.get("value")}
                for src, dst in (("targetValue", "target"), ("improvementValue", "improvement_needed"), ("priority", "priority"), ("stale", "stale"), ("lastMeasurementDate", "last_measurement")):
                    if c.get(src) is not None:
                        info[dst] = c[src]
                if c.get("potentialAge") is not None:
                    info["potential_age_if_improved"] = round(c["potentialAge"], 1)
                comps[name] = info
            if comps:
                out["components"] = comps
        return drop_none(out)

    def status_for(day: str) -> dict[str, Any] | None:
        try:
            return m.day("training_status", day).data
        except GarminNotFound:
            return None

    @reg.tool(annotations=READ)
    def get_training_status(date: str) -> Any:
        """Get training status with curated metrics: status, VO2 max, acute/chronic load (ACWR) and Load Focus balance.

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        status = status_for(date)
        if not status:
            raise NoData(f"No training status data found for {date}.")
        names = device_names()
        latest = as_dict(as_dict(status.get("mostRecentTrainingStatus")).get("latestTrainingStatusData"))
        dev, dev_id, primary = _select_device(latest)
        acwr = as_dict(dev.get("acuteTrainingLoadDTO"))
        vo2 = as_dict(status.get("mostRecentVO2Max"))
        g, c = as_dict(vo2.get("generic")), as_dict(vo2.get("cycling"))
        load_map = as_dict(as_dict(status.get("mostRecentTrainingLoadBalance")).get("metricsTrainingLoadBalanceDTOMap"))
        load, load_id, _ = _select_device(load_map)
        out = {
            "date": dev.get("calendarDate", date), "reporting_device_id": int(dev_id) if dev_id and dev_id.isdigit() else dev_id, "reporting_device_name": names.get(dev_id or ""), "load_device_id": int(load_id) if load_id and load_id.isdigit() else load_id,
            "training_status": dev.get("trainingStatus"), "training_status_feedback": dev.get("trainingStatusFeedbackPhrase"), "sport": dev.get("sport"), "fitness_trend": dev.get("fitnessTrend"),
            "vo2_max": g.get("vo2MaxValue"), "vo2_max_precise": g.get("vo2MaxPreciseValue"), "cycling_vo2_max": c.get("vo2MaxValue"), "cycling_vo2_max_precise": c.get("vo2MaxPreciseValue"),
            "monthly_load_aerobic_low": load.get("monthlyLoadAerobicLow"), "monthly_load_aerobic_high": load.get("monthlyLoadAerobicHigh"), "monthly_load_anaerobic": load.get("monthlyLoadAnaerobic"), "training_balance_feedback": load.get("trainingBalanceFeedbackPhrase"),
            "acute_load": acwr.get("dailyTrainingLoadAcute"), "chronic_load": acwr.get("dailyTrainingLoadChronic"), "load_ratio": acwr.get("dailyAcuteChronicWorkloadRatio"), "acwr_status": acwr.get("acwrStatus"), "acwr_percent": acwr.get("acwrPercent"),
            "optimal_chronic_load_min": acwr.get("minTrainingLoadChronic"), "optimal_chronic_load_max": acwr.get("maxTrainingLoadChronic"),
        }
        if primary is False:
            out["acute_load_source"] = "non_primary_device"
        return drop_none(out)

    @reg.tool(annotations=READ)
    def get_cycling_ftp() -> Any:
        """Get the latest cycling Functional Threshold Power (FTP) data."""
        ftp = m.optional("cycling_ftp")
        return ftp if ftp else "No cycling FTP data found."

    @reg.tool(annotations=READ)
    def get_lactate_threshold(start_date: str | None = None, end_date: str | None = None) -> Any:
        """Get lactate threshold data (the exercise intensity at which lactate starts to accumulate): latest values, or history with a date range.

        Args:
            start_date: Start date in YYYY-MM-DD format (optional; give with end_date for history, omit both for latest)
            end_date: End date in YYYY-MM-DD format (optional; give with start_date for history, omit both for latest)
        """
        if bool(start_date) != bool(end_date):
            raise ToolFailure("Provide both start_date and end_date for lactate threshold history, or omit both for the latest values.")
        if start_date and end_date:
            parse_range(start_date, end_date)
            out: dict[str, Any] = {"start_date": start_date, "end_date": end_date}
            for metric, key, label, conv in (("speed", "speed_history", "speed_mps", lambda v: v * CM_PER_MS_TO_MPS if v is not None else None), ("heartRate", "heart_rate_history", "heart_rate_bpm", lambda v: v), ("power", "power_history", "power_watts", lambda v: v)):
                try:
                    hist = m.report(f"lactate_threshold_{metric}", f"/biometric-service/stats/lactateThreshold/range/{metric}/{start_date}/{end_date}", {"aggregation": "daily"}, f"{start_date}_{end_date}").data
                except GarminNotFound:
                    hist = None
                if hist:
                    out[key] = [drop_none({"date": e.get("from"), label: conv(e.get("value")), "series": e.get("series")}) for e in hist if isinstance(e, dict)]
            if len(out) == 2:
                return f"No lactate threshold history found between {start_date} and {end_date}."
            return out
        try:
            shr = m.report("lactate_threshold_latest", "/biometric-service/biometric/latestLactateThreshold", None, "self").data
        except GarminNotFound:
            shr = None
        try:
            power = m.report("power_to_weight_latest", f"/biometric-service/biometric/powerToWeight/latest/{m.today().isoformat()}", {"sport": "Running"}, "self").data
        except GarminNotFound:
            power = None
        if isinstance(shr, list):
            shr = next((x for x in shr if isinstance(x, dict) and (x.get("speed") is not None or x.get("heartRate") is not None)), shr[0] if shr else {})
        if isinstance(power, list):
            power = power[0] if power else {}
        shr, power = as_dict(shr), as_dict(power)
        if not shr and not power:
            return "No lactate threshold data found."
        return drop_none({"lactate_threshold_speed_mps": shr.get("speed") * CM_PER_MS_TO_MPS if shr.get("speed") is not None else None, "lactate_threshold_heart_rate_bpm": shr.get("heartRate"), "heart_rate_cycling_bpm": shr.get("heartRateCycling"), "speed_hr_date": shr.get("calendarDate"),
                          "functional_threshold_power_watts": power.get("functionalThresholdPower"), "weight_kg": power.get("weight"), "power_to_weight": power.get("powerToWeight"), "sport": power.get("sport"), "power_date": power.get("calendarDate"), "is_stale": power.get("isStale")})

    @reg.tool(annotations=WRITE)
    def request_reload(date: str) -> Any:
        """Request reload of epoch data (asks Garmin to restore the archived intraday data of a day; about 30 per day are allowed, the sync engine uses 25 of them).

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        m.engine.run_job("reload", date, 5)
        row = m.db.one("SELECT status, response FROM reload_request WHERE calendar_date = ?::DATE ORDER BY requested_at DESC LIMIT 1", [date])
        note_provenance(Provenance("garmin", None, notes=["reload request sent to Garmin; the day's intraday datasets will be re-fetched in about three hours"]))
        import json

        return WriteResult({"status": row["status"] if row else None, "response": json.loads(row["response"]) if row and isinstance(row["response"], str) else (row or {}).get("response")})

    @reg.tool(annotations=READ)
    def get_training_load_trend(start_date: str, end_date: str) -> Any:
        """Get Garmin's daily acute and chronic training load over a date range.

        Returns, per day, acute load (atl, the 7-day load), chronic load (ctl), tsb = ctl - atl, and Garmin's Acute:Chronic Workload
        Ratio (acwr, acwr_status). These are Garmin's dailyTrainingLoadAcute/Chronic values on Garmin's weekly load scale, not
        TrainingPeaks CTL/ATL. Recommended range: 4-8 weeks. Maximum: 90 days. Empty days are skipped.

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
        """
        s, e = parse_range(start_date, end_date, 90)
        payloads = m.days("training_status", start_date, end_date).data
        names = device_names()
        trend = []
        for d in sorted(payloads):
            data = payloads[d]
            latest = as_dict(as_dict(data.get("mostRecentTrainingStatus")).get("latestTrainingStatusData"))
            dev, dev_id, primary = _select_device(latest)
            acwr = as_dict(dev.get("acuteTrainingLoadDTO"))
            g = as_dict(as_dict(data.get("mostRecentVO2Max")).get("generic"))
            entry: dict[str, Any] = {"date": d}
            if dev_id:
                entry["reporting_device_id"] = int(dev_id) if dev_id.isdigit() else dev_id
                if names.get(dev_id):
                    entry["reporting_device_name"] = names[dev_id]
            atl, ctl, ratio = acwr.get("dailyTrainingLoadAcute"), acwr.get("dailyTrainingLoadChronic"), acwr.get("dailyAcuteChronicWorkloadRatio")
            if atl is not None:
                entry["atl"] = round(atl, 1)
            if ctl is not None:
                entry["ctl"] = round(ctl, 1)
            if atl is not None and ctl is not None:
                entry["tsb"] = round(ctl - atl, 1)
            if ratio is not None:
                entry["acwr"] = round(ratio, 2)
            for src, dst in (("acwrStatus", "acwr_status"), ("acwrPercent", "acwr_percent"), ("minTrainingLoadChronic", "optimal_chronic_load_min"), ("maxTrainingLoadChronic", "optimal_chronic_load_max")):
                if acwr.get(src) is not None:
                    entry[dst] = acwr[src]
            if dev.get("trainingStatusFeedbackPhrase"):
                entry["training_status"] = dev["trainingStatusFeedbackPhrase"]
            if g.get("vo2MaxValue") is not None:
                entry["vo2_max"] = g["vo2MaxValue"]
            if len(entry) > 1:
                trend.append(entry)
        if not trend:
            return f"No training load data found between {start_date} and {end_date}."
        return {"start_date": start_date, "end_date": end_date, "days_with_data": len(trend), "trend": trend}

    @reg.tool(annotations=READ)
    def get_training_load_balance(date: str) -> Any:
        """Get Garmin's Load Focus: the distribution of the trailing-month training load across Aerobic Low, Aerobic High and
        Anaerobic bands with Garmin's target ranges, a status of below/within/above per band, and the feedback phrase
        (e.g. AEROBIC_HIGH_SHORTAGE, BALANCED). load_device_id/load_device_name say which device the numbers came from.

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        status = status_for(date)
        if not status:
            raise NoData(f"No training status data found for {date}.")
        load_map = as_dict(as_dict(status.get("mostRecentTrainingLoadBalance")).get("metricsTrainingLoadBalanceDTOMap"))
        load, load_id, _ = _select_device(load_map)
        if not load:
            raise NoData(f"No training load balance data found for {date}.")
        names = device_names()

        def band(key: str) -> dict[str, Any]:
            v, lo, hi = load.get(f"monthlyLoad{key}"), load.get(f"monthlyLoad{key}TargetMin"), load.get(f"monthlyLoad{key}TargetMax")
            st = None
            if v is not None and lo is not None and hi is not None:
                st = "below" if v < lo else "above" if v > hi else "within"
            return drop_none({"load": round(v, 1) if v is not None else None, "target_min": lo, "target_max": hi, "status": st})

        return drop_none({"date": load.get("calendarDate", date), "load_device_id": int(load_id) if load_id and load_id.isdigit() else load_id, "load_device_name": names.get(load_id or ""),
                          "aerobic_low": band("AerobicLow"), "aerobic_high": band("AerobicHigh"), "anaerobic": band("Anaerobic"), "feedback": load.get("trainingBalanceFeedbackPhrase")})

    @reg.tool(annotations=READ)
    def get_hrv_trend(start_date: str, end_date: str) -> Any:
        """Get HRV (Heart Rate Variability) trend over a date range: daily values and the period average. A drop of >10ms from the
        7-day baseline warrants reducing training load. Recommended range: 7-21 days. Maximum: 30 days.

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
        """
        parse_range(start_date, end_date, 30)
        m.range_days("hrv_summary", start_date, end_date)
        rows = m.rows("SELECT calendar_date, last_night_avg, weekly_avg, last_night_5min_high, status, feedback_phrase FROM hrv WHERE calendar_date BETWEEN ?::DATE AND ?::DATE ORDER BY calendar_date", [start_date, end_date])
        trend = []
        for r in rows:
            entry = drop_none({"date": r["calendar_date"], "last_night_avg_hrv_ms": r["last_night_avg"], "weekly_avg_hrv_ms": r["weekly_avg"], "last_night_5min_high_hrv_ms": r["last_night_5min_high"], "status": r["status"], "feedback": r["feedback_phrase"]})
            if len(entry) > 1:
                trend.append(entry)
        if not trend:
            return f"No HRV data found between {start_date} and {end_date}."
        vals = [e["last_night_avg_hrv_ms"] for e in trend if e.get("last_night_avg_hrv_ms") is not None]
        return {"start_date": start_date, "end_date": end_date, "days_with_data": len(trend), "period_avg_hrv_ms": round(sum(vals) / len(vals), 1) if vals else None, "trend": trend}

    @reg.tool(annotations=READ)
    def get_vo2max_trend(start_date: str, end_date: str) -> Any:
        """Get VO2 max trend over a date range (Garmin's FirstBeat estimates). Days between recomputes carry the last known value
        forward and are marked carried_forward: true, matching Garmin Connect's chart. Daily changes of <0.5 are noise; focus on the
        4-6 week direction. Recommended range: 4-12 weeks. Maximum: 90 days.

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
        """
        s, e = parse_range(start_date, end_date, 90)
        lookback = (s - timedelta(days=60)).isoformat()
        m.range_days("max_metrics", lookback, end_date)
        rows = m.rows("SELECT calendar_date, vo2max_precise, vo2max, vo2max_cycling_precise, vo2max_cycling FROM max_metrics WHERE calendar_date BETWEEN ?::DATE AND ?::DATE ORDER BY calendar_date", [lookback, end_date])
        hist: dict[str, list[tuple[str, float]]] = {"running": [], "cycling": []}
        for r in rows:
            if r["vo2max_precise"] is not None or r["vo2max"] is not None:
                hist["running"].append((r["calendar_date"], float(r["vo2max_precise"] if r["vo2max_precise"] is not None else r["vo2max"])))
            if r["vo2max_cycling_precise"] is not None or r["vo2max_cycling"] is not None:
                hist["cycling"].append((r["calendar_date"], float(r["vo2max_cycling_precise"] if r["vo2max_cycling_precise"] is not None else r["vo2max_cycling"])))
        sport = max(hist, key=lambda k: (len(hist[k]), k == "running"))
        series = hist[sport]
        trend = []
        if series:
            last: tuple[str, float] | None = None
            idx = 0
            d = s
            while d <= e:
                ds = d.isoformat()
                while idx < len(series) and series[idx][0] <= ds:
                    last = series[idx]
                    idx += 1
                if last is not None:
                    trend.append({"date": ds, "vo2_max": round(last[1], 1), "measurement_date": last[0], "carried_forward": last[0] != ds})
                d += timedelta(days=1)
        if not trend:
            profile = m.snapshot("user_settings").data.get("userData") or {}
            cur = profile.get("vo2MaxRunning") or profile.get("vo2MaxCycling")
            if cur is None:
                return f"No VO2 max data found between {start_date} and {end_date}."
            return {"start_date": start_date, "end_date": end_date, "sport": "running" if profile.get("vo2MaxRunning") else "cycling", "data_points": 0, "trend": [], "current_estimate": cur, "note": "No dated VO2 max measurements in range; current profile estimate returned instead."}
        first, latest = trend[0]["vo2_max"], trend[-1]["vo2_max"]
        return {"start_date": start_date, "end_date": end_date, "sport": sport, "data_points": len(trend), "first_vo2_max": first, "latest_vo2_max": latest, "change": round(latest - first, 1), "trend": trend}

    @reg.tool(annotations=READ)
    def get_respiration_trend(start_date: str, end_date: str) -> Any:
        """Get overnight respiration rate trend over a date range. Elevated resting respiration vs the personal baseline is an early
        sign of overreaching, illness or poor recovery. Recommended range: 7-21 days. Maximum: 30 days.

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
        """
        parse_range(start_date, end_date, 30)
        m.days("respiration", start_date, end_date)
        rows = m.rows("SELECT calendar_date, avg_sleep_respiration_value, avg_waking_respiration_value, lowest_respiration_value, highest_respiration_value FROM respiration_day WHERE calendar_date BETWEEN ?::DATE AND ?::DATE ORDER BY calendar_date", [start_date, end_date])
        trend = []
        for r in rows:
            entry = drop_none({"date": r["calendar_date"], "avg_sleep_breaths_per_min": r["avg_sleep_respiration_value"], "avg_waking_breaths_per_min": r["avg_waking_respiration_value"], "lowest_breaths_per_min": r["lowest_respiration_value"], "highest_breaths_per_min": r["highest_respiration_value"]})
            if len(entry) > 1:
                trend.append(entry)
        if not trend:
            return f"No respiration data found between {start_date} and {end_date}."
        vals = [t["avg_sleep_breaths_per_min"] for t in trend if t.get("avg_sleep_breaths_per_min") is not None]
        return {"start_date": start_date, "end_date": end_date, "days_with_data": len(trend), "period_avg_sleep_breaths_per_min": round(sum(vals) / len(vals), 1) if vals else None, "trend": trend}

    def tolerance_point(entry: dict[str, Any], daily: bool) -> dict[str, Any]:
        tol = entry.get("acuteTolerance") if daily else entry.get("tolerance")
        load = entry.get("acuteImpactLoad") if daily else entry.get("totalImpactLoad")
        dist = entry.get("acuteDistance") if daily else entry.get("totalDistance")
        p: dict[str, Any] = {"date": entry.get("calendarDate")}
        if tol is not None:
            p["tolerance_km"] = round(tol / 1000, 2)
        if load is not None:
            p["acute_load_km"] = round(load / 1000, 2)
        if dist is not None:
            p["distance_km"] = round(dist / 1000, 2)
        if load is not None and dist:
            p["load_ratio"] = round(load / dist, 2)
        if daily and entry.get("runningToleranceFeedBackPhrase"):
            p["feedback_phrase"] = entry["runningToleranceFeedBackPhrase"]
        if not daily:
            for k, dst in (("startOfWeek", "start_of_week"), ("endOfWeek", "end_of_week"), ("weekIndex", "week_index")):
                if entry.get(k) is not None:
                    p[dst] = entry[k]
        return p

    @reg.tool(annotations=READ)
    def get_running_tolerance(date: str) -> Any:
        """Get Running Tolerance for a single day: tolerance_km (load capacity), acute_load_km (intensity-adjusted load), distance_km,
        and load_ratio (acute_load_km / distance_km).

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        try:
            data = m.day("running_tolerance", date).data
        except GarminNotFound as err:
            raise NoData(f"No running tolerance data found for {date}. ({err})") from err
        entry = data[0] if isinstance(data, list) and data else data
        if not isinstance(entry, dict):
            raise NoData(f"No running tolerance data found for {date}.")
        out = tolerance_point(entry, True)
        out["date"] = out.get("date") or date
        return out

    @reg.tool(annotations=READ)
    def get_running_tolerance_trend(start_date: str, end_date: str, aggregation: Literal["daily", "weekly"] = "weekly") -> Any:
        """Get Running Tolerance trend over a date range, per period: tolerance_km, acute_load_km, distance_km and load_ratio.
        Weekly aggregation (default) gives a compact multi-month view. Maximum: 90 days for daily, 366 for weekly.

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
            aggregation: "daily" or "weekly" (default "weekly")
        """
        parse_range(start_date, end_date, 90 if aggregation == "daily" else 366)
        data = m.report("running_tolerance_stats", "/metrics-service/metrics/runningtolerance/stats", {"startDate": start_date, "endDate": end_date, "aggregation": aggregation}, f"{start_date}_{end_date}_{aggregation}").data
        entries = [e for e in (data if isinstance(data, list) else []) if isinstance(e, dict)]
        if not entries:
            return f"No running tolerance data found between {start_date} and {end_date}."
        trend = [tolerance_point(e, aggregation == "daily") for e in entries]
        return {"start_date": start_date, "end_date": end_date, "aggregation": aggregation, "points": len(trend), "trend": trend}

    @reg.tool(annotations=READ)
    def get_acclimation(date: str) -> Any:
        """Get heat and altitude acclimation status for a given date (heat_acclimation_percent 0-100, altitude acclimation in meters,
        Garmin's trend labels and the previous readings).

        Args:
            date: Date in YYYY-MM-DD format
        """
        parse_date(date)
        try:
            data = m.range_day("max_metrics", date).data
        except GarminNotFound as err:
            raise NoData(f"No acclimation data found for {date}. ({err})") from err
        acc = as_dict((data if isinstance(data, dict) else {}).get("heatAltitudeAcclimation"))
        if not acc:
            raise NoData(f"No acclimation data found for {date}. Garmin populates this only for days with a heat/altitude estimate.")
        out: dict[str, Any] = {"date": acc.get("calendarDate", date)}
        for dst, src in (("heat_acclimation_percent", "heatAcclimationPercentage"), ("previous_heat_acclimation_percent", "previousHeatAcclimationPercentage"), ("heat_trend", "heatTrend"), ("heat_acclimation_date", "heatAcclimationDate"),
                         ("previous_heat_acclimation_date", "previousHeatAcclimationDate"), ("altitude_acclimation_meters", "altitudeAcclimation"), ("previous_altitude_acclimation_meters", "previousAltitudeAcclimation"), ("altitude_trend", "altitudeTrend"), ("current_altitude_meters", "currentAltitude")):
            if acc.get(src) is not None:
                out[dst] = acc[src]
        heat, prev = out.get("heat_acclimation_percent"), out.get("previous_heat_acclimation_percent")
        if isinstance(heat, (int, float)) and isinstance(prev, (int, float)):
            out["heat_acclimation_change"] = round(heat - prev, 1)
        return out

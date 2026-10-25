"""Badge, challenge, goal and personal-record mappings and formatters.

Vendored from Taxuspt/garmin_mcp (MIT), src/garmin_mcp/challenges.py.
"""

import datetime
from typing import Any, Dict, List

# Badge category mappings
# This list is estimated based on real data, the mapping might not be 100% accurate
BADGE_CATEGORY_MAPPING = {
    1: "Activity",
    2: "Running",
    3: "Cycling",
    4: "Challenge",
    5: "Steps",
    9: "Diving",
}

# Badge difficulty mappings
BADGE_DIFFICULTY_MAPPING = {
    1: "Easy",
    2: "Medium",
    3: "Hard",
}

# Badge unit mappings: unitId -> (name, value_type)
# value_type used to format the progress/target values
BADGE_UNIT_MAPPING = {
    1: ("distance", "distance"),  # meters
    2: ("elevation", "elevation"),  # meters
    3: ("activities", "count"),
    5: ("steps", "count"),
    7: ("time", "time"),  # seconds
}

# Challenge category mappings
CHALLENGE_CATEGORY_MAPPING = {
    1: "Running",
    2: "Cycling",
    3: "Fitness",
    4: "Steps",
    5: "Walking",
    6: "Yoga/Mindfulness",
    9: "Multi-Activity",
}

# Challenge status mappings
CHALLENGE_STATUS_MAPPING = {
    1: "Not Started",
    2: "In Progress",
    3: "Completed",
    4: "Ended",
}

# Adhoc challenge activity type mappings
ADHOC_ACTIVITY_TYPE_MAPPING = {
    1: "Running",
    2: "Cycling",
    3: "Swimming",
    4: "Steps",
    5: "Walking",
}

# Personal record type mappings: typeId -> (name, value_type)
# value_type: "time" (seconds), "distance" (meters), "count", "elevation" (meters), "days"
# This list is estimated based on real data, the type mapping might not be 100% accurate
PR_TYPE_MAPPING = {
    1: ("Fastest 1K", "time"),
    2: ("Fastest Mile", "time"),
    3: ("Fastest 5K", "time"),
    4: ("Fastest 10K", "time"),
    5: ("Fastest Half Marathon", "time"),
    6: ("Fastest Marathon", "time"),
    7: ("Longest Run", "distance"),
    8: ("Longest Ride", "distance"),
    9: ("Most Elevation Gain Cycling", "elevation"),
    10: ("Fastest 100K Cycling", "time"),
    11: ("Fastest 40K Cycling", "time"),
    12: ("Most Steps Day", "count"),
    13: ("Most Steps Week", "count"),
    14: ("Most Steps Month", "count"),
    15: ("Longest Daily Goal Streak", "days"),
    16: ("Longest Weekly Goal Streak", "days"),
    17: ("Longest Pool Swim", "distance"),
    18: ("Fastest 100m Pool Swim", "time"),
    19: ("Fastest 400m Pool Swim", "time"),
    20: ("Fastest 500m Pool Swim", "time"),
    21: ("Fastest 800m Pool Swim", "time"),
    22: ("Fastest 1500m Pool Swim", "time"),
    23: ("Fastest 1 Mile Pool Swim", "time"),
}


def _format_time(seconds: float) -> str:
    """Format seconds into human-readable time string"""
    if seconds is None:
        return None
    total_seconds = int(seconds)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60

    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _format_distance(meters: float) -> str:
    """Format meters into human-readable distance string"""
    if meters is None:
        return None
    if meters >= 1000:
        return f"{meters / 1000:.2f} km"
    return f"{meters:.0f} m"


def _format_timestamp(timestamp_ms: int) -> str:
    """Convert a GMT millisecond timestamp to its UTC date string"""
    if timestamp_ms is None:
        return None
    dt = datetime.datetime.fromtimestamp(
        timestamp_ms / 1000.0, tz=datetime.timezone.utc
    )
    return dt.strftime("%Y-%m-%d")


def _parse_iso_date(iso_string: str) -> str:
    """Extract just the date part from an ISO datetime string"""
    if not iso_string:
        return None
    # Handle formats like "2024-03-31T18:55:47.534" or "2024-03-31T18:55:47.0"
    return iso_string.split("T")[0] if "T" in iso_string else iso_string


def _first_non_none(data: dict, *keys: str) -> Any:
    """Return the first non-None value for the supplied keys."""
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _format_badge_value(value: float, unit_id: int) -> str:
    """Format a badge progress/target value based on its unit type"""
    if value is None:
        return None

    unit_info = BADGE_UNIT_MAPPING.get(unit_id)
    if not unit_info:
        return str(value)

    _, value_type = unit_info

    if value_type == "time":
        return _format_time(value)
    elif value_type == "distance":
        return _format_distance(value)
    elif value_type == "elevation":
        return f"{value:.0f} m"
    elif value_type == "count":
        return f"{int(value):,}"
    return str(value)


def _calculate_progress_percent(progress: float, target: float) -> str:
    """Calculate progress percentage"""
    if progress is None or target is None or target == 0:
        return None
    percent = (progress / target) * 100
    return f"{min(percent, 100):.1f}%"


def _curate_badge_challenge(challenge: dict) -> dict:
    """Curate a badge challenge into a clean format"""
    category_id = challenge.get("challengeCategoryId")
    status_id = challenge.get("badgeChallengeStatusId")
    unit_id = challenge.get("badgeUnitId")

    progress = challenge.get("badgeProgressValue")
    target = challenge.get("badgeTargetValue")

    curated = {
        "name": challenge.get("badgeChallengeName"),
        "uuid": challenge.get("uuid"),
        "category": CHALLENGE_CATEGORY_MAPPING.get(category_id, f"category_{category_id}"),
        "status": CHALLENGE_STATUS_MAPPING.get(status_id, f"status_{status_id}"),
        "points": challenge.get("badgePoints"),
        "start_date": _parse_iso_date(challenge.get("startDate")),
        "end_date": _parse_iso_date(challenge.get("endDate")),
        "joined": challenge.get("userJoined", False),
    }

    # Add progress info if available
    if target is not None and target > 0:
        curated["progress"] = _format_badge_value(progress, unit_id)
        curated["target"] = _format_badge_value(target, unit_id)
        curated["progress_percent"] = _calculate_progress_percent(progress, target)

    # Add earned date if completed
    earned_date = challenge.get("badgeEarnedDate")
    if earned_date:
        curated["earned_date"] = _parse_iso_date(earned_date)

    return curated


def _format_pr_value(value: float, value_type: str) -> str:
    """Format a PR value based on its type"""
    if value is None:
        return None
    if value_type == "time":
        return _format_time(value)
    elif value_type == "distance":
        return _format_distance(value)
    elif value_type == "elevation":
        return f"{value:.0f} m"
    elif value_type == "count":
        return f"{int(value):,}"
    elif value_type == "days":
        return f"{int(value)} days"
    return str(value)


def _as_goal_list(payload: Any) -> List[Dict[str, Any]]:
    """Normalize Connect goal payloads to a list of dicts."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("goals", "items", "data"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        if any(key in payload for key in ("name", "goalType", "id", "type")):
            return [payload]
    return []


def _is_connect_ui_goal(goal: Dict[str, Any]) -> bool:
    """True for the current Connect Goals UI shape (distance/time accumulation)."""
    progress = goal.get("progress")
    return "name" in goal and (
        isinstance(progress, dict)
        or "distanceInMeters" in goal
        or ("type" in goal and "goalType" not in goal)
    )


def _curate_connect_ui_goal(goal: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten Connect UI goal progress/remaining into LLM-friendly fields."""
    progress = goal.get("progress") if isinstance(goal.get("progress"), dict) else {}
    remaining = goal.get("remaining") if isinstance(goal.get("remaining"), dict) else {}
    overage = goal.get("overage") if isinstance(goal.get("overage"), dict) else {}
    curated = {
        "id": goal.get("id"),
        "name": goal.get("name"),
        "type": goal.get("type"),
        "activity_type": goal.get("activityType"),
        "period": goal.get("period"),
        "privacy": goal.get("privacy"),
        "start_date": goal.get("startDate"),
        "end_date": goal.get("endDate"),
        "active": goal.get("active"),
        "completed": goal.get("completed"),
        "target_distance_meters": goal.get("distanceInMeters"),
        "target_duration_seconds": goal.get("durationInSeconds"),
        "target_calories": goal.get("caloriesInKiloCalories"),
        "target_activities": goal.get("numberOfActivities"),
        "progress_percent": progress.get("percent"),
        "progress_distance_meters": progress.get("distanceInMeters"),
        "progress_duration_seconds": progress.get("durationInSeconds"),
        "progress_calories": progress.get("caloriesInKiloCalories"),
        "progress_activities": progress.get("numberOfActivities"),
        "progress_days": progress.get("days"),
        "remaining_percent": remaining.get("percent"),
        "remaining_distance_meters": remaining.get("distanceInMeters"),
        "remaining_duration_seconds": remaining.get("durationInSeconds"),
        "remaining_calories": remaining.get("caloriesInKiloCalories"),
        "remaining_activities": remaining.get("numberOfActivities"),
        "remaining_days": remaining.get("days"),
        "overage_percent": overage.get("percent"),
    }
    return {key: value for key, value in curated.items() if value is not None}



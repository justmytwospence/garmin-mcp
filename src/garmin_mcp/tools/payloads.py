"""Payload builders for writes: strength exercise sets and course creation from a
parsed GPX. Vendored from Taxuspt/garmin_mcp (MIT), activity_management.py and courses.py.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, Dict, List, Optional


def _build_exercise_sets(sets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert friendly set dicts into Garmin's exerciseSets wire format.

    Mirrors what the Garmin Connect web editor sends: weight in grams with -1
    meaning unweighted, the exercise name nulled when it equals the category,
    and rest as a separate REST set following the active set it belongs to.
    """
    out: List[Dict[str, Any]] = []
    for i, s in enumerate(sets, start=1):
        category = str(s.get("category") or "").strip().upper()
        if not category:
            raise ValueError(f"set {i}: category is required")
        name = str(s.get("exercise_name") or "").strip().upper() or None

        if s.get("weight_kg") is not None and s.get("weight_lb") is not None:
            raise ValueError(f"set {i}: give weight_kg or weight_lb, not both")
        if s.get("weight_kg") is not None:
            kg = float(s["weight_kg"])
        elif s.get("weight_lb") is not None:
            kg = float(s["weight_lb"]) * 0.45359237
        else:
            kg = None
        if kg is not None and kg < 0:
            raise ValueError(f"set {i}: weight cannot be negative")

        reps = s.get("reps")
        if reps is not None and (int(reps) != reps or reps < 0):
            raise ValueError(f"set {i}: reps must be a non-negative whole number")

        out.append({
            "setType": "ACTIVE",
            "exercises": [{
                "category": category,
                "name": None if name == category else name,
                "probability": 100,
            }],
            "repetitionCount": None if reps is None else int(reps),
            "weight": -1 if kg is None else round(kg * 1000),
            "duration": s.get("duration_seconds"),
        })
        if s.get("rest_seconds"):
            out.append({
                "setType": "REST",
                "exercises": [],
                "repetitionCount": None,
                "weight": -1,
                "duration": s["rest_seconds"],
            })
    return out




def _gpx_decimal(value: Any) -> str:
    """GPX uses xsd:decimal; expand exponent notation without rounding."""
    return format(Decimal(str(value)), "f")


_EARTH_RADIUS_M = 6371000.0


def _haversine(p1: Dict[str, float], p2: Dict[str, float]) -> float:
    lat1, lon1 = math.radians(p1["latitude"]), math.radians(p1["longitude"])
    lat2, lon2 = math.radians(p2["latitude"]), math.radians(p2["longitude"])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _initial_bearing(p1: Dict[str, float], p2: Dict[str, float]) -> float:
    lat1, lat2 = math.radians(p1["latitude"]), math.radians(p2["latitude"])
    dlon = math.radians(p2["longitude"] - p1["longitude"])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    initial_bearing = math.atan2(x, y)
    initial_bearing = math.degrees(initial_bearing)
    return (initial_bearing + 360) % 360


# Garmin Connect activity typeIds (see get_activity_types).
_ACTIVITY_TYPE_IDS = {
    "running": 1,
    "cycling": 2,
    "hiking": 3,
    "walking": 9,
    "trail_running": 6,
    "mountain_biking": 5,
    "road_biking": 10,
    "gravel_cycling": 143,
}


def _build_course_payload(
    parsed_skeleton: Dict[str, Any],
    course_name: str,
    activity_type_id: int,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    geo_points = parsed_skeleton.get("geoPoints", [])
    if not geo_points:
        raise ValueError("GPX parsed skeleton contains no geoPoints")

    total_distance = 0.0
    for i in range(1, len(geo_points)):
        total_distance += _haversine(geo_points[i - 1], geo_points[i])

    for p in geo_points:
        if p.get("elevation") is None:
            p["elevation"] = 0.0

    lats = [p["latitude"] for p in geo_points]
    lons = [p["longitude"] for p in geo_points]

    bbox = {
        "center": {
            "latitude": (min(lats) + max(lats)) / 2,
            "longitude": (min(lons) + max(lons)) / 2,
        },
        "lowerLeft": {"latitude": min(lats), "longitude": min(lons)},
        "upperRight": {"latitude": max(lats), "longitude": max(lons)},
        "lowerLeftLatIsSet": True,
        "lowerLeftLongIsSet": True,
        "upperRightLatIsSet": True,
        "upperRightLongIsSet": True,
    }

    start_point = {
        "latitude": geo_points[0]["latitude"],
        "longitude": geo_points[0]["longitude"],
        "elevation": geo_points[0].get("elevation") or 0.0,
        "distance": None,
        "timestamp": None,
    }

    bearing = _initial_bearing(geo_points[0], geo_points[-1])

    return {
        "courseName": course_name,
        "description": description,
        "openStreetMap": False,
        "matchedToSegments": False,
        "userProfilePk": None,
        "userGroupPk": None,
        "rulePK": 2,  # private
        "geoRoutePk": None,
        "sourceTypeId": 3,  # GPX
        "sourcePk": None,
        "distanceMeter": total_distance,
        "elevationGainMeter": 0.0,
        "elevationLossMeter": 0.0,
        "startPoint": start_point,
        "coursePoints": [],
        "boundingBox": bbox,
        "hasShareableEvent": False,
        "hasTurnDetectionDisabled": False,
        "activityTypePk": activity_type_id,
        "virtualPartnerId": None,
        "includeLaps": False,
        "elapsedSeconds": None,
        "startBearing": bearing,
        "endBearing": None,
        "geoPoints": geo_points,
    }

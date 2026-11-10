"""Modelled weather along an activity's track, for the canonical export (`weather` in each manifest entry).

Open-Meteo values at points about ten minutes apart, each asked for at the recorded altitude: the algorithm is version 1
of the curves app's canonical schema (SPEC.md, "Weather"), pinned case by case by test/fixtures/weather-v1-fixture.json.
Pure apart from `fetch_weather`'s requests (through an injectable session) and the `WeatherCache` file.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("garmin_mcp.weather")

WEATHER_VERSION = 1
BASES = {
    "historical-forecast": "https://historical-forecast-api.open-meteo.com/v1/forecast",
    "archive": "https://archive-api.open-meteo.com/v1/archive",
}
STEP_S = 600
CHUNK = 50
HEADROOM = 0.1  # Open-Meteo reports how much of each window is left (X-RateLimit-*); pause when less than this share remains
MAX_WAIT = 120.0  # a pause up to this long (seconds) is waited out in place; a longer one stops the run
HOURLY = "temperature_2m,dew_point_2m,wind_speed_10m,wind_direction_10m"
NONE_DAYS = 30

# a point of the track: (seconds from the start, latitude x 1e7, longitude x 1e7, altitude in m or None)
Point = tuple[int, int, int, "float | None"]
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class WeatherFormatError(Exception):
    pass


class RateLimited(Exception):
    """Open-Meteo said slow down (or is about to): `scope` is the window that ran out, `until` when to try again (epoch seconds)."""

    def __init__(self, scope: str, message: str = "", until: float | None = None) -> None:
        super().__init__(message or f"Open-Meteo rate limit ({scope})")
        self.scope = scope
        self.until = until


def limit_scope(reason: str) -> str:
    if re.search("daily", reason, re.I):
        return "day"
    if re.search("hourly", reason, re.I):
        return "hour"
    return "minute"


def plan_points(indoor: bool, manual: bool, t: Sequence[int], lat: Sequence[int | None], lng: Sequence[int | None], alt: Sequence[float | None] | None) -> list[Point]:
    """Steps 1 and 2: the points to ask about; an empty list means "no weather"."""
    if indoor or manual:
        return []
    rows = [i for i in range(len(t)) if lat[i] is not None and lng[i] is not None and (lat[i] != 0 or lng[i] != 0)]
    if len(rows) < 2:
        return []

    def pt(i: int) -> Point:
        a = alt[i] if alt is not None else None
        return (int(t[i]), int(lat[i]), int(lng[i]), float(a) if a is not None and math.isfinite(a) else None)  # type: ignore[arg-type]

    out = [pt(rows[0])]
    for i in rows:
        if t[i] >= out[-1][0] + STEP_S:
            out.append(pt(i))
    last = rows[-1]
    if t[last] > out[-1][0]:
        out.append(pt(last))
    return out


def fmt4(e7: int) -> str:
    """Degrees x 1e7 as degrees with exactly 4 decimals, rounded half up by integer arithmetic."""
    n = (e7 + 500) // 1000
    a = abs(n)
    return f"{'-' if n < 0 else ''}{a // 10000}.{a % 10000:04d}"


def start_ms_of(start_time: str) -> int:
    """Whole milliseconds since the epoch of an ISO 8601 UTC time."""
    dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    return (dt - _EPOCH) // timedelta(milliseconds=1)


def _day(ms: int) -> str:
    return (_EPOCH + timedelta(milliseconds=ms)).strftime("%Y-%m-%d")


def requests_for(points: list[Point], start_ms: int, api: str, bases: dict[str, str] | None = None) -> list[tuple[str, int]]:
    """Steps 3 and 4: the requests, in order, with the number of locations in each."""
    base = (bases or BASES)[api]
    out = []
    for i in range(0, len(points), CHUNK):
        ch = points[i : i + CHUNK]
        lat = ",".join(fmt4(p[1]) for p in ch)
        lng = ",".join(fmt4(p[2]) for p in ch)
        elev = ",".join("nan" if p[3] is None else str(math.floor(p[3] + 0.5)) for p in ch)
        d0, d1 = _day(start_ms + ch[0][0] * 1000), _day(start_ms + (ch[-1][0] + 3600) * 1000)
        out.append((
            f"{base}?latitude={lat}&longitude={lng}&elevation={elev}&hourly={HOURLY}"
            f"&wind_speed_unit=ms&timeformat=unixtime&timezone=GMT&start_date={d0}&end_date={d1}",
            len(ch),
        ))
    return out


def _lerp_at(h: dict[str, Any], key: str, T: float) -> float | None:
    v = h.get(key)
    times = h["time"]
    if not v:
        return None
    for j in range(len(times) - 1):
        t0, t1 = times[j], times[j + 1]
        if t0 <= T <= t1:
            a, b = v[j], v[j + 1]
            if a is None or b is None:
                return None
            f = (T - t0) / (t1 - t0)
            return a + (b - a) * f
    return None


def _near_at(h: dict[str, Any], key: str, T: float) -> float | None:
    v = h.get(key)
    times = h["time"]
    if not v:
        return None
    for j in range(len(times) - 1):
        t0, t1 = times[j], times[j + 1]
        if t0 <= T <= t1:
            return v[j if T - t0 <= t1 - T else j + 1]
    return None


def _r1(x: float) -> float:
    return math.floor(x * 10 + 0.5) / 10


def parse_weather(points: list[Point], start_ms: int, bodies: list[Any]) -> dict[str, Any]:
    """Steps 5 and 6: the kept points from the responses (one per request, an object or a list of locations)."""
    locs: list[Any] = []
    for b in bodies:
        locs.extend(b if isinstance(b, list) else [b])
    if len(locs) != len(points):
        raise WeatherFormatError(f"{len(locs)} locations for {len(points)} points")
    out: dict[str, Any] = {"t": [], "elev": [], "temp": [], "dew": [], "wind": [], "wind_dir": []}
    for p, loc in zip(points, locs, strict=True):
        h = loc.get("hourly") if isinstance(loc, dict) else None
        if not h or not isinstance(h.get("time"), list):
            raise WeatherFormatError("a response without hourly.time")
        T = start_ms / 1000 + p[0]
        temp = _lerp_at(h, "temperature_2m", T)
        if p[3] is not None:
            elev: int | None = math.floor(p[3] + 0.5)
        elif isinstance(loc.get("elevation"), (int, float)):
            elev = math.floor(loc["elevation"] + 0.5)
        else:
            elev = None
        if temp is None or elev is None:
            continue
        dew, wind, wdir = _lerp_at(h, "dew_point_2m", T), _lerp_at(h, "wind_speed_10m", T), _near_at(h, "wind_direction_10m", T)
        out["t"].append(p[0])
        out["elev"].append(elev)
        out["temp"].append(_r1(temp))
        out["dew"].append(None if dew is None else _r1(dew))
        out["wind"].append(None if wind is None else _r1(wind))
        out["wind_dir"].append(None if wdir is None else math.floor(wdir + 0.5))
    return out


def _num(res: Any, key: str) -> float | None:
    try:
        v = res.headers.get(key)
        return None if v is None else float(v)
    except (AttributeError, TypeError, ValueError):
        return None


def _scope_of(wait: float) -> str:
    return "minute" if wait <= MAX_WAIT else "hour" if wait <= 3600 else "day"


class Limiter:
    """Learns Open-Meteo's limits from its responses and keeps the run inside them, whoever else shares the IP address:
    the X-RateLimit-* headers say how much of the tightest window is left and when it resets."""

    def __init__(self, clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self.clock = clock
        self.sleep = sleep
        self.until = 0.0  # when the current pause ends (epoch seconds)
        self.total = 0  # calls taken through this limiter

    def pause_left(self) -> float:
        return max(0.0, self.until - self.clock())

    def take(self, n: int) -> None:
        """Before a request of `n` locations: waits out a short pause, raises RateLimited for a long one."""
        while True:
            wait = self.pause_left()
            if wait <= 0:
                break
            if wait > MAX_WAIT:
                raise RateLimited(_scope_of(wait), "Open-Meteo asked for a pause", self.until)
            self.sleep(wait + 0.001)
        self.total += n

    def observe(self, res: Any) -> None:
        remaining, limit, reset = _num(res, "x-ratelimit-remaining"), _num(res, "x-ratelimit-limit"), _num(res, "x-ratelimit-reset")
        if remaining is not None and limit and reset is not None and remaining < limit * HEADROOM:
            self.until = max(self.until, reset + 1)

    def hit(self, res: Any, scope: str) -> None:
        """Open-Meteo refused: pause for Retry-After, else for the scope's window."""
        t = self.clock()
        retry = _num(res, "retry-after")
        if retry is not None:
            until = t + retry + 1
        elif scope == "day":
            until = _next_day(datetime.fromtimestamp(t, UTC)).timestamp()
        elif scope == "hour":
            until = _next_hour(datetime.fromtimestamp(t, UTC)).timestamp()
        else:
            until = t + 60
        self.until = max(self.until, until)


def _get_json(session: Any, url: str, n: int, limiter: Limiter) -> Any:
    for attempt in range(2):
        limiter.take(n)
        res = session.get(url, timeout=60)
        limiter.observe(res)
        limited: RateLimited | None = None
        body: Any = None
        if res.status_code == 429:
            try:
                reason = str((res.json() or {}).get("reason", ""))
            except ValueError:
                reason = ""
            limited = RateLimited(limit_scope(reason), reason or "HTTP 429")
        elif res.status_code >= 400:
            raise RuntimeError(f"HTTP {res.status_code}")
        else:
            body = res.json()
            if isinstance(body, dict) and body.get("error") is True:
                reason = str(body.get("reason", ""))
                if re.search("limit", reason, re.I):
                    limited = RateLimited(limit_scope(reason), reason)
                else:
                    raise RuntimeError(reason or "Open-Meteo error")
        if limited is None:
            return body
        limiter.hit(res, limited.scope)
        if attempt >= 1 or limiter.pause_left() > MAX_WAIT:
            raise RateLimited(limited.scope, str(limited), limiter.until)
    raise AssertionError("unreachable")


def _attempt(points: list[Point], start_ms: int, api: str, session: Any, limiter: Limiter, bases: dict[str, str] | None) -> dict[str, Any]:
    bodies = [_get_json(session, url, n, limiter) for url, n in requests_for(points, start_ms, api, bases)]
    return parse_weather(points, start_ms, bodies)


def fetch_weather(
    start_time: str, indoor: bool, manual: bool, t: Sequence[int], lat: Sequence[int | None], lng: Sequence[int | None], alt: Sequence[float | None] | None,
    *, session: Any, limiter: Limiter, now: Callable[[], str], bases: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Steps 1 to 8: the activity's weather, or None when it has none (indoor, no track, nothing from the model)."""
    points = plan_points(indoor, manual, t, lat, lng, alt)
    if not points:
        return None
    start_ms = start_ms_of(start_time)
    api = "historical-forecast"
    best = _attempt(points, start_ms, api, session, limiter, bases)
    if len(best["t"]) * 2 < len(points):
        other = _attempt(points, start_ms, "archive", session, limiter, bases)
        if len(other["t"]) > len(best["t"]):
            best, api = other, "archive"
    if not best["t"]:
        return None
    return {"version": WEATHER_VERSION, "source": "open-meteo", "api": api, "fetched_at": now(), **best}


def _next_hour(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def _next_day(t: datetime) -> datetime:
    return t.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class WeatherCache:
    """`weather-v1.json` beside the export: per activity the weather found for a given fingerprint (or None, with when it was
    tried) and a pause after Open-Meteo's limits. A sidecar file, never served."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = {"version": 1, "retry_after": None, "entries": {}}
        try:
            d = json.loads(self.path.read_text())
            if isinstance(d, dict) and d.get("version") == 1:
                self.data = {**self.data, **d}
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data))
        os.replace(tmp, self.path)

    def weather(self, aid: str, fingerprint: str) -> dict[str, Any] | None:
        e = self.data["entries"].get(aid)
        return e["weather"] if e and e.get("fingerprint") == fingerprint else None

    def done(self, aid: str, fingerprint: str, now: datetime) -> bool:
        e = self.data["entries"].get(aid)
        if not e or e.get("fingerprint") != fingerprint:
            return False
        if e.get("weather") is not None:
            return True
        return now - datetime.fromisoformat(e["tried_at"].replace("Z", "+00:00")) < timedelta(days=NONE_DAYS)

    def forget(self, keep: set[str]) -> None:
        for k in [k for k in self.data["entries"] if k not in keep]:
            del self.data["entries"][k]


def fill_weather(
    cache: WeatherCache,
    candidates: list[dict[str, Any]],
    read_samples: Callable[[str], tuple[list[int], list[int | None], list[int | None], list[float | None]]],
    session: Any,
    *,
    max_calls: int = 2000,
    max_seconds: float = 300,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    bases: dict[str, str] | None = None,
) -> dict[str, int]:
    """Fetch weather for the candidates that need it, newest first, within a budget; never raises for a weather problem.

    A candidate is `{id, fingerprint, start_time, indoor, manual}`. The cache is saved every 25 activities and at the end.
    """
    stats = {"fetched": 0, "none": 0, "failed": 0}
    now_dt = lambda: datetime.fromtimestamp(clock(), UTC)  # noqa: E731
    retry = cache.data.get("retry_after")
    if retry and now_dt() < datetime.fromisoformat(retry.replace("Z", "+00:00")):
        return stats
    limiter = Limiter(clock, sleep)
    todo = [c for c in candidates if not c["indoor"] and not c["manual"] and not cache.done(c["id"], c["fingerprint"], now_dt())]
    todo.sort(key=lambda c: c["start_time"], reverse=True)
    began, since_save, failures = clock(), 0, 0
    try:
        for c in todo:
            if limiter.total >= max_calls or clock() - began > max_seconds:
                break
            try:
                t, lat, lng, alt = read_samples(c["id"])
                w = fetch_weather(
                    c["start_time"], c["indoor"], c["manual"], t, lat, lng, alt,
                    session=session, limiter=limiter, now=lambda: _iso(now_dt()), bases=bases,
                )
            except RateLimited:
                raise
            except Exception:  # noqa: BLE001 - one activity's trouble must not stop the rest
                log.exception("weather: %s failed", c["id"])
                stats["failed"] += 1
                failures += 1
                if failures >= 5:
                    break
                continue
            failures = 0
            cache.data["entries"][c["id"]] = {"fingerprint": c["fingerprint"], "weather": w, "tried_at": _iso(now_dt())}
            stats["none" if w is None else "fetched"] += 1
            since_save += 1
            if since_save >= 25:
                cache.save()
                since_save = 0
    except RateLimited as e:
        t = now_dt()
        until = datetime.fromtimestamp(e.until, UTC) if e.until else _next_day(t) if e.scope == "day" else _next_hour(t) if e.scope == "hour" else t + timedelta(minutes=1)
        cache.data["retry_after"] = _iso(until)
        log.warning("weather: Open-Meteo limit (%s), pausing until %s", e.scope, cache.data["retry_after"])
    else:
        if cache.data.get("retry_after"):
            cache.data["retry_after"] = None
    finally:
        cache.save()
    return stats

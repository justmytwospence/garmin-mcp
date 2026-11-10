"""Modelled weather along a track: the conformance fixture shared with the curves app (SPEC.md, Weather), and the rest of the algorithm."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from garmin_mcp.weather import (
    BASES,
    PER_DAY,
    PER_MINUTE,
    Limiter,
    RateLimited,
    WeatherCache,
    WeatherFormatError,
    fetch_weather,
    fill_weather,
    fmt4,
    limit_scope,
    parse_weather,
    plan_points,
    requests_for,
    start_ms_of,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "weather-v1-fixture.json").read_text())


class Res:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        return self._body


class Replay:
    """A session that answers the fixture's requests (and records what was asked)."""

    def __init__(self, exchanges: list[dict[str, Any]]) -> None:
        self.exchanges = exchanges
        self.asked: list[str] = []

    def get(self, url: str, timeout: float = 0) -> Res:
        self.asked.append(url)
        for e in self.exchanges:
            if e["url"] == url:
                return Res(e["status"], e["body"])
        raise AssertionError(f"unexpected request {url}")


class SyntheticMeteo:
    """Open-Meteo for any request: 15 degrees C everywhere, from the query's dates and locations. `status`/`reason` make it fail."""

    def __init__(self, status: int = 200, reason: str = "") -> None:
        self.calls = 0
        self.urls: list[str] = []
        self.status, self.reason = status, reason

    def get(self, url: str, timeout: float = 0) -> Res:
        from urllib.parse import parse_qs, urlparse

        self.calls += 1
        self.urls.append(url)
        if self.status != 200:
            return Res(self.status, {"error": True, "reason": self.reason})
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        t0 = int(datetime.fromisoformat(q["start_date"]).replace(tzinfo=UTC).timestamp())
        t1 = int(datetime.fromisoformat(q["end_date"]).replace(tzinfo=UTC).timestamp()) + 23 * 3600
        times = list(range(t0, t1 + 1, 3600))
        locs = [
            {"elevation": 1234, "hourly": {"time": times, "temperature_2m": [15.0] * len(times), "dew_point_2m": [7.0] * len(times),
                                           "wind_speed_10m": [3.0] * len(times), "wind_direction_10m": [270] * len(times)}}
            for _ in q["latitude"].split(",")
        ]
        return Res(200, locs[0] if len(locs) == 1 else locs)


def limiter() -> Limiter:
    return Limiter({"day": "", "used": 0}, clock=lambda: 1_800_000_000.0, sleep=lambda s: None)


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["name"])
def test_fixture_case(case):
    s = case["samples"]
    session = Replay(case["exchanges"])
    w = fetch_weather(
        case["entry"]["start_time"], case["entry"]["indoor"], case["entry"]["manual"], s["t"], s["lat"], s["lng"], s["alt"] if s["alt"][0] is not None else None,
        session=session, limiter=limiter(), now=lambda: case["now"],
    )
    assert session.asked == [e["url"] for e in case["exchanges"]]
    assert w == case["weather"]


def test_fixture_has_the_cases_every_implementation_needs():
    assert [c["name"] for c in FIXTURE["cases"]] == ["track", "no-altitude", "indoor", "no-position", "archive-fallback", "long"]


def test_plan_points_takes_first_every_600_s_and_last():
    n = 1501
    t = list(range(n))
    lat, lng, alt = [450_000_000] * n, [-1_200_000_000] * n, [100.0] * n
    assert [p[0] for p in plan_points(False, False, t, lat, lng, alt)] == [0, 600, 1200, 1500]
    assert [p[0] for p in plan_points(False, False, t[:1201], lat[:1201], lng[:1201], alt[:1201])] == [0, 600, 1200]


def test_plan_points_has_none_without_a_track():
    t, lat, lng = [0, 700], [450_000_000] * 2, [-1_200_000_000] * 2
    assert plan_points(True, False, t, lat, lng, None) == []
    assert plan_points(False, True, t, lat, lng, None) == []
    assert plan_points(False, False, t, [0, 0], [0, 0], None) == []
    assert plan_points(False, False, t, [None, None], [None, None], None) == []
    assert plan_points(False, False, [5], [450_000_000], [-1_200_000_000], None) == []
    assert [p[3] for p in plan_points(False, False, t, lat, lng, None)] == [None, None]


def test_coordinates_round_half_up_in_integers():
    assert fmt4(450_000_000) == "45.0000"
    assert fmt4(-1_200_000_000) == "-120.0000"
    assert fmt4(10_312_500) == "1.0313"
    assert fmt4(-10_312_500) == "-1.0312"
    assert fmt4(-40) == "0.0000"
    assert fmt4(-600) == "-0.0001"


def test_requests_chunk_at_50_and_date_each_chunk():
    pts = [(i * 600, 450_000_000, -1_200_000_000, 100.5 if i % 2 else None) for i in range(120)]
    reqs = requests_for(pts, start_ms_of("2024-07-15T23:00:00.000Z"), "archive")
    assert [n for _, n in reqs] == [50, 50, 20]
    assert reqs[0][0].startswith(f"{BASES['archive']}?latitude=45.0000,")
    assert "&elevation=nan,101,nan," in reqs[0][0]
    assert "&start_date=2024-07-15&end_date=2024-07-16" in reqs[0][0]


def test_start_time_keeps_its_milliseconds():
    assert start_ms_of("2024-07-15T22:30:00.123Z") == 1721082600123


def body(temp, direction=(90, 180)):
    return {"elevation": 100, "hourly": {"time": [0, 3600], "temperature_2m": temp, "dew_point_2m": [0, 0], "wind_speed_10m": [1, 2], "wind_direction_10m": list(direction)}}


def test_parse_interpolates_and_takes_the_nearer_hour_for_direction():
    p = (1000, 450_000_000, -1_200_000_000, 100.0)
    r = parse_weather([p], 0, [body([10, 20])])
    assert r["temp"] == [12.8] and r["wind"] == [1.3] and r["wind_dir"] == [90]
    assert parse_weather([(2000, *p[1:])], 0, [body([10, 20])])["wind_dir"] == [180]
    assert parse_weather([(1800, *p[1:])], 0, [body([10, 20])])["wind_dir"] == [90]


def test_parse_drops_missing_and_refuses_malformed():
    p = (0, 450_000_000, -1_200_000_000, 100.0)
    assert parse_weather([p], 0, [body([None, 20])])["t"] == []
    assert parse_weather([(7200, *p[1:])], 0, [body([10, 20])])["t"] == []
    with pytest.raises(WeatherFormatError):
        parse_weather([p], 0, [{}])
    r = parse_weather([p, (600, *p[1:])], 0, [[body([10, 20]), body([30, 40])]])
    assert r["temp"] == [10, 31.7]


def test_limit_scope():
    assert limit_scope("Daily API request limit exceeded") == "day"
    assert limit_scope("Hourly API request limit exceeded") == "hour"
    assert limit_scope("Minutely API request limit exceeded") == "minute"


def one_case():
    c = FIXTURE["cases"][0]
    s = c["samples"]
    return c, (c["entry"]["start_time"], False, False, s["t"], s["lat"], s["lng"], s["alt"])


def test_a_limited_minute_is_waited_out_once():
    c, args = one_case()
    sleeps: list[float] = []

    class Limited:
        calls = 0

        def get(self, url, timeout=0):
            self.calls += 1
            return Res(200, {"error": True, "reason": "Minutely API request limit exceeded"})

    s = Limited()
    with pytest.raises(RateLimited) as e:
        fetch_weather(*args, session=s, limiter=limiter(), now=lambda: c["now"], sleep=sleeps.append)
    assert e.value.scope == "minute" and s.calls == 2 and sleeps == [60]
    ok = c["exchanges"][0]["body"]

    class Recovers:
        calls = 0

        def get(self, url, timeout=0):
            self.calls += 1
            return Res(429, {"error": True, "reason": "Minutely API request limit exceeded"}) if self.calls == 1 else Res(200, ok)

    assert fetch_weather(*args, session=Recovers(), limiter=limiter(), now=lambda: c["now"], sleep=lambda s: None) == c["weather"]


def test_a_daily_limit_stops_at_once_and_other_errors_are_errors():
    c, args = one_case()

    class Daily:
        calls = 0

        def get(self, url, timeout=0):
            self.calls += 1
            return Res(429, {"error": True, "reason": "Daily API request limit exceeded"})

    s = Daily()
    with pytest.raises(RateLimited) as e:
        fetch_weather(*args, session=s, limiter=limiter(), now=lambda: c["now"])
    assert e.value.scope == "day" and s.calls == 1

    class Broken:
        def get(self, url, timeout=0):
            return Res(500, {})

    with pytest.raises(RuntimeError, match="HTTP 500"):
        fetch_weather(*args, session=Broken(), limiter=limiter(), now=lambda: c["now"])


def test_limiter_waits_at_the_minute_cap_and_stops_at_the_day_cap():
    t = [1_800_000_000.0]
    slept: list[float] = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    lim = Limiter({"day": "", "used": 0}, clock=lambda: t[0], sleep=sleep)
    for _ in range(PER_MINUTE // 50):
        lim.take(50)
    assert slept == []
    lim.take(50)
    assert len(slept) == 1 and slept[0] >= 59
    day = Limiter({"day": "2027-01-15", "used": PER_DAY - 10}, clock=lambda: 1_800_000_000.0)
    assert datetime.fromtimestamp(1_800_000_000, UTC).strftime("%Y-%m-%d") == "2027-01-15"
    with pytest.raises(RateLimited) as e:
        day.take(11)
    assert e.value.scope == "day"
    nxt = Limiter({"day": "2027-01-14", "used": PER_DAY}, clock=lambda: 1_800_000_000.0)
    nxt.take(11)
    assert nxt.state == {"day": "2027-01-15", "used": 11}


def candidates(n: int):
    return [
        {"id": f"garmin:{i}", "fingerprint": f"f{i}", "start_time": f"2024-07-15T18:00:{i:02d}.000Z", "indoor": False, "manual": False}
        for i in range(n)
    ]


def reader(aid: str):
    c = FIXTURE["cases"][0]["samples"]
    return c["t"], c["lat"], c["lng"], c["alt"]


class Server:
    """Open-Meteo for any request: the fixture's `track` body (one location per point, so any chunk it asks for fits)."""

    def __init__(self) -> None:
        self.calls = 0
        c = FIXTURE["cases"][0]
        self.body = c["exchanges"][0]["body"]

    def get(self, url, timeout=0):
        self.calls += 1
        n = len(url.split("latitude=")[1].split("&")[0].split(","))
        return Res(200, self.body if n == len(self.body) else [self.body[0]] * n)


def test_fill_fetches_newest_first_and_remembers(tmp_path):
    cache = WeatherCache(tmp_path / "weather-v1.json")
    seen: list[str] = []

    def read(aid):
        seen.append(aid)
        return reader(aid)

    server = Server()
    stats = fill_weather(cache, candidates(3), read, server, max_calls=1000)
    assert stats == {"fetched": 3, "none": 0, "failed": 0} and seen == ["garmin:2", "garmin:1", "garmin:0"]
    again = WeatherCache(tmp_path / "weather-v1.json")
    assert again.weather("garmin:1", "f1")["version"] == 1 and again.weather("garmin:1", "other") is None
    before = server.calls
    assert fill_weather(again, candidates(3), read, server) == {"fetched": 0, "none": 0, "failed": 0}
    assert server.calls == before


def test_fill_skips_indoor_and_manual_and_remembers_none_for_30_days(tmp_path):
    cache = WeatherCache(tmp_path / "w.json")
    cs = candidates(3)
    cs[0]["indoor"], cs[1]["manual"] = True, True
    server = Server()
    assert fill_weather(cache, cs, reader, server)["fetched"] == 1
    # a track with no positions is "none": not asked about again for 30 days
    empty = lambda aid: ([0, 1], [None, None], [None, None], [None, None])  # noqa: E731
    cache2 = WeatherCache(tmp_path / "w2.json")
    assert fill_weather(cache2, candidates(1), empty, server) == {"fetched": 0, "none": 1, "failed": 0}
    assert fill_weather(cache2, candidates(1), empty, server)["none"] == 0
    old = datetime.now(UTC) - timedelta(days=31)
    cache2.data["entries"]["garmin:0"]["tried_at"] = old.isoformat().replace("+00:00", "Z")
    assert fill_weather(cache2, candidates(1), empty, server)["none"] == 1


def test_fill_stops_at_its_budget(tmp_path):
    cache = WeatherCache(tmp_path / "w.json")
    server = Server()
    stats = fill_weather(cache, candidates(5), reader, server, max_calls=8)  # an activity here is 4 calls
    assert stats["fetched"] == 2


def test_fill_pauses_after_a_daily_limit(tmp_path):
    cache = WeatherCache(tmp_path / "w.json")

    class Daily:
        calls = 0

        def get(self, url, timeout=0):
            self.calls += 1
            return Res(429, {"error": True, "reason": "Daily API request limit exceeded"})

    s = Daily()
    assert fill_weather(cache, candidates(3), reader, s) == {"fetched": 0, "none": 0, "failed": 0}
    assert s.calls == 1 and cache.data["retry_after"] is not None
    fill_weather(WeatherCache(tmp_path / "w.json"), candidates(3), reader, s)
    assert s.calls == 1  # paused until the next UTC day


def test_fill_survives_an_activity_that_fails(tmp_path):
    cache = WeatherCache(tmp_path / "w.json")

    def read(aid):
        if aid == "garmin:2":
            raise OSError("unreadable")
        return reader(aid)

    stats = fill_weather(cache, candidates(3), read, Server())
    assert stats == {"fetched": 2, "none": 0, "failed": 1}

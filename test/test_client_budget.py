"""Offline retry/admission accounting with fake clocks and transports."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace

import pytest

from garmin_mcp.garmin.client import GarminClient, GarminConnectTransport, RateLimited, Response, Unavailable, retry_after


class Clock:
    t = 100.0
    def clock(self):
        return self.t
    def now(self):
        return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=self.t)
    def sleep(self, seconds):
        self.t += seconds


class Transport:
    display_name = "synthetic"
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0
    def request(self, *args, **kwargs):
        self.calls += 1
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response
    def call(self, *args, **kwargs):
        return self.request()


def client(responses):
    clock = Clock()
    samples = []
    transport = Transport(responses)
    c = GarminClient(transport, clock=clock.clock, now=clock.now, sleep=clock.sleep, background_gap=0, interactive_gap=0, jitter=0, on_sample=samples.append)
    return c, transport, clock, samples


def test_retries_record_exactly_once_before_sleep():
    c, transport, clock, samples = client([Response(503, b"", headers={"Retry-After": "12"}), Unavailable("network"), Response(200, b"ok")])
    assert c.get_text("/synthetic") == "ok"
    assert c.state.requests == transport.calls == len(samples) == 3
    assert c.state.errors == 2
    assert [s.status for s in samples] == [503, None, 200]
    assert [s.ms for s in samples] == [0, 0, 0], "backoff is not response latency"
    assert clock.t == 120  # 12 seconds Retry-After, then exponential 8 seconds


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "CALL"])
def test_ambiguous_writes_never_replay(method):
    c, transport, _, samples = client([Unavailable("lost response", 503), Response(200, b"unexpected")])
    with pytest.raises(Unavailable):
        if method == "CALL":
            c.call("synthetic_write")
        else:
            c.request(method, "/synthetic")
    assert transport.calls == c.state.requests == len(samples) == 1


@pytest.mark.parametrize("header", ["3600", "http-date", "garbage", "NaN", "-10"])
def test_429_stops_all_admission_and_reports_observation(header):
    c, transport, clock, samples = client([])
    value = format_datetime(clock.now() + timedelta(hours=1), usegmt=True) if header == "http-date" else header
    transport.responses = iter([Response(429, b"", headers={"Retry-After": value})])
    with pytest.raises(RateLimited):
        c.get_text("/synthetic")
    status = c.status()
    expected = 3600 if header in ("3600", "http-date") else 1800
    assert status["backoff_remaining_s"] == expected
    assert status["observed_at"] == clock.now().isoformat()
    assert status["rate_limit_observed_at"] == clock.now().isoformat()
    assert status["cooldown_until"] == (clock.now() + timedelta(seconds=expected)).isoformat()
    assert status["general_quota"]["remaining"] is None
    def blocked(_):
        with pytest.raises(RateLimited):
            c.get_text("/synthetic", priority="interactive")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(blocked, range(8)))
    assert transport.calls == c.state.requests == len(samples) == 1


def test_retry_after_error_chain_and_library_retries_disabled():
    import garminconnect

    session = SimpleNamespace(hooks={})
    api = SimpleNamespace(retry_attempts=3, client=SimpleNamespace(_api_session=session))
    transport = GarminConnectTransport(api)
    assert api.retry_attempts == 0
    response = SimpleNamespace(headers={"Retry-After": "3600", "Set-Cookie": "never-retain"})
    session.hooks["response"][0](response)
    err = garminconnect.GarminConnectConnectionError("API Error 429")
    assert transport._translate(err).retry_after_s == 3600
    assert transport._last_headers == {"Retry-After": "3600"}
    assert retry_after({"Retry-After": "invalid"}, datetime.now(UTC)) is None


def test_reload_reservation_prevents_ambiguous_replay_after_restart(db, tmp_path):
    from garmin_mcp.sync.engine import SyncEngine

    c, transport, _, _ = client([Unavailable("lost response", 503)])
    engine = SyncEngine(db, c, str(tmp_path))
    with pytest.raises(Unavailable):
        engine.run_job("reload", "2020-01-01", 5)
    assert db.scalar("SELECT status FROM reload_request") == "outcome_unknown"
    restarted = SyncEngine(db, c, str(tmp_path))
    assert restarted.run_job("reload", "2020-01-01", 5) is False
    assert transport.calls == 1
    assert db.scalar("SELECT count(*) FROM sync_job WHERE kind = 'refetch_day'") == 1


def test_reload_quota_concurrent_admission_and_next_slot(db, tmp_path):
    from garmin_mcp.sync.engine import SyncEngine, _Deferred

    db.run("INSERT INTO reload_request (calendar_date, requested_at, status) SELECT DATE '2020-01-01' + i::INTEGER, now(), 'COMPLETE' FROM range(24) t(i)")
    c, transport, _, _ = client([Response(200, b'{"status":"COMPLETE"}')])
    engine = SyncEngine(db, c, str(tmp_path))
    def reload(day):
        try:
            engine.run_job("reload", day, 5)
            return "sent"
        except _Deferred:
            return "deferred"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reload, ["2020-02-01", "2020-02-02"])) == ["deferred", "sent"]
    status = engine.status()
    assert status["reload_quota"]["used_last_24h"] == 25
    assert status["reload_quota"]["next_local_slot_at"]
    assert status["reload_quota"]["provider_remaining"] is None
    assert transport.calls == 1


def test_overfull_reload_ledger_reports_a_real_next_slot(db, tmp_path):
    from garmin_mcp.sync.fetchers import Fetchers

    db.run("INSERT INTO reload_request (calendar_date, requested_at, status) SELECT DATE '2020-01-01' + i::INTEGER, now() - i * INTERVAL 1 MINUTE, 'COMPLETE' FROM range(27) t(i)")
    c, _, _, _ = client([])
    fetch = Fetchers(db, c, tmp_path)
    expected = db.scalar("SELECT min(requested_at) + INTERVAL 24 HOUR + INTERVAL 5 MINUTE FROM (SELECT requested_at FROM reload_request ORDER BY requested_at DESC LIMIT 25)")
    assert fetch.next_reload_slot() == expected


def test_retry_exhaustion_counts_all_attempts_once():
    c, transport, _, samples = client([Unavailable("network")] * 4)
    with pytest.raises(Unavailable):
        c.get_text("/synthetic")
    assert transport.calls == c.state.requests == c.state.errors == len(samples) == 4

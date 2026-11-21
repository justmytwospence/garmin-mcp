"""GARMIN_TRANSPORT=hybrid: routing by path, writes and calls to Connect, falling back to Connect when the Personal API
fails, the Personal API's datasets syncing while Connect's login is broken, the login, the settings and the
unchanged connect and personal modes."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from garmin_mcp.cli import health_body, open_runtime, validate_settings
from garmin_mcp.export import CanonicalExporter
from garmin_mcp.garmin.auth import HybridLogin, LoginManager, PendingTransport, PersonalLogin
from garmin_mcp.garmin.client import AuthError, ConnectUnavailable, GarminClient, Response, Unavailable, Unsupported
from garmin_mcp.garmin.datasets import ACTIVITY_FIT_PATH, ACTIVITY_LIST_PATH, BY_NAME, RELOAD_PATH, WELLNESS_FIT_PATH
from garmin_mcp.garmin.hybrid import RoutingTransport
from garmin_mcp.garmin.personal import SUPPORTED_PATHS, PersonalApiTransport, PersonalToken, serves
from garmin_mcp.mirror import Mirror
from garmin_mcp.snapshot import SnapshotPublisher
from garmin_mcp.sync.engine import job_path
from garmin_mcp.tools.mirror_tools import login_state
from test_engine import drain, make_engine
from test_personal import NOW, Resp, Session, jwt

from fake import FakeTransport


class PersonalFake(FakeTransport):
    """The Personal API side: the same fixtures, the Personal transport's display name, and scripted failures."""

    def __init__(self) -> None:
        super().__init__()
        self.display_name = "personal-api"
        self.statuses: list[int] = []

    def request(self, method, path, **kw):
        if self.statuses:
            self.calls.append((method, path, kw.get("params")))
            return Response(self.statuses.pop(0), b"", "")
        return super().request(method, path, **kw)


def pair(connect=True):
    personal, conn = PersonalFake(), FakeTransport() if connect else None
    return RoutingTransport(personal, conn), personal, conn


def paths(t):
    return [c[1] for c in t.calls]


# ----------------------------------------------------------------- the matcher


def resolved(template: str) -> str:
    fmt = {"dn": "display-name-fixture", "key": "21000000001", "start": "2026-01-01", "end": "2026-01-28"}
    return template.format(**fmt)


def test_matcher_agrees_with_supports_for_every_path_the_mirror_requests():
    templates = {ds.path for ds in BY_NAME.values()} | {ACTIVITY_LIST_PATH, ACTIVITY_FIT_PATH, WELLNESS_FIT_PATH, RELOAD_PATH}
    for t in templates:
        assert serves(resolved(t)) == (t in SUPPORTED_PATHS), t
    assert SUPPORTED_PATHS <= templates


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/activity-service/activity/21000000001", True),
        ("/download-service/files/activity/21000000001", True),
        ("/hrv-service/hrv/2026-01-02", True),
        ("/hrv-service/hrv/daily/2026-01-01/2026-01-28", True),
        ("/usersummary-service/stats/steps/daily/2026-01-01/2026-01-28", True),
        ("/weight-service/weight/range/2026-01-01/2026-01-28?includeAll=true", True),
        ("/usersummary-service/v2/dailysummaries?startDate=2026-01-01&endDate=2026-01-28", True),
        (ACTIVITY_LIST_PATH + "?start=0&limit=100", True),
        ("/activity-service/activity/activityTypes", False),
        ("/activity-service/activity/eventTypes", False),
        ("/activity-service/activity/21000000001/splits", False),
        ("/activity-service/activity/polyline/21000000001", False),
        ("/activity-service/activity/21000000001/", False),
        ("/hrv-service/hrv/", False),
        ("/download-service/files/wellness/2026-01-02", False),
        ("/wellness-service/wellness/dailyStress/2026-01-02", False),
        ("/x/activity-service/activity/21000000001", False),
    ],
)
def test_matcher_on_resolved_paths(path, expected):
    assert serves(path) is expected


# ----------------------------------------------------------------- routing


def test_supported_gets_go_to_the_personal_api_and_the_rest_to_connect():
    t, personal, conn = pair()
    t.request("GET", "/activity-service/activity/21000000001")
    t.request("GET", "/hrv-service/hrv/daily/2026-01-01/2026-01-28")
    t.request("GET", ACTIVITY_LIST_PATH, params={"start": "0", "limit": "100"})
    t.request("GET", "/usersummary-service/v2/dailysummaries", params={"startDate": "2026-01-01", "endDate": "2026-01-28"})
    assert len(personal.calls) == 4 and conn.calls == []
    t.request("GET", "/activity-service/activity/activityTypes")
    t.request("GET", "/wellness-service/wellness/dailyStress/2026-01-02")
    assert paths(conn) == ["/activity-service/activity/activityTypes", "/wellness-service/wellness/dailyStress/2026-01-02"]
    assert len(personal.calls) == 4


def test_writes_calls_and_the_display_name_go_to_connect():
    t, personal, conn = pair()
    t.request("PUT", "/activity-service/activity/21000000001", json={"activityName": "x"})
    t.request("DELETE", "/activity-service/activity/21000000001")
    t.request("POST", "/wellness-service/wellness/epoch/request/2026-01-02")
    assert t.call("add_hydration_data", 250) == {"ok": True, "method": "add_hydration_data"}
    assert [c[0] for c in conn.calls] == ["PUT", "DELETE", "POST", "CALL"] and personal.calls == []
    assert t.display_name == conn.display_name
    assert t.partial is False and t.supports(WELLNESS_FIT_PATH) and t.supports(ACTIVITY_FIT_PATH)
    assert {k: v["state"] for k, v in t.side_status().items()} == {"connect": "ready", "personal": "ready"}


# ----------------------------------------------------------------- the Personal API failing


@pytest.mark.parametrize("status", [500, 503, 401, 403])
def test_personal_failure_falls_back_to_connect(status):
    t, personal, conn = pair()
    personal.statuses = [status]
    r = t.request("GET", "/activity-service/activity/21000000001")
    assert r.status < 400 and paths(conn) == ["/activity-service/activity/21000000001"]
    rejected = status in (401, 403)
    assert t.side_status()["personal"]["state"] == ("failed" if rejected else "ready")
    t.request("GET", "/hrv-service/hrv/2026-01-02")
    # a rejected key takes the Personal side out; a 5xx is retried there next time
    assert len(conn.calls) == (2 if rejected else 1)
    assert t.partial is False


def test_unsupported_and_network_errors_fall_back_to_connect():
    t, personal, conn = pair()
    personal.fail_with = Unsupported("not here")
    assert t.request("GET", "/hrv-service/hrv/2026-01-02").status < 400
    personal.fail_with = ConnectionError("reset by peer")
    assert t.request("GET", "/hrv-service/hrv/2026-01-02").status < 400
    assert len(conn.calls) == 2 and t.side_status()["personal"]["state"] == "ready"


def test_rejected_api_key_falls_back_to_connect(tmp_path):
    s = Session()
    personal = PersonalApiTransport(PersonalToken("bad", tmp_path / "t.json", session=s, clock=lambda: NOW), session=s)
    conn = FakeTransport()
    client = GarminClient(RoutingTransport(personal, conn), background_gap=0, interactive_gap=0, jitter=0, sleep=lambda s: None)
    client.get_text("/activity-service/activity/21000000001")
    assert paths(conn) == ["/activity-service/activity/21000000001"] and s.gets == []
    assert client.transport.side_status()["personal"]["kind"] == "api_key_rejected"
    client.get_text("/hrv-service/hrv/2026-01-02")
    assert len(s.posts) == 1  # the rejected key is not tried again
    assert client.state.auth_error is None


def test_personal_5xx_with_connect_down_is_retried_not_latched():
    t, personal, _ = pair(connect=False)
    personal.statuses = [503] * 10
    client = GarminClient(t, background_gap=0, interactive_gap=0, jitter=0, sleep=lambda s: None, retries=2)
    with pytest.raises(Unavailable):
        client.get_text("/hrv-service/hrv/2026-01-02")
    assert len(personal.calls) == 3 and client.state.auth_error is None


def test_token_endpoint_5xx_is_unavailable_not_a_rejected_key():
    t, personal, _ = pair(connect=False)
    personal.fail_with = AuthError("Garmin's token endpoint answered HTTP 502", 502)
    with pytest.raises(Unavailable):
        t.request("GET", "/hrv-service/hrv/2026-01-02")
    assert t.side_status()["personal"]["state"] == "ready"


def test_both_sides_down_stops_everything_with_both_fixes():
    t, personal, _ = pair(connect=False)
    personal.fail_with = AuthError("Garmin rejected the Personal API key (HTTP 401)", 401)
    client = GarminClient(t, background_gap=0, interactive_gap=0, jitter=0, sleep=lambda s: None)
    with pytest.raises(AuthError):
        client.get_text("/hrv-service/hrv/2026-01-02")
    assert client.state.auth_error and "GARMIN_PERSONAL_API_KEY" in client.state.auth_error_fix and "garmin-mcp-auth" in client.state.auth_error_fix
    with pytest.raises(AuthError):
        client.get_text("/hrv-service/hrv/2026-01-02")


# ----------------------------------------------------------------- Garmin Connect failing


def test_connect_down_refuses_only_what_connect_serves():
    t, personal, _ = pair(connect=False)
    client = GarminClient(t, background_gap=0, interactive_gap=0, jitter=0, sleep=lambda s: None)
    client.get_text("/activity-service/activity/21000000001")
    assert paths(personal) == ["/activity-service/activity/21000000001"]
    with pytest.raises(ConnectUnavailable, match="no_tokens|unknown"):
        client.get_text("/wellness-service/wellness/dailyStress/2026-01-02")
    with pytest.raises(ConnectUnavailable):
        client.call("add_hydration_data", 250)
    with pytest.raises(ConnectUnavailable):
        client.send("PUT", "/activity-service/activity/21000000001", json={})
    assert client.state.auth_error is None
    assert client.display_name == "personal-api"
    assert t.partial is True and t.supports(ACTIVITY_FIT_PATH) and not t.supports(WELLNESS_FIT_PATH)


def test_connect_down_at_login_keeps_the_personal_datasets_and_the_export_going(tmp_path):
    t, _, _ = pair(connect=False)
    db, _, client, engine = make_engine(t)
    engine.schedule()
    drain(engine)
    engine.plan()
    drain(engine)
    kinds = {r["kind"] for r in db.all("SELECT DISTINCT kind FROM sync_log")}
    assert {"list_recent", "act:activity", "activity_fit", "range:daily_summary_v2", "range:hrv_summary"} <= kinds
    assert not {"poll", "wellness_fit", "day:stress", "snap:devices"} & kinds
    assert client.state.auth_error is None
    assert db.scalar("SELECT count(*) FROM activity") == 12
    assert db.scalar("SELECT count(*) FROM raw_file WHERE kind = 'activity_fit'") == 12
    stats = CanonicalExporter(tmp_path / "canonical-v1", importer="garmin-mcp@test").run(SnapshotPublisher(db).publish())
    assert stats["activities"] == 12
    state = login_state(Mirror(db, engine, client))
    assert state["state"] == "degraded" and state["failing"] == ["connect"] and state["sides"]["personal"] == {"state": "ready"}


def test_connect_auth_error_mid_session_drops_connect_jobs_and_keeps_syncing():
    t, personal, conn = pair()
    db, _, client, engine = make_engine(t)
    engine.schedule()  # plans as under connect: profile, devices, poll, snapshots
    queued = {r["kind"] for r in db.all("SELECT DISTINCT kind FROM sync_job")}
    assert "poll" in queued and any(k.startswith("snap:") and k != "snap:userprofile_settings" for k in queued)
    conn.fail_with = AuthError("API Error 401", 401)
    drain(engine)
    engine.plan()
    drain(engine)
    assert client.state.auth_error is None
    assert len(conn.calls) == 1  # the one request that found the session rejected
    assert t.side_status()["connect"]["kind"] == "expired" and t.partial
    left = {r["kind"] for r in db.all("SELECT DISTINCT kind FROM sync_job")}
    assert all(job_path(k) is None or t.supports(job_path(k)) for k in left)
    outcomes = {r["outcome"] for r in db.all("SELECT outcome FROM sync_log")}
    assert "unsupported" in outcomes and "auth_error" not in outcomes
    assert db.scalar("SELECT count(*) FROM activity") == 12
    assert db.scalar("SELECT count(*) FROM raw_file WHERE kind = 'activity_fit'") == 12
    assert len(personal.calls) > 30


def test_mirror_serves_stale_when_connect_is_down(landed_db):
    from garmin_mcp.mirror import can_serve_stale

    assert can_serve_stale(ConnectUnavailable("x")) and not can_serve_stale(Unsupported("x"))


# ----------------------------------------------------------------- the login


@pytest.fixture
def personal_api(monkeypatch):
    import garmin_mcp.garmin.personal as p

    def post(url, headers=None, timeout=None):
        return Resp(401) if headers["Authorization"] == "ApiKey bad" else Resp(200, jwt(time.time() + 86400))

    monkeypatch.setattr(p, "requests", type("R", (), {"post": staticmethod(post), "get": None}))


def test_hybrid_login_with_connect_tokens_missing_keeps_the_personal_side(tmp_path, personal_api):
    client = GarminClient(PendingTransport())
    login = HybridLogin(client, str(tmp_path), key="key1")
    login.start()
    login.wait(10)
    assert isinstance(client.transport, RoutingTransport) and login.state == "ready"
    assert client.state.auth_error is None
    sides = client.transport.side_status()
    assert sides["personal"] == {"state": "ready"} and sides["connect"]["kind"] == "no_tokens" and "garmin-mcp-auth" in sides["connect"]["fix"]


def test_hybrid_login_with_both_sides_failing_stops_like_the_other_modes(tmp_path, personal_api):
    client = GarminClient(PendingTransport())
    login = HybridLogin(client, str(tmp_path), key="bad")
    login.start()
    login.wait(10)
    assert login.state == "failed" and isinstance(client.transport, PendingTransport)
    assert client.state.auth_error_kind == "no_tokens"
    assert "Garmin Connect:" in client.state.auth_error and "Personal API:" in client.state.auth_error
    assert "garmin-mcp-auth" in client.state.auth_error_fix and "GARMIN_PERSONAL_API_KEY" in client.state.auth_error_fix


# ----------------------------------------------------------------- settings and the runtime


def test_settings_accept_hybrid_with_a_key():
    validate_settings({"GARMIN_TRANSPORT": "hybrid", "GARMIN_PERSONAL_API_KEY": "k"})
    with pytest.raises(ValueError, match="GARMIN_TRANSPORT=hybrid needs GARMIN_PERSONAL_API_KEY"):
        validate_settings({"GARMIN_TRANSPORT": "hybrid"})
    with pytest.raises(ValueError, match='"connect", "personal" or "hybrid"'):
        validate_settings({"GARMIN_TRANSPORT": "both"})
    with pytest.raises(ValueError, match="GARMIN_TRANSPORT=personal needs GARMIN_PERSONAL_API_KEY"):
        validate_settings({"GARMIN_TRANSPORT": "personal"})
    validate_settings({})


def runtime(tmp_path, **env):
    return open_runtime({"GARMIN_DB_PATH": str(tmp_path / "garmin.duckdb"), "GARMIN_SYNC": "off", **env}, start=False)


def test_connect_and_personal_modes_construct_what_they_did(tmp_path):
    rt = runtime(tmp_path, GARMIN_EMAIL="a@example.com")
    try:
        assert type(rt.login) is LoginManager and rt.login.email == "a@example.com" and rt.login.cn is False
        assert isinstance(rt.client.transport, PendingTransport)
        assert health_body(rt) == {"status": "ok", "name": health_body(rt)["name"], "version": health_body(rt)["version"], "sync": "off", "transport": "connect", "strava_sync": "off"}
    finally:
        rt.close()
    rt = runtime(tmp_path, GARMIN_TRANSPORT="personal", GARMIN_PERSONAL_API_KEY="k")
    try:
        assert type(rt.login) is PersonalLogin and rt.login.key == "k"
        assert health_body(rt)["transport"] == "connect"  # computed once at startup, before the login finished
    finally:
        rt.close()


def test_hybrid_runtime_and_health(tmp_path):
    rt = runtime(tmp_path, GARMIN_TRANSPORT="hybrid", GARMIN_PERSONAL_API_KEY="k", GARMIN_EMAIL="a@example.com", GARMIN_IS_CN="true")
    try:
        assert type(rt.login) is HybridLogin and rt.login.personal.key == "k" and rt.login.email == "a@example.com" and rt.login.cn is True
        assert health_body(rt)["transport"] == "hybrid" and health_body(rt)["garmin"] == {"connect": "pending", "personal": "pending"}
        rt.client.transport = RoutingTransport(PersonalFake(), None)
        assert health_body(rt)["garmin"] == {"connect": "failed", "personal": "ready"}
        from mcp.server.mcpserver import MCPServer

        from garmin_mcp.http import build_app

        app = build_app(MCPServer("t"), snapshot=None, health=lambda: health_body(rt))

        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
                return (await c.get("/health")).json()

        assert asyncio.run(go())["garmin"]["connect"] == "failed"
    finally:
        rt.close()

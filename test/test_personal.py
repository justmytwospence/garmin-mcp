"""The Personal API transport: token exchange and cache, retry on 401, read-only, and the partial sync plan."""

from __future__ import annotations

import base64
import json
import os
import stat

import pytest

from garmin_mcp.garmin.auth import PersonalLogin
from garmin_mcp.garmin.client import AuthError, GarminClient, Unsupported
from garmin_mcp.garmin.personal import PersonalApiTransport, PersonalToken
from test_engine import make_engine

from fake import FakeTransport

NOW = 1_800_000_000.0


def jwt(exp: float) -> str:
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{b({'alg': 'none'})}.{b({'exp': exp})}.sig"


class Resp:
    def __init__(self, status=200, text="", content=b"", headers=None):
        self.status_code, self.text, self.content, self.headers = status, text, content or text.encode(), headers or {}


class Session:
    def __init__(self):
        self.posts, self.gets, self.token_exp, self.data_statuses = [], [], NOW + 86400, []

    def post(self, url, headers=None, timeout=None):
        self.posts.append((url, headers))
        if headers["Authorization"] == "ApiKey bad":
            return Resp(401)
        return Resp(200, jwt(self.token_exp) + "\n")

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append((url, params, headers))
        status = self.data_statuses.pop(0) if self.data_statuses else 200
        return Resp(status, "[]", b"[]", {"content-type": "application/json"})


@pytest.fixture
def token(tmp_path):
    s = Session()
    return PersonalToken("key1", tmp_path / "t.json", session=s, clock=lambda: NOW), s


def test_exchange_sends_api_key_and_caches_private(token, tmp_path):
    t, s = token
    assert t.get().count(".") == 2
    assert s.posts[0][1]["Authorization"] == "ApiKey key1"
    assert stat.S_IMODE(os.stat(tmp_path / "t.json").st_mode) == 0o600
    t.get()
    assert len(s.posts) == 1


def test_cache_refreshed_under_one_hour(token, tmp_path):
    t, s = token
    s.token_exp = NOW + 1800
    t.get()
    t.get()
    assert len(s.posts) == 2


def test_other_key_does_not_reuse_cache(token, tmp_path):
    t, s = token
    t.get()
    PersonalToken("key2", tmp_path / "t.json", session=s, clock=lambda: NOW).get()
    assert len(s.posts) == 2


def test_rejected_key(tmp_path):
    with pytest.raises(AuthError) as e:
        PersonalToken("bad", tmp_path / "t.json", session=Session(), clock=lambda: NOW).get()
    assert e.value.status == 401


def test_401_refreshes_once_and_retries(token):
    t, s = token
    tr = PersonalApiTransport(t, session=s)
    s.data_statuses = [401, 200]
    assert tr.request("GET", "/x").status == 200
    assert len(s.posts) == 2
    s.data_statuses = [401, 401]
    assert tr.request("GET", "/x").status == 401


def test_binary_accept_and_read_only(token):
    t, s = token
    tr = PersonalApiTransport(t, session=s)
    tr.request("GET", "/f", binary=True)
    assert "application/zip" in s.gets[-1][2]["Accept"]
    with pytest.raises(Unsupported):
        tr.request("POST", "/x")
    with pytest.raises(Unsupported):
        tr.call("add_hydration_data")


def test_login_with_rejected_key_sets_problem(tmp_path, monkeypatch):
    import garmin_mcp.garmin.personal as p

    monkeypatch.setattr(p, "requests", type("R", (), {"post": staticmethod(lambda *a, **k: Resp(401)), "get": None}))
    client = GarminClient(FakeTransport())
    login = PersonalLogin(client, str(tmp_path), key="bad")
    assert login.attempt() is None
    assert login.problem.kind == "api_key_rejected"


class PartialFake(FakeTransport):
    partial = True

    def supports(self, path):
        from garmin_mcp.garmin.personal import SUPPORTED_PATHS

        return path in SUPPORTED_PATHS


def test_partial_transport_plans_only_supported_datasets():
    db, transport, client, engine = make_engine(PartialFake())
    engine.schedule()
    while engine.step() == "worked":
        pass
    engine.plan()
    while engine.step() == "worked":
        pass
    kinds = {r["kind"] for r in db.all("SELECT DISTINCT kind FROM sync_log")}
    assert "activity_fit" in kinds and "act:activity" in kinds and "range:daily_summary_v2" in kinds
    for bad in ("act:activity_splits", "day:stress", "wellness_fit", "poll"):
        assert bad not in kinds
    assert not any(k.startswith("snap:") and k != "snap:userprofile_settings" for k in kinds)
    assert db.scalar("SELECT count(*) FROM user_profile") == 0
    assert db.scalar("SELECT count(*) FROM activity") == 12
    assert db.scalar("SELECT count(*) FROM raw_file WHERE kind = 'activity_fit'") == 12

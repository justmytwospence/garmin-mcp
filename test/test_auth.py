"""Login failures are explained, with the command that fixes them, without ever calling
Garmin: python-garminconnect is replaced by fakes raising what the library raises.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
from garminconnect import GarminConnectAuthenticationError, GarminConnectConnectionError, GarminConnectTooManyRequestsError

from garmin_mcp import auth_cli
from garmin_mcp.garmin import auth
from garmin_mcp.garmin.auth import AUTH_COMMAND, LoginManager, classify_login_error, region_mismatch, write_marker
from garmin_mcp.garmin.client import AuthError, GarminClient, Unavailable


def tokens(tmp_path: Path, content: str = '{"di_token": "x", "di_refresh_token": "y", "di_client_id": "z"}') -> str:
    d = tmp_path / "tokens"
    d.mkdir(exist_ok=True)
    (d / "garmin_tokens.json").write_text(content)
    return str(d)


# ----------------------------------------------------------------- classification


@pytest.mark.parametrize(
    ("err", "kind", "needle"),
    [
        (GarminConnectTooManyRequestsError("Too many login attempts. Please wait a few minutes before trying again."), "rate_limited", "Wait at least 30 minutes"),
        (GarminConnectTooManyRequestsError("All login strategies rate limited (429). Garmin may be blocking this network"), "rate_limited", "never retries logins"),
        (GarminConnectAuthenticationError("401 Unauthorized (Invalid Username or Password)"), "wrong_password", "GARMIN_IS_CN=true"),
        (GarminConnectAuthenticationError("Authentication failed (401 Unauthorized). Possible causes:\n  Incorrect email or password\n  Account locked"), "wrong_password", "connect.garmin.com"),
        (GarminConnectAuthenticationError("MFA Required but no prompt_mfa mechanism supplied"), "mfa_required", AUTH_COMMAND),
        (AuthError("Garmin asks for an MFA code; run garmin-mcp-auth interactively to log in"), "mfa_required", "MFA"),
        (GarminConnectAuthenticationError("DI token refresh failed: 401"), "expired", "expired, were revoked"),
        (GarminConnectAuthenticationError("Authentication failed: 403 Forbidden"), "expired", "403"),
        (AuthError("Login from stored tokens did not load the profile (display name missing)"), "expired", AUTH_COMMAND),
        (GarminConnectConnectionError("Connection error: HTTPSConnectionPool(host='connect.garmin.com'): Max retries exceeded"), "unreachable", "--check"),
        (GarminConnectConnectionError("Mobile login: HTTP 403 (Cloudflare bot challenge)"), "unreachable", "bot protection"),
        (ValueError("something else entirely"), "unknown", AUTH_COMMAND),
    ],
)
def test_login_errors_are_classified_with_a_fix(tmp_path, err, kind, needle):
    problem = classify_login_error(err, tokenstore=tokens(tmp_path))
    assert problem.kind == kind, problem
    assert needle in problem.text(), problem.text()
    assert "garmin-mcp-auth" in problem.text()


def test_missing_tokens_are_explained(tmp_path):
    store = str(tmp_path / "empty")
    p = classify_login_error(GarminConnectAuthenticationError("Username and password are required"), tokenstore=store)
    assert p.kind == "no_tokens" and "garmin_tokens.json" in p.message and AUTH_COMMAND in p.fix
    assert classify_login_error(FileNotFoundError("x"), tokenstore=store).kind == "no_tokens"


def test_legacy_garth_tokens_are_explained(tmp_path):
    d = tmp_path / "old"
    d.mkdir()
    (d / "oauth1_token.json").write_text("{}")
    p = classify_login_error(GarminConnectAuthenticationError("Username and password are required"), tokenstore=str(d))
    assert p.kind == "legacy_tokens" and "oauth1_token.json" in p.message


def test_rejected_mfa_code_after_a_prompt(tmp_path):
    p = classify_login_error(GarminConnectAuthenticationError("Widget MFA failed: Invalid verification code"), tokenstore=tokens(tmp_path), mfa_prompted=True)
    assert p.kind == "mfa_rejected" and "newest code" in p.fix


def test_china_region_hint_and_marker(tmp_path):
    store = tokens(tmp_path)
    p = classify_login_error(GarminConnectAuthenticationError("401 Unauthorized (Invalid Username or Password)"), tokenstore=store, cn=True)
    assert "garmin.cn" in p.text() and "GARMIN_IS_CN is set" in p.message
    assert region_mismatch(store, cn=False) is None
    write_marker(store, cn=True, display_name="dn")
    assert json.loads((Path(store) / "garmin-mcp-auth.json").read_text())["domain"] == "garmin.cn"
    assert region_mismatch(store, cn=True) is None
    mismatch = region_mismatch(store, cn=False)
    assert mismatch is not None and mismatch.kind == "region_mismatch" and "GARMIN_IS_CN=false selects garmin.com" in mismatch.message


# ----------------------------------------------------------------- the server's login thread


class FakeGarmin:
    """Stands in for garminconnect.Garmin: `outcome` is an exception to raise or a display name."""

    outcome: object = "fixture-name"
    calls: list[tuple] = []

    def __init__(self, email=None, password=None, is_cn=False, prompt_mfa=None, return_on_mfa=False):
        self.is_cn = is_cn
        self.display_name = None
        self.client = types.SimpleNamespace(login=self._client_login, dump=lambda path: None)

    def login(self, tokenstore=None):
        FakeGarmin.calls.append(("login", tokenstore, self.is_cn))
        if isinstance(FakeGarmin.outcome, BaseException):
            raise FakeGarmin.outcome
        self.display_name = FakeGarmin.outcome
        return None, None

    def _client_login(self, email, password, return_on_mfa=False):
        FakeGarmin.calls.append(("credentials", email))
        if isinstance(FakeGarmin.outcome, BaseException):
            raise FakeGarmin.outcome
        return (FakeGarmin.outcome, None) if FakeGarmin.outcome == "needs_mfa" else (None, None)


@pytest.fixture
def fake_garmin(monkeypatch):
    import garminconnect

    FakeGarmin.calls = []
    FakeGarmin.outcome = "fixture-name"
    monkeypatch.setattr(garminconnect, "Garmin", FakeGarmin)
    return FakeGarmin


def manager(store: str, **kw) -> tuple[GarminClient, LoginManager]:
    client = GarminClient(auth.PendingTransport(), sleep=lambda s: None)
    return client, LoginManager(client, store, **kw)


def test_server_login_from_tokens_succeeds(tmp_path, fake_garmin):
    client, m = manager(tokens(tmp_path))
    m.start()
    m.wait(5)
    assert m.state == "ready" and client.transport.display_name == "fixture-name"


def test_server_without_tokens_reports_no_tokens_and_the_command(tmp_path, fake_garmin):
    client, m = manager(str(tmp_path / "none"))
    m.start()
    m.wait(5)
    assert m.state == "failed" and m.problem.kind == "no_tokens"
    assert client.state.auth_error_kind == "no_tokens" and AUTH_COMMAND in client.state.auth_error
    assert fake_garmin.calls == []  # nothing was sent to Garmin
    with pytest.raises(AuthError) as info:
        client.get_text("/x", priority="interactive")
    assert "garmin_tokens.json is missing" in str(info.value)


@pytest.mark.parametrize(
    ("err", "kind"),
    [
        (GarminConnectAuthenticationError("DI token refresh failed: 401"), "expired"),
        (GarminConnectTooManyRequestsError("Too many login attempts"), "rate_limited"),
        (GarminConnectConnectionError("Connection error: timed out"), "unreachable"),
    ],
)
def test_server_token_failures_are_classified(tmp_path, fake_garmin, err, kind):
    fake_garmin.outcome = err
    client, m = manager(tokens(tmp_path))
    m.start()
    m.wait(5)
    assert m.state == "failed" and m.problem.kind == kind
    assert client.state.auth_error_kind == kind and client.state.auth_error_fix


def test_server_credential_fallback_stops_at_mfa(tmp_path, fake_garmin):
    fake_garmin.outcome = GarminConnectAuthenticationError("DI token refresh failed: 401")
    client, m = manager(tokens(tmp_path), email="e@example.com", password="pw")
    m.start()
    m.wait(5)
    assert m.state == "failed" and m.problem.kind == "expired"
    assert ("credentials", "e@example.com") in fake_garmin.calls


def test_server_does_not_try_credentials_while_rate_limited(tmp_path, fake_garmin):
    fake_garmin.outcome = GarminConnectTooManyRequestsError("Too many login attempts")
    client, m = manager(tokens(tmp_path), email="e@example.com", password="pw")
    m.start()
    m.wait(5)
    assert m.problem.kind == "rate_limited"
    assert not any(c[0] == "credentials" for c in fake_garmin.calls)


def test_server_region_mismatch_is_reported_before_calling_garmin(tmp_path, fake_garmin):
    store = tokens(tmp_path)
    write_marker(store, cn=True, display_name=None)
    client, m = manager(store, cn=False)
    m.start()
    m.wait(5)
    assert m.problem.kind == "region_mismatch" and fake_garmin.calls == []


def test_sync_status_login_block_carries_kind_and_fix(tmp_path, fake_garmin):
    from garmin_mcp.tools.mirror_tools import login_state

    client, m = manager(str(tmp_path / "none"))
    m.start()
    m.wait(5)
    state = login_state(types.SimpleNamespace(client=client))
    assert state["state"] == "failed" and state["kind"] == "no_tokens" and AUTH_COMMAND in state["fix"] and "stale" in state["effect"]


# ----------------------------------------------------------------- garmin-mcp-auth


def test_auth_check_without_tokens(tmp_path, fake_garmin, capsys):
    store = str(tmp_path / "none")
    assert auth_cli.main(["--check", "--tokens", store]) == 1
    err = capsys.readouterr().err
    assert "no_tokens" in err and AUTH_COMMAND in err and fake_garmin.calls == []


def test_auth_check_with_working_tokens(tmp_path, fake_garmin, capsys):
    store = tokens(tmp_path)
    assert auth_cli.main(["--check", "--tokens", store]) == 0
    assert "work on garmin.com" in capsys.readouterr().err
    assert auth_cli.main(["--tokens", store]) == 0  # no re-login without --force-reauth
    assert "--force-reauth" in capsys.readouterr().err


def test_auth_check_explains_expired_tokens(tmp_path, fake_garmin, capsys):
    fake_garmin.outcome = GarminConnectAuthenticationError("DI token refresh failed: 401")
    assert auth_cli.main(["--check", "--tokens", tokens(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "expired" in err and AUTH_COMMAND in err


def test_auth_login_needs_a_terminal(tmp_path, fake_garmin, monkeypatch, capsys):
    monkeypatch.delenv("GARMIN_EMAIL", raising=False)
    monkeypatch.delenv("GARMIN_PASSWORD", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert auth_cli.main(["--tokens", str(tmp_path / "none")]) == 2
    assert "No terminal attached" in capsys.readouterr().err


def test_auth_login_prompts_mfa_and_saves_marker(tmp_path, fake_garmin, monkeypatch, capsys):
    store = str(tmp_path / "new")
    answers = iter(["e@example.com", "123456"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(auth_cli.getpass, "getpass", lambda prompt="": "pw")

    class MfaGarmin(FakeGarmin):
        def login(self, tokenstore=None):
            code = self.prompt_mfa()
            assert code == "123456"
            self.display_name = "dn"
            Path(tokenstore).mkdir(parents=True, exist_ok=True)
            (Path(tokenstore) / "garmin_tokens.json").write_text("{}")
            return None, None

        def __init__(self, email=None, password=None, is_cn=False, prompt_mfa=None, return_on_mfa=False):
            super().__init__(email, password, is_cn, prompt_mfa, return_on_mfa)
            self.prompt_mfa = prompt_mfa

    import garminconnect

    monkeypatch.setattr(garminconnect, "Garmin", MfaGarmin)
    assert auth_cli.main(["--tokens", store]) == 0
    err = capsys.readouterr().err
    assert "multi-factor code" in err and "Logged in as dn" in err and "docker compose restart garmin-mcp" in err
    assert json.loads((Path(store) / "garmin-mcp-auth.json").read_text())["domain"] == "garmin.com"


@pytest.mark.parametrize(
    ("err", "kind", "needle"),
    [
        (GarminConnectAuthenticationError("401 Unauthorized (Invalid Username or Password)"), "wrong_password", "GARMIN_IS_CN=true"),
        (GarminConnectTooManyRequestsError("Too many login attempts. Please wait a few minutes before trying again."), "rate_limited", "Wait at least 30 minutes"),
        (GarminConnectAuthenticationError("Widget MFA failed: Invalid verification code"), "mfa_rejected", "newest code"),
    ],
)
def test_auth_login_failures_are_explained(tmp_path, fake_garmin, monkeypatch, capsys, err, kind, needle):
    store = str(tmp_path / "new")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "e@example.com" if "email" in prompt else "000000")
    monkeypatch.setattr(auth_cli.getpass, "getpass", lambda prompt="": "pw")

    class FailingGarmin(FakeGarmin):
        def __init__(self, email=None, password=None, is_cn=False, prompt_mfa=None, return_on_mfa=False):
            super().__init__(email, password, is_cn, prompt_mfa, return_on_mfa)
            self.prompt_mfa = prompt_mfa

        def login(self, tokenstore=None):
            if kind == "mfa_rejected":
                self.prompt_mfa()
            raise err

    import garminconnect

    monkeypatch.setattr(garminconnect, "Garmin", FailingGarmin)
    assert auth_cli.main(["--tokens", store]) == 1
    out = capsys.readouterr().err
    assert f"Problem ({kind})" in out and needle in out


def test_garmin_mcp_entrypoint_dispatches_to_auth(tmp_path, fake_garmin, capsys):
    from garmin_mcp.cli import main

    assert main(["garmin-mcp-auth", "--check", "--tokens", str(tmp_path / "none")]) == 1
    assert main(["auth", "--check", "--tokens", tokens(tmp_path)]) == 0


def test_tool_error_text_for_auth_failures():
    from garmin_mcp.tools.base import error_text

    assert "garmin-mcp-auth" in error_text(AuthError("Not authenticated: x"))
    assert error_text(Unavailable("boom")).startswith("Garmin Connect unreachable")

"""Logging in: stored tokens first, a one-time credential login as a fallback.

The server never logs in on a schedule. Tokens (python-garminconnect's garmin_tokens.json,
DI format, refreshed in place by the library) are loaded from GARMINTOKENS; if that fails
and GARMIN_EMAIL/GARMIN_PASSWORD are set, one credential login is attempted, but only when
Garmin does not ask for MFA. Anything else is reported and the server keeps serving the
mirror with background sync stopped until a person runs garmin-mcp-auth.

Every failure is classified (``classify_login_error``) into a short kind with a plain
explanation and the exact command that fixes it, shown by garmin-mcp-auth, in the server
log and in garmin_sync_status.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .client import AUTH_COMMAND, AUTH_COMMAND_LOCAL, RESTART_COMMAND, AuthError, GarminConnectTransport, Response, Transport, Unavailable, auth_fix

log = logging.getLogger("garmin_mcp.auth")

DEFAULT_TOKEN_DIR = "~/.garminconnect"
TOKEN_FILE = "garmin_tokens.json"
# Written next to the tokens by garmin-mcp-auth: which Garmin domain they belong to.
MARKER_FILE = "garmin-mcp-auth.json"


def token_dir(env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return os.path.expanduser(env.get("GARMINTOKENS") or DEFAULT_TOKEN_DIR)


def is_cn(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return (env.get("GARMIN_IS_CN") or "false").lower() in ("1", "true", "yes")


def domain(cn: bool) -> str:
    return "garmin.cn" if cn else "garmin.com"


def token_file(tokenstore: str) -> Path:
    return Path(tokenstore) / TOKEN_FILE


def has_tokens(tokenstore: str) -> bool:
    return token_file(tokenstore).is_file()


def has_legacy_tokens(tokenstore: str) -> bool:
    p = Path(tokenstore)
    return (p / "oauth1_token.json").exists() or (p / "oauth2_token.json").exists()


def read_marker(tokenstore: str) -> dict[str, Any] | None:
    try:
        return json.loads((Path(tokenstore) / MARKER_FILE).read_text())
    except (OSError, ValueError):
        return None


def write_marker(tokenstore: str, cn: bool, display_name: str | None) -> None:
    """Record which domain the tokens belong to, so a GARMIN_IS_CN mismatch is explained."""
    p = Path(tokenstore) / MARKER_FILE
    p.write_text(json.dumps({"domain": domain(cn), "display_name": display_name, "created_at": datetime.now(UTC).isoformat(timespec="seconds")}, indent=2) + "\n")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


# ----------------------------------------------------------------- failure classification


@dataclass(frozen=True)
class LoginProblem:
    """What went wrong (kind, a sentence) and what to do about it (fix, with the command)."""

    kind: str
    message: str
    fix: str

    def text(self) -> str:
        return f"{self.message} {self.fix}"


def classify_login_error(err: BaseException, *, tokenstore: str, cn: bool = False, mfa_prompted: bool = False) -> LoginProblem:
    """Turn a python-garminconnect (or transport) error into a LoginProblem.

    Works on the message text, since the library signals most conditions through
    two exception classes; the tests cover each phrase.
    """
    text = str(err)
    low = text.lower()
    name = type(err).__name__
    where = f"in {tokenstore}"
    region = f" If your account is on Garmin Connect China, set GARMIN_IS_CN=true for both the server and {AUTH_COMMAND_LOCAL}; tokens of one region do not work on the other." if not cn else " GARMIN_IS_CN is set: tokens and credentials must belong to a garmin.cn account."

    if name in ("GarminConnectTooManyRequestsError",) or "429" in low or "too many" in low or "rate limit" in low:
        return LoginProblem(
            "rate_limited",
            "Garmin is rate limiting logins from this network right now (HTTP 429).",
            f"Wait at least 30 minutes (longer after repeated attempts, or try another network), then run `{AUTH_COMMAND}`. The server never retries logins by itself, so it will not make this worse.",
        )
    if "garth" in low or "oauth1_token" in low or has_legacy_tokens(tokenstore):
        return LoginProblem(
            "legacy_tokens",
            f"The token directory {where} holds tokens from an older python-garminconnect (oauth1_token.json/oauth2_token.json), which this version cannot use.",
            auth_fix(),
        )
    if "mfa required but no prompt" in low or "needs_mfa" in low or "asks for an mfa code" in low or ("mfa" in low and not mfa_prompted):
        return LoginProblem(
            "mfa_required",
            "Garmin requires a multi-factor (MFA) code for this login, which the server cannot answer by itself.",
            auth_fix(),
        )
    if mfa_prompted and ("mfa" in low or "verify" in low or "service ticket" in low or "csrf" in low):
        return LoginProblem(
            "mfa_rejected",
            "Garmin did not accept the MFA code (it may have expired, or a stale code from an earlier email was used).",
            f"Run `{AUTH_COMMAND}` again and enter the newest code within a minute of receiving it.",
        )
    if "invalid username or password" in low or "incorrect email or password" in low or ("401" in low and ("password" in low or "username" in low)):
        return LoginProblem(
            "wrong_password",
            "Garmin rejected the email or password (HTTP 401)." + region,
            f"Check the credentials at https://connect.{domain(cn)} (an account can also be locked there), then run `{AUTH_COMMAND}` again.",
        )
    no_tokens = LoginProblem("no_tokens", f"No usable Garmin tokens {where} ({TOKEN_FILE} is missing or unreadable).", auth_fix())
    if "username and password are required" in low or "missing tokens" in low or "token path not loading" in low or isinstance(err, FileNotFoundError):
        return no_tokens
    unreachable = LoginProblem(
        "unreachable",
        f"Garmin Connect could not be reached or refused the connection ({text[:160]}).",
        f"Check the network and https://connect.{domain(cn)} from this host, wait a while (Garmin's bot protection blocks bursts and some hosting providers), then run `{AUTH_COMMAND} --check`.",
    )
    if "cloudflare" in low or "challenge" in low or "connection error" in low or "timed out" in low or "name resolution" in low or "all login strategies" in low:
        return unreachable
    if "refresh failed" in low or "401" in low or "unauthorized" in low or "403" in low or "authentication failed" in low or "rejected" in low or "display name missing" in low or isinstance(err, AuthError):
        status = "403" if "403" in low else "401"
        if has_tokens(tokenstore):
            return LoginProblem(
                "expired",
                f"The stored tokens {where} were rejected by Garmin: they expired, were revoked (a password change, a sign-out of all devices, too many sessions), or could not be refreshed (HTTP {status})." + region,
                auth_fix(),
            )
        return LoginProblem("expired", f"Garmin rejected the login (HTTP {status}: {text[:160]})." + region, auth_fix())
    if name in ("GarminConnectConnectionError", "ConnectionError", "Unavailable") or "connection" in low or "timeout" in low or "dns" in low:
        return unreachable
    if not has_tokens(tokenstore):
        return no_tokens
    return LoginProblem("unknown", f"Login failed: {name}: {text[:300]}", auth_fix())


def region_mismatch(tokenstore: str, cn: bool) -> LoginProblem | None:
    """A problem when the stored tokens were created for the other Garmin domain."""
    marker = read_marker(tokenstore)
    if not marker or marker.get("domain") not in ("garmin.com", "garmin.cn") or marker["domain"] == domain(cn):
        return None
    wanted = domain(cn)
    return LoginProblem(
        "region_mismatch",
        f"The tokens in {tokenstore} were created for {marker['domain']} but GARMIN_IS_CN={'true' if cn else 'false'} selects {wanted}.",
        f"Set GARMIN_IS_CN to match the account's region for both the server and {AUTH_COMMAND_LOCAL} (true for Garmin Connect China), restart, or run `{AUTH_COMMAND}` to log in on {wanted}.",
    )


# ----------------------------------------------------------------- logins


def login_from_tokens(tokenstore: str, cn: bool = False) -> GarminConnectTransport:
    from garminconnect import Garmin

    if not has_tokens(tokenstore):
        raise FileNotFoundError(f"{token_file(tokenstore)} does not exist")
    api = Garmin(is_cn=cn)
    api.login(tokenstore)
    if not getattr(api, "display_name", None):
        raise AuthError("Login from stored tokens did not load the profile (display name missing)")
    return GarminConnectTransport(api)


def first_login(email: str, password: str, tokenstore: str, cn: bool = False) -> GarminConnectTransport:
    """One credential login without MFA; saves tokens to `tokenstore`."""
    from garminconnect import Garmin

    api = Garmin(email=email, password=password, is_cn=cn, return_on_mfa=True)
    status, _ = api.client.login(email, password, return_on_mfa=True)
    if status == "needs_mfa":
        raise AuthError("Garmin asks for an MFA code; run garmin-mcp-auth interactively to log in")
    Path(tokenstore).mkdir(parents=True, exist_ok=True)
    api.client.dump(tokenstore)
    transport = login_from_tokens(tokenstore, cn)
    write_marker(tokenstore, cn, transport.display_name)
    return transport


class PendingTransport:
    """Stands in until the background login finishes, or when it failed."""

    def __init__(self, reason: str = "login in progress") -> None:
        self.display_name: str | None = None
        self.reason = reason
        self.failed = False

    def request(self, method: str, path: str, **_: Any) -> Response:
        if self.failed:
            raise AuthError(self.reason, 401)
        raise Unavailable(f"Garmin Connect {self.reason}")

    def call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        return self.request("CALL", method_name)


class LoginManager:
    """Logs in on a background thread and hands the transport to the client when ready."""

    def __init__(self, client: Any, tokenstore: str, *, email: str | None = None, password: str | None = None, cn: bool = False) -> None:
        self.client = client
        self.tokenstore = tokenstore
        self.email = email
        self.password = password
        self.cn = cn
        self.pending = PendingTransport()
        self.state = "pending"
        self.error: str | None = None
        self.problem: LoginProblem | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.client.transport = self.pending
        self._thread = threading.Thread(target=self._run, name="garmin-login", daemon=True)
        self._thread.start()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def attempt(self) -> Transport | None:
        """One login attempt: tokens, then credentials. Sets self.problem on failure."""
        mismatch = region_mismatch(self.tokenstore, self.cn)
        if mismatch is not None:
            self.problem = mismatch
            return None
        try:
            transport = login_from_tokens(self.tokenstore, self.cn)
            log.info("logged in to Garmin Connect (%s) from tokens in %s", domain(self.cn), self.tokenstore)
            return transport
        except Exception as err:  # noqa: BLE001
            problem = classify_login_error(err, tokenstore=self.tokenstore, cn=self.cn)
            if not (self.email and self.password):
                self.problem = problem
                return None
            if problem.kind in ("rate_limited", "legacy_tokens", "unreachable"):
                # Credentials would not help, and a login attempt now could lock the account or extend the block.
                self.problem = problem
                return None
            log.warning("login from tokens in %s failed (%s); trying GARMIN_EMAIL/GARMIN_PASSWORD once", self.tokenstore, problem.kind)
        try:
            transport = first_login(self.email, self.password, self.tokenstore, self.cn)  # type: ignore[arg-type]
            log.info("logged in to Garmin Connect (%s) with credentials; tokens saved to %s", domain(self.cn), self.tokenstore)
            return transport
        except Exception as err2:  # noqa: BLE001
            self.problem = classify_login_error(err2, tokenstore=self.tokenstore, cn=self.cn)
            return None

    def _run(self) -> None:
        transport = self.attempt()
        if transport is None:
            problem = self.problem or LoginProblem("unknown", "Login failed.", auth_fix())
            self.state = "failed"
            self.error = problem.text()
            self.pending.failed = True
            self.pending.reason = self.error
            with self.client.state.lock:
                self.client.state.auth_error = self.error
                self.client.state.auth_error_kind = problem.kind
                self.client.state.auth_error_fix = problem.fix
                self.client.state.auth_error_at = datetime.now(UTC)
            log.error("Garmin login failed (%s): %s Background sync is stopped; tools keep serving the mirror.", problem.kind, self.error)
            return
        self.client.transport = transport
        self.state = "ready"

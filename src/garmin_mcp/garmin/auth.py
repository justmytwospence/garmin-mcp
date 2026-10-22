"""Logging in: stored tokens first, a one-time credential login as a fallback.

The server never logs in on a schedule. Tokens (python-garminconnect's garmin_tokens.json,
DI format, refreshed in place by the library) are loaded from GARMINTOKENS; if that fails
and GARMIN_EMAIL/GARMIN_PASSWORD are set, one credential login is attempted, but only when
Garmin does not ask for MFA. Anything else is reported and the server keeps serving the
mirror with background sync stopped until a person runs garmin-mcp-auth.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from .client import AuthError, GarminConnectTransport, Response, Transport, Unavailable

log = logging.getLogger("garmin_mcp.auth")

DEFAULT_TOKEN_DIR = "~/.garminconnect"


def token_dir(env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return os.path.expanduser(env.get("GARMINTOKENS") or DEFAULT_TOKEN_DIR)


def is_cn(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return (env.get("GARMIN_IS_CN") or "false").lower() in ("1", "true", "yes")


def login_from_tokens(tokenstore: str, cn: bool = False) -> GarminConnectTransport:
    from garminconnect import Garmin

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
    api.client.dump(tokenstore)
    return login_from_tokens(tokenstore, cn)


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
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.client.transport = self.pending
        self._thread = threading.Thread(target=self._run, name="garmin-login", daemon=True)
        self._thread.start()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        transport: Transport | None = None
        try:
            transport = login_from_tokens(self.tokenstore, self.cn)
            log.info("logged in to Garmin Connect from tokens in %s", self.tokenstore)
        except Exception as err:  # noqa: BLE001
            log.warning("login from tokens in %s failed: %s", self.tokenstore, str(err)[:300])
            if self.email and self.password:
                try:
                    transport = first_login(self.email, self.password, self.tokenstore, self.cn)
                    log.info("logged in to Garmin Connect with credentials; tokens saved to %s", self.tokenstore)
                except Exception as err2:  # noqa: BLE001
                    self.error = f"token login failed ({str(err)[:160]}); credential login failed ({str(err2)[:160]})"
            else:
                self.error = f"token login failed: {str(err)[:300]}. Run garmin-mcp-auth."
        if transport is None:
            self.state = "failed"
            self.pending.failed = True
            self.pending.reason = self.error or "not logged in"
            with self.client.state.lock:
                self.client.state.auth_error = self.pending.reason
                from datetime import UTC, datetime

                self.client.state.auth_error_at = datetime.now(UTC)
            log.error("Garmin login failed: %s", self.error)
            return
        self.client.transport = transport
        self.state = "ready"

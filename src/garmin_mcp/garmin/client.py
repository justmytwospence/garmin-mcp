"""The Garmin Connect client: python-garminconnect behind a pacer.

Every request to Garmin goes through one GarminClient, strictly sequentially. Background
work keeps about 1.5 s (with jitter) between calls; tool calls get a shorter gap and go
first. A 429 stops all background work for 30 minutes; 5xx and network errors are
retried with exponential backoff; an authentication failure stops everything until a
person re-authenticates with garmin-mcp-auth (the server never re-logs in on its own,
so it cannot hammer Garmin's SSO).

The Transport protocol is what tests fake: a plain request(...) over the Connect API.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

log = logging.getLogger("garmin_mcp.client")

Priority = Literal["interactive", "incremental", "backfill"]


class GarminError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NotFound(GarminError):
    """Garmin answered 404 (or 204 / an empty body where a resource was expected)."""


class RateLimited(GarminError):
    """Garmin answered 429, or the client is in its 30-minute backoff."""


class AuthError(GarminError):
    """Tokens were rejected; a person must run garmin-mcp-auth."""


class Unavailable(GarminError):
    """5xx or network error after retries."""


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = ""

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class Transport(Protocol):
    """One HTTP exchange with the Connect API, raising the errors above."""

    display_name: str | None

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None, binary: bool = False) -> Response: ...


_STATUS_RE = re.compile(r"API Error (\d{3})")


class GarminConnectTransport:
    """Transport over python-garminconnect's native client, using its stored tokens."""

    def __init__(self, api: Any) -> None:
        self.api = api
        self.display_name: str | None = getattr(api, "display_name", None)

    @classmethod
    def login(cls, tokenstore: str) -> GarminConnectTransport:
        from garminconnect import Garmin

        api = Garmin()
        api.login(tokenstore)
        if not getattr(api, "display_name", None):
            raise AuthError("Login from stored tokens did not load the profile (display name missing)")
        return cls(api)

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None, binary: bool = False) -> Response:
        import garminconnect

        kwargs: dict[str, Any] = {}
        if params:
            kwargs["params"] = params
        if json is not None:
            kwargs["json"] = json
        if binary:
            kwargs["headers"] = {"Accept": "*/*"}
        try:
            resp = self.api.client._run_request(method, path, **kwargs)
        except garminconnect.GarminConnectNotFoundError as err:
            raise NotFound(str(err), 404) from err
        except garminconnect.GarminConnectTooManyRequestsError as err:
            raise RateLimited(str(err), 429) from err
        except garminconnect.GarminConnectAuthenticationError as err:
            raise AuthError(str(err), 401) from err
        except garminconnect.GarminConnectConnectionError as err:
            m = _STATUS_RE.search(str(err))
            status = int(m.group(1)) if m else None
            if status == 404:
                raise NotFound(str(err), 404) from err
            if status == 429:
                raise RateLimited(str(err), 429) from err
            if status in (401, 403):
                raise AuthError(str(err), status) from err
            if status is not None and status >= 500:
                raise Unavailable(str(err), status) from err
            if status is None:
                raise Unavailable(str(err), None) from err
            raise GarminError(str(err), status) from err
        except Exception as err:  # network and library errors
            if isinstance(err, (ValueError, TypeError)):
                raise GarminError(str(err)) from err
            raise Unavailable(str(err)) from err
        status = getattr(resp, "status_code", 200)
        body = getattr(resp, "content", b"") or b""
        ctype = ""
        headers = getattr(resp, "headers", None)
        if headers is not None:
            try:
                ctype = headers.get("content-type", "") or ""
            except Exception:
                ctype = ""
        return Response(status, body, ctype)


@dataclass
class RequestSample:
    observed_at: datetime
    method: str
    path: str
    status: int | None
    priority: str
    ms: int
    bytes: int


@dataclass
class ClientState:
    backoff_until: float = 0.0
    auth_error: str | None = None
    auth_error_at: datetime | None = None
    last_request_at: float = 0.0
    requests: int = 0
    errors: int = 0
    rate_limits: int = 0
    last_error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class GarminClient:
    """Paced, sequential access to Garmin Connect."""

    def __init__(
        self,
        transport: Transport,
        *,
        on_sample: Callable[[RequestSample], None] | None = None,
        background_gap: float = 1.5,
        interactive_gap: float = 0.5,
        jitter: float = 0.5,
        backoff_seconds: float = 30 * 60,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.transport = transport
        self.on_sample = on_sample
        self.background_gap = background_gap
        self.interactive_gap = interactive_gap
        self.jitter = jitter
        self.backoff_seconds = backoff_seconds
        self.retries = retries
        self._sleep = sleep
        self.state = ClientState()
        self._cond = threading.Condition()
        self._busy = False
        self._interactive_waiting = 0

    @property
    def display_name(self) -> str:
        dn = self.transport.display_name
        if not dn:
            raise AuthError("No display name: not logged in")
        return dn

    # ------------------------------------------------------------- pacing

    def backoff_remaining(self) -> float:
        return max(0.0, self.state.backoff_until - time.monotonic())

    def check_available(self, priority: Priority) -> None:
        """Raise if Garmin must not be asked right now."""
        if self.state.auth_error:
            raise AuthError(f"Not authenticated: {self.state.auth_error}. Run garmin-mcp-auth.")
        remaining = self.backoff_remaining()
        if remaining > 0:
            raise RateLimited(f"Backing off after a 429 for another {int(remaining)} s", 429)

    def _acquire(self, priority: Priority) -> None:
        interactive = priority == "interactive"
        with self._cond:
            if interactive:
                self._interactive_waiting += 1
            try:
                while self._busy or (not interactive and self._interactive_waiting > 0):
                    self._cond.wait()
                self._busy = True
            finally:
                if interactive:
                    self._interactive_waiting -= 1

    def _release(self) -> None:
        with self._cond:
            self._busy = False
            self._cond.notify_all()

    def _pace(self, priority: Priority) -> None:
        gap = self.interactive_gap if priority == "interactive" else self.background_gap + random.random() * self.jitter
        wait = self.state.last_request_at + gap - time.monotonic()
        if wait > 0:
            self._sleep(wait)

    # ------------------------------------------------------------- requests

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        binary: bool = False,
        priority: Priority = "backfill",
    ) -> Response:
        self.check_available(priority)
        self._acquire(priority)
        try:
            attempt = 0
            while True:
                self.check_available(priority)
                self._pace(priority)
                t0 = time.monotonic()
                status: int | None = None
                size = 0
                try:
                    resp = self.transport.request(method, path, params=params, json=json, binary=binary)
                    status, size = resp.status, len(resp.body)
                    return resp
                except NotFound as err:
                    status = 404
                    raise err
                except RateLimited as err:
                    status = 429
                    with self.state.lock:
                        self.state.rate_limits += 1
                        self.state.backoff_until = time.monotonic() + self.backoff_seconds
                        self.state.last_error = f"429 on {path}"
                    log.warning("garmin: 429 on %s; backing off %d s", path, self.backoff_seconds)
                    raise err
                except AuthError as err:
                    status = err.status
                    with self.state.lock:
                        self.state.auth_error = str(err)[:300]
                        self.state.auth_error_at = datetime.now(UTC)
                        self.state.last_error = f"auth: {err}"
                    log.error("garmin: authentication failed on %s: %s", path, err)
                    raise err
                except Unavailable as err:
                    status = err.status
                    attempt += 1
                    with self.state.lock:
                        self.state.errors += 1
                        self.state.last_error = str(err)[:300]
                    if attempt > self.retries:
                        raise err
                    delay = min(2.0 * 2**attempt, 60.0)
                    log.info("garmin: %s on %s, retry %d in %.0f s", err, path, attempt, delay)
                    self._record(method, path, status, priority, t0, size)
                    self._sleep(delay)
                    continue
                except GarminError as err:
                    status = err.status
                    with self.state.lock:
                        self.state.errors += 1
                        self.state.last_error = str(err)[:300]
                    raise err
                finally:
                    with self.state.lock:
                        self.state.last_request_at = time.monotonic()
                        self.state.requests += 1
                    self._record(method, path, status, priority, t0, size)
        finally:
            self._release()

    def _record(self, method: str, path: str, status: int | None, priority: str, t0: float, size: int) -> None:
        if self.on_sample is None:
            return
        try:
            self.on_sample(RequestSample(datetime.now(UTC), method, path, status, priority, int((time.monotonic() - t0) * 1000), size))
        except Exception:  # never let logging break a request
            log.exception("request log failed")

    def get_text(self, path: str, params: dict[str, Any] | None = None, *, priority: Priority = "backfill") -> str | None:
        """GET returning the body text; None for 404, 204 and empty bodies."""
        try:
            resp = self.request("GET", path, params=params, priority=priority)
        except NotFound:
            return None
        if resp.status == 204 or not resp.body.strip():
            return None
        return resp.text

    def get_bytes(self, path: str, *, priority: Priority = "backfill") -> bytes | None:
        try:
            resp = self.request("GET", path, binary=True, priority=priority)
        except NotFound:
            return None
        return resp.body or None

    def send(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None, priority: Priority = "interactive") -> str | None:
        """POST/PUT/DELETE returning the body text (None when empty)."""
        resp = self.request(method, path, params=params, json=json, priority=priority)
        if resp.status == 204 or not resp.body.strip():
            return None
        return resp.text

    def status(self) -> dict[str, Any]:
        s = self.state
        return {
            "requests": s.requests,
            "errors": s.errors,
            "rate_limits": s.rate_limits,
            "backoff_remaining_s": int(self.backoff_remaining()),
            "auth_error": s.auth_error,
            "auth_error_at": s.auth_error_at.isoformat() if s.auth_error_at else None,
            "last_error": s.last_error,
            "background_gap_s": self.background_gap,
        }

"""The Garmin Connect client: python-garminconnect behind a pacer.

Every request to Garmin goes through one GarminClient, strictly sequentially. Background
work keeps about 1.5 s (with jitter) between calls; tool calls get a shorter gap and go
first. A 429 stops admission for at least the configured cooldown (normally 30
minutes), honoring longer Retry-After; only safe reads retry 5xx/network failures.
Writes are never replayed on ambiguous failures. An authentication failure stops everything until a
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
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import Any, Literal, Protocol

log = logging.getLogger("garmin_mcp.client")

Priority = Literal["interactive", "incremental", "backfill"]

# The commands every authentication message points at.
AUTH_COMMAND = "docker compose run --rm garmin-mcp garmin-mcp-auth"
AUTH_COMMAND_LOCAL = "garmin-mcp-auth"
RESTART_COMMAND = "docker compose restart garmin-mcp"


def auth_fix() -> str:
    return f"Run `{AUTH_COMMAND}` (outside Docker: `{AUTH_COMMAND_LOCAL}`), enter the MFA code Garmin sends, then `{RESTART_COMMAND}`."


class GarminError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after_s: float | None = None


class NotFound(GarminError):
    """Garmin answered 404 (or 204 / an empty body where a resource was expected)."""


class RateLimited(GarminError):
    """Garmin answered 429, or the client is in its cooldown."""


class AuthError(GarminError):
    """Tokens were rejected; a person must run garmin-mcp-auth."""


class Unavailable(GarminError):
    """5xx or network error after retries."""


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class Transport(Protocol):
    """One HTTP exchange with the Connect API, raising the errors above."""

    display_name: str | None

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None, binary: bool = False, headers: dict[str, str] | None = None, files: Any = None) -> Response: ...

    def call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke a python-garminconnect method by name (for writes whose payloads the library builds)."""
        ...


_STATUS_RE = re.compile(r"API Error (\d{3})")


def retry_after(headers: dict[str, str], now: datetime) -> float | None:
    """Retry-After delta seconds or HTTP date; malformed/absent means unknown."""
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if value is None:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            at = parsedate_to_datetime(value)
            seconds = (at - now).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if isfinite(seconds) else None


class GarminConnectTransport:
    """Transport over python-garminconnect's native client, using its stored tokens."""

    def __init__(self, api: Any) -> None:
        self.api = api
        self.display_name: str | None = getattr(api, "display_name", None)
        # Library method decorators otherwise retry writes on ambiguous failures.
        # Our paced client owns retries, and permits them only for safe reads.
        api.retry_attempts = 0
        self._last_headers: dict[str, str] = {}
        session = getattr(getattr(api, "client", None), "_api_session", None)
        if session is not None:
            session.hooks.setdefault("response", []).append(self._observe_response)

    def _observe_response(self, response: Any, **_: Any) -> None:
        # _run_request discards response headers when it raises; capture only
        # Retry-After, never cookies, auth headers or account payloads.
        self._last_headers = {k: v for k, v in response.headers.items() if k.lower() == "retry-after"}

    @classmethod
    def login(cls, tokenstore: str) -> GarminConnectTransport:
        from garminconnect import Garmin

        api = Garmin()
        api.login(tokenstore)
        if not getattr(api, "display_name", None):
            raise AuthError("Login from stored tokens did not load the profile (display name missing)")
        return cls(api)

    def call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        self._last_headers = {}
        try:
            return getattr(self.api, method_name)(*args, **kwargs)
        except Exception as err:  # noqa: BLE001
            raise self._translate(err) from err

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None, binary: bool = False, headers: dict[str, str] | None = None, files: Any = None) -> Response:
        self._last_headers = {}
        kwargs: dict[str, Any] = {}
        if params:
            kwargs["params"] = params
        if json is not None:
            kwargs["json"] = json
        if files is not None:
            kwargs["files"] = files
        h = dict(headers or {})
        if binary:
            h["Accept"] = "*/*"
        if h:
            kwargs["headers"] = h
        try:
            resp = self.api.client._run_request(method, path, **kwargs)
        except Exception as err:  # noqa: BLE001
            raise self._translate(err) from err
        status = getattr(resp, "status_code", 200)
        body = getattr(resp, "content", b"") or b""
        ctype = ""
        resp_headers = getattr(resp, "headers", None)
        if resp_headers is not None:
            try:
                ctype = resp_headers.get("content-type", "") or ""
            except Exception:
                ctype = ""
        return Response(status, body, ctype, dict(resp_headers or {}))

    def _translate(self, err: Exception) -> GarminError:
        translated = self._classify(err)
        headers = self._last_headers
        seen: set[int] = set()
        cause: BaseException | None = err
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            response = getattr(cause, "response", None)
            if response is not None:
                headers = dict(getattr(response, "headers", {}) or {})
                break
            cause = cause.__cause__ or cause.__context__
        translated.retry_after_s = retry_after(headers, datetime.now(UTC))
        return translated

    @staticmethod
    def _classify(err: Exception) -> GarminError:
        import garminconnect

        try:
            raise err
        except garminconnect.GarminConnectNotFoundError as err:
            return NotFound(str(err), 404)
        except garminconnect.GarminConnectTooManyRequestsError as err:
            return RateLimited(str(err), 429)
        except garminconnect.GarminConnectAuthenticationError as err:
            return AuthError(str(err), 401)
        except garminconnect.GarminConnectConnectionError as err:
            m = _STATUS_RE.search(str(err))
            status = int(m.group(1)) if m else None
            if status == 404:
                return NotFound(str(err), 404)
            if status == 429:
                return RateLimited(str(err), 429)
            if status in (401, 403):
                return AuthError(str(err), status)
            if status is not None and status >= 500:
                return Unavailable(str(err), status)
            if status is None:
                return Unavailable(str(err), None)
            return GarminError(str(err), status)
        except GarminError as err:
            return err
        except (ValueError, TypeError) as err:
            return GarminError(str(err))
        except Exception as err:  # network and library errors
            return Unavailable(str(err))


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
    # Why authentication failed (see garmin.auth.classify_login_error) and what fixes it.
    auth_error_kind: str | None = None
    auth_error_fix: str | None = None
    last_request_at: float = 0.0
    last_observed_at: datetime | None = None
    cooldown_until: datetime | None = None
    rate_limit_observed_at: datetime | None = None
    retry_after_s: float | None = None
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
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.transport = transport
        self.on_sample = on_sample
        self.background_gap = background_gap
        self.interactive_gap = interactive_gap
        self.jitter = jitter
        self.backoff_seconds = backoff_seconds
        self.retries = retries
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self.state = ClientState()
        self._cond = threading.Condition()
        self._busy = False
        self._interactive_waiting = 0

    @property
    def display_name(self) -> str:
        dn = self.transport.display_name
        if not dn:
            if self.state.auth_error:
                raise AuthError(f"Not authenticated: {self.state.auth_error}")
            raise AuthError("Not logged in yet (the login runs once at startup; try again in a few seconds)")
        return dn

    # ------------------------------------------------------------- pacing

    def backoff_remaining(self) -> float:
        return max(0.0, self.state.backoff_until - self._clock())

    def check_available(self, priority: Priority) -> None:
        """Raise if Garmin must not be asked right now."""
        if self.state.auth_error:
            raise AuthError(f"Not authenticated: {self.state.auth_error}")
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
        wait = self.state.last_request_at + gap - self._clock()
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
        headers: dict[str, str] | None = None,
        files: Any = None,
        priority: Priority = "backfill",
    ) -> Response:
        return self._exchange(priority, path, method, lambda: self.transport.request(method, path, params=params, json=json, binary=binary, headers=headers, files=files))

    def call(self, method_name: str, *args: Any, priority: Priority = "interactive", **kwargs: Any) -> Any:
        """Run a library operation (possibly several HTTP requests) under the pacer, without ambiguous retries."""
        return self._exchange(priority, f"call:{method_name}", "CALL", lambda: self.transport.call(method_name, *args, **kwargs))

    def _exchange(self, priority: Priority, path: str, method: str, do: Callable[[], Any]) -> Any:
        self.check_available(priority)
        self._acquire(priority)
        try:
            attempt = 0
            while True:
                self.check_available(priority)
                self._pace(priority)
                t0 = self._clock()
                status: int | None = None
                size = 0
                retry_delay: float | None = None
                try:
                    resp = do()
                    if isinstance(resp, Response):
                        status, size = resp.status, len(resp.body)
                        error: GarminError | None = None
                        if status == 429:
                            error = RateLimited("HTTP 429", status)
                        elif status in (401, 403):
                            error = AuthError(f"HTTP {status}", status)
                        elif status == 404:
                            error = NotFound("HTTP 404", status)
                        elif status >= 500:
                            error = Unavailable(f"HTTP {status}", status)
                        elif status >= 400:
                            error = GarminError(f"HTTP {status}", status)
                        if error is not None:
                            error.retry_after_s = retry_after(resp.headers, self._now())
                            raise error
                    else:
                        status = 200
                    return resp
                except NotFound as err:
                    status = 404
                    raise err
                except RateLimited as err:
                    status = 429
                    with self.state.lock:
                        self.state.rate_limits += 1
                        delay = max(self.backoff_seconds, err.retry_after_s or 0)
                        self.state.backoff_until = self._clock() + delay
                        self.state.rate_limit_observed_at = self._now()
                        try:
                            self.state.cooldown_until = self._now() + timedelta(seconds=delay)
                        except OverflowError:
                            self.state.cooldown_until = None  # Duration exceeds datetime's representable range; monotonic admission remains blocked.
                        self.state.retry_after_s = err.retry_after_s
                        self.state.last_error = f"429 on {path}"
                    log.warning("garmin: 429 on %s; backing off %d s", path, delay)
                    raise err
                except AuthError as err:
                    status = err.status
                    with self.state.lock:
                        if not self.state.auth_error:
                            self.state.auth_error = f"Garmin rejected the session while requesting {path} (HTTP {err.status or 'error'}: {str(err)[:200]}); the tokens expired or were revoked. {auth_fix()}"
                            self.state.auth_error_kind = "expired"
                            self.state.auth_error_fix = auth_fix()
                        self.state.auth_error_at = self._now()
                        self.state.last_error = f"auth: {err}"
                    log.error("garmin: authentication failed on %s: %s. Background sync is stopped; %s", path, err, auth_fix())
                    raise err
                except Unavailable as err:
                    status = err.status
                    attempt += 1
                    with self.state.lock:
                        self.state.errors += 1
                        self.state.last_error = str(err)[:300]
                    if method.upper() not in {"GET", "HEAD", "OPTIONS"} or attempt > self.retries:
                        raise err  # Never replay an ambiguous write, including library CALLs.
                    retry_delay = max(min(2.0 * 2**attempt, 60.0), err.retry_after_s or 0)
                    log.info("garmin: %s on %s, retry %d in %.0f s", err, path, attempt, retry_delay)
                except GarminError as err:
                    status = err.status
                    with self.state.lock:
                        self.state.errors += 1
                        self.state.last_error = str(err)[:300]
                    raise err
                finally:
                    with self.state.lock:
                        self.state.last_request_at = self._clock()
                        self.state.last_observed_at = self._now()
                        self.state.requests += 1
                    self._record(method, path, status, priority, t0, size)
                if retry_delay is not None:
                    self._sleep(retry_delay)
        finally:
            self._release()

    def _record(self, method: str, path: str, status: int | None, priority: str, t0: float, size: int) -> None:
        if self.on_sample is None:
            return
        try:
            self.on_sample(RequestSample(self._now(), method, path, status, priority, int((self._clock() - t0) * 1000), size))
        except Exception:  # never let logging break a request
            log.exception("request log failed")

    def get_text(self, path: str, params: dict[str, Any] | None = None, *, priority: Priority = "backfill", headers: dict[str, str] | None = None) -> str | None:
        """GET returning the body text; None for 404, 204 and empty bodies."""
        try:
            resp = self.request("GET", path, params=params, headers=headers, priority=priority)
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

    def send(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None, files: Any = None, priority: Priority = "interactive") -> str | None:
        """POST/PUT/DELETE returning the body text (None when empty)."""
        resp = self.request(method, path, params=params, json=json, files=files, priority=priority)
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
            "auth_error_kind": s.auth_error_kind,
            "auth_error_at": s.auth_error_at.isoformat() if s.auth_error_at else None,
            "last_error": s.last_error,
            "background_gap_s": self.background_gap,
            "interactive_gap_s": self.interactive_gap,
            "background_jitter_s": self.jitter,
            "observed_at": s.last_observed_at.isoformat() if s.last_observed_at else None,
            "cooldown_until": s.cooldown_until.isoformat() if s.cooldown_until else None,
            "retry_after_s": s.retry_after_s,
            "rate_limit_observed_at": s.rate_limit_observed_at.isoformat() if s.rate_limit_observed_at else None,
            "retry_after_source": "Provider Retry-After when available; cooldown floor is local policy, not a quota reset.",
            "general_quota": {"limit": None, "remaining": None, "reset_at": None, "source": "unknown; Garmin publishes no general allowance here"},
            "accounting": "Local transport operations including retries, not authoritative HTTP quota. Library token refreshes, 401 recovery and compound methods may make additional requests; other clients are not counted.",
            "retry_policy": "Only GET/HEAD/OPTIONS retry network/5xx failures; writes and library CALLs are never replayed by this client. 429 stops admission for the configured cooldown (normally 30 minutes) or a longer Retry-After.",
            "cooldown_policy_s": self.backoff_seconds,
        }

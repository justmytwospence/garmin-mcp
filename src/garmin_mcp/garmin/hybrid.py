"""GARMIN_TRANSPORT=hybrid: the Personal API for what it serves, Garmin Connect for everything else.

A GET whose path is one of the Personal API's (personal.SUPPORTED_PATHS, matched by personal.serves) goes to the
Personal API; every other GET, every write and every library call go to Connect. Each side fails on its own:

- Connect's login failed or its session was rejected: requests only Connect serves raise ConnectUnavailable, which
  does not stop the client, and the engine plans as under GARMIN_TRANSPORT=personal (supports() and partial follow
  the Connect side), so the Personal API's datasets and the exports keep going.
- The Personal API fails on a request (a rejected key, 401/403, 5xx, a network error, Unsupported): the request is
  sent to Connect instead while Connect is logged in. A rejected key takes the Personal side out until a restart.

Only when both sides are down does an AuthError reach the client and stop everything, as in the other modes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from .auth import LoginProblem
from .client import AuthError, ConnectUnavailable, GarminError, Response, Unavailable, auth_fix
from .personal import FIX as PERSONAL_FIX
from .personal import SUPPORTED_PATHS, serves

log = logging.getLogger("garmin_mcp.hybrid")

SIDES = ("connect", "personal")


class RoutingTransport:
    def __init__(self, personal: Any | None, connect: Any | None, *, personal_problem: LoginProblem | None = None, connect_problem: LoginProblem | None = None) -> None:
        self.personal = personal
        self.connect = connect
        self.down: dict[str, tuple[LoginProblem, datetime]] = {}
        if personal is None:
            self._fail("personal", personal_problem or LoginProblem("unknown", "The Personal API login failed.", PERSONAL_FIX))
        if connect is None:
            self._fail("connect", connect_problem or LoginProblem("unknown", "The Garmin Connect login failed.", auth_fix()))

    # ------------------------------------------------------------- state

    def ok(self, side: str) -> bool:
        return getattr(self, side) is not None and side not in self.down

    def _fail(self, side: str, problem: LoginProblem) -> None:
        if side in self.down:
            return
        self.down[side] = (problem, datetime.now(UTC))
        if side == "connect":
            log.error("hybrid: Garmin Connect is failing (%s): %s Only the Personal API's datasets sync until it is fixed.", problem.kind, problem.text())
        else:
            log.error("hybrid: the Personal API is failing (%s): %s Its requests go to Garmin Connect instead.", problem.kind, problem.text())

    def side_status(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for side in SIDES:
            if side in self.down:
                p, at = self.down[side]
                out[side] = {"state": "failed", "kind": p.kind, "error": p.message, "since": at.isoformat(), "fix": p.fix}
            else:
                out[side] = {"state": "ready"}
        return out

    @property
    def display_name(self) -> str | None:
        # The Personal API's placeholder name keeps its datasets resolving while Connect is down; the paths that
        # need the real display name are Connect's and are refused then anyway.
        if self.ok("connect"):
            return self.connect.display_name
        return self.personal.display_name if self.ok("personal") else None

    @property
    def auth_fix_text(self) -> str | None:
        """What the client shows when it stops: both sides are down by then."""
        return " ".join(p.fix for p, _ in self.down.values()) or None

    @property
    def partial(self) -> bool:
        return not self.ok("connect")

    def supports(self, path_template: str) -> bool:
        return self.ok("connect") or (self.ok("personal") and path_template in SUPPORTED_PATHS)

    # ------------------------------------------------------------- requests

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None, binary: bool = False, headers: dict[str, str] | None = None, files: Any = None) -> Response:
        kw: dict[str, Any] = {"params": params, "json": json, "binary": binary, "headers": headers, "files": files}
        if method.upper() != "GET" or not serves(path) or not self.ok("personal"):
            return self._connect(f"{method} {path}", lambda: self.connect.request(method, path, **kw))
        resp: Response | None = None
        error: Exception | None = None
        try:
            resp = self.personal.request(method, path, **kw)
            if resp.status not in (401, 403) and resp.status < 500:
                return resp
            failure = f"HTTP {resp.status}"
        except Exception as err:  # noqa: BLE001
            error, failure = err, str(err)[:200]
        rejected = (resp is not None and resp.status in (401, 403)) or (isinstance(error, AuthError) and error.status in (401, 403))
        if rejected:
            self._fail("personal", LoginProblem("api_key_rejected", f"The Personal API rejected the key or its token while requesting {path} ({failure}).", PERSONAL_FIX))
        if self.ok("connect"):
            log.info("hybrid: the Personal API failed on %s (%s); asking Garmin Connect", path, failure)
            try:
                return self._connect(f"{method} {path}", lambda: self.connect.request(method, path, **kw))
            except ConnectUnavailable:
                pass  # Connect's session was rejected just now too; report the Personal API's failure
        if resp is not None:
            return resp  # the client turns it into its error, as under GARMIN_TRANSPORT=personal
        assert error is not None
        if isinstance(error, AuthError) and not rejected:
            raise Unavailable(str(error), error.status) from error  # a token endpoint 5xx is not a rejected key
        if isinstance(error, GarminError):
            raise error
        raise Unavailable(str(error)) from error

    def call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        return self._connect(f"call:{method_name}", lambda: self.connect.call(method_name, *args, **kwargs))

    def _connect(self, what: str, do: Any) -> Any:
        if not self.ok("connect"):
            raise self._refused(what)
        try:
            resp = do()
        except AuthError as err:
            self._connect_rejected(what, f"HTTP {err.status or 'error'}: {str(err)[:200]}")
            raise self._refused(what) from err
        if isinstance(resp, Response) and resp.status in (401, 403):
            self._connect_rejected(what, f"HTTP {resp.status}")
            raise self._refused(what)
        return resp

    def _connect_rejected(self, what: str, detail: str) -> None:
        self._fail("connect", LoginProblem("expired", f"Garmin Connect rejected the session while requesting {what} ({detail}); the tokens expired or were revoked.", auth_fix()))

    def _refused(self, what: str) -> GarminError:
        problem, _ = self.down["connect"]
        if not self.ok("personal"):
            return AuthError(f"Both sides of the hybrid transport are failing. Garmin Connect: {problem.message} Personal API: {self.down['personal'][0].message}", 401)
        return ConnectUnavailable(f"{what} needs Garmin Connect, which is failing ({problem.kind}): {problem.message} The Personal API's datasets keep syncing. {problem.fix}")

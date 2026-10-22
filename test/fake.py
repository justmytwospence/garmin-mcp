"""A fake Garmin Connect transport serving the scrubbed fixtures."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from fitgen import activity_fit, wellness_fit  # noqa: E402

from garmin_mcp.garmin.client import NotFound, RateLimited, Response, Unavailable  # noqa: E402
from garmin_mcp.garmin.datasets import ACTIVITY_FIT_PATH, ACTIVITY_LIST_PATH, BY_NAME, RELOAD_PATH, WELLNESS_FIT_PATH  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "payloads.json"
DISPLAY_NAME = "display-name-fixture"


def load_fixtures() -> list[dict[str, Any]]:
    return json.loads(FIXTURES.read_text())


class FakeTransport:
    """Answers the Connect API paths of the dataset registry from the fixtures."""

    def __init__(self, fixtures: list[dict[str, Any]] | None = None) -> None:
        self.display_name = DISPLAY_NAME
        self.fixtures = fixtures if fixtures is not None else load_fixtures()
        self.calls: list[tuple[str, str, dict | None]] = []
        self.fail_with: Exception | None = None
        self.fail_paths: dict[str, Exception] = {}
        self.by_type: dict[str, dict[str, Any]] = {}
        for f in self.fixtures:
            self.by_type.setdefault(f["type"], {})[f["key"]] = f["body"]
        self.activities = sorted(self.by_type.get("activity_summary", {}).values(), key=lambda a: a.get("startTimeGMT", ""), reverse=True)
        self.reloads = 0

    def _dump(self, body: Any) -> Response:
        return Response(200, json.dumps(body).encode(), "application/json")

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None, binary: bool = False) -> Response:  # noqa: A002
        self.calls.append((method, path, params))
        if self.fail_with is not None:
            raise self.fail_with
        for p, err in self.fail_paths.items():
            if path.startswith(p):
                raise err
        if path == ACTIVITY_LIST_PATH:
            start = int((params or {}).get("start", 0))
            limit = int((params or {}).get("limit", 100))
            return self._dump(self.activities[start : start + limit])
        if path.startswith("/download-service/files/activity/"):
            aid = path.rsplit("/", 1)[1]
            if aid in self.by_type.get("activity_summary", {}):
                return Response(200, activity_fit(datetime(2026, 1, 2, 10, 0, tzinfo=UTC)), "application/octet-stream")
            raise NotFound("no file", 404)
        if path.startswith("/download-service/files/wellness/"):
            return Response(200, wellness_fit(datetime(2026, 1, 2, 0, 0, tzinfo=UTC)), "application/octet-stream")
        if method == "POST" and path.startswith("/wellness-service/wellness/epoch/request/"):
            self.reloads += 1
            if self.reloads > 30:
                return self._dump({"status": "DENIED"})
            return self._dump({"status": "SUBMITTED", "calendarDate": path.rsplit("/", 1)[1]})
        # datasets: match by path template
        for ds in BY_NAME.values():
            key = self._match(ds, path, params)
            if key is None:
                continue
            bodies = self.by_type.get(ds.name, {})
            if ds.kind == "range":
                return self._range(ds, bodies, params, path)
            body = bodies.get(key)
            if body is None and ds.kind == "snapshot" and key != "self" and "self" in bodies:
                body = bodies["self"]
            if body is None and ds.kind == "snapshot" and bodies:
                body = next(iter(bodies.values()))
            if body is None:
                return Response(204, b"", "")
            return self._dump(body)
        raise NotFound(f"no fixture for {path}", 404)

    def _match(self, ds, path: str, params: dict | None) -> str | None:
        tmpl = ds.path.replace("{dn}", self.display_name)
        parts_t = tmpl.split("/")
        parts_p = path.split("/")
        if len(parts_t) != len(parts_p):
            return None
        key = None
        for t, p in zip(parts_t, parts_p, strict=True):
            if t in ("{key}", "{start}", "{end}"):
                key = key or p
                continue
            if t != p:
                return None
        # params that carry the key
        for k, v in ds.params.items():
            if isinstance(v, str) and "{key}" in v:
                if params is None or k not in params:
                    return None
                key = str(params[k])
            elif isinstance(v, str) and "{" not in v and (params is None or str(params.get(k)) != v):
                return None
        return key or "self"

    def _range(self, ds, bodies: dict[str, Any], params: dict | None, path: str) -> Response:
        if ds.whole:
            return self._dump(next(iter(bodies.values()))) if bodies else Response(204, b"", "")
        # wrap per-day fixtures back into the list the endpoint returns
        items = list(bodies.values())
        if ds.name == "training_readiness":
            flat = [el for v in items for el in v]
            return self._dump(flat)
        if ds.name == "endurance_score":
            return self._dump({"groupMap": bodies})
        if ds.name == "daily_summary_v2":
            return self._dump({"data": items})
        if ds.name == "hrv_summary":
            return self._dump({"hrvSummaries": items})
        if ds.name == "sleep_stats":
            return self._dump({"individualStats": items})
        if ds.name == "hill_score":
            return self._dump({"hillScoreDTOList": items})
        return self._dump(items)


class FlakyTransport(FakeTransport):
    """Fails the first N requests with a 503 to exercise retries."""

    def __init__(self, failures: int = 2) -> None:
        super().__init__()
        self.failures = failures

    def request(self, method, path, **kw):
        if self.failures > 0:
            self.failures -= 1
            raise Unavailable("API Error 503", 503)
        return super().request(method, path, **kw)


class RateLimitedTransport(FakeTransport):
    def request(self, method, path, **kw):
        raise RateLimited("API Error 429", 429)

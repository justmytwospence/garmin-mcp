"""Adapters that let vendored upstream tool modules (written against a
python-garminconnect client and FastMCP's `@app.tool()`) run over the mirror.

LegacyClient offers the few client calls those modules make: `connectapi` (a GET,
served from the mirror's cached pass-through: fetched at interactive priority, kept for
a short TTL, served stale when Garmin cannot be asked) and `client.put/post/delete`
(writes through the paced client, after which the cached reports of that area are
dropped so the next read is fresh). UpstreamApp turns `@app.tool()` into a Registry
registration with annotations derived from the tool name.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from ..garmin.client import NotFound
from ..mirror import GarminNotFound, Mirror
from .base import DESTRUCTIVE, READ, WRITE, Registry


class LegacyClient:
    def __init__(self, mirror: Mirror, area: str, ttl: timedelta = timedelta(minutes=10)) -> None:
        self.mirror = mirror
        self.area = area
        self.ttl = ttl
        self.client = self  # upstream calls garmin_client.client.put(...)

    def _key(self, path: str, params: dict[str, Any] | None) -> str:
        raw = path + ("?" + json.dumps(params, sort_keys=True) if params else "")
        return hashlib.sha1(raw.encode()).hexdigest()[:16] + ":" + path[-60:]

    def connectapi(self, path: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        if not path.startswith("/"):
            path = "/" + path
        try:
            return self.mirror.report(f"{self.area}:{path.split('/')[2] if path.count('/') > 2 else 'root'}", path, params, self._key(path, params), self.ttl).data
        except GarminNotFound as err:
            raise NotFound(f"API Error 404 - {err}", 404) from err

    def invalidate(self) -> None:
        self.mirror.db.run("DELETE FROM raw_latest WHERE object_type LIKE ?", [f"{self.area}:%"])

    def _write(self, method: str, path: str, **kwargs: Any) -> Any:
        if not path.startswith("/"):
            path = "/" + path
        kwargs.pop("api", None)
        result = self.mirror.write(method, path, params=kwargs.get("params"), json_body=kwargs.get("json"), refetch=self.invalidate)
        return result

    def put(self, _domain: str, path: str, **kwargs: Any) -> Any:
        return self._write("PUT", path, **kwargs)

    def post(self, _domain: str, path: str, **kwargs: Any) -> Any:
        return self._write("POST", path, **kwargs)

    def delete(self, _domain: str, path: str, **kwargs: Any) -> Any:
        return self._write("DELETE", path, **kwargs)

    def request(self, method: str, _domain: str, path: str, **kwargs: Any) -> Any:
        return self._write(method.upper(), path, **kwargs)


class UpstreamApp:
    """`app.tool()` as the vendored modules expect it."""

    def __init__(self, reg: Registry) -> None:
        self.reg = reg

    def tool(self, *args: Any, **kwargs: Any):  # noqa: ANN201
        name = kwargs.get("name")

        def decorator(fn):  # noqa: ANN001, ANN202
            tool_name = name or fn.__name__
            if tool_name.startswith(("get_", "search_", "list_", "count_")):
                ann = READ
            elif tool_name.startswith("delete_"):
                ann = DESTRUCTIVE
            else:
                ann = WRITE
            return self.reg.tool(tool_name, annotations=ann)(fn)

        return decorator

    def __getattr__(self, item: str) -> Any:
        return getattr(self.reg.server, item)

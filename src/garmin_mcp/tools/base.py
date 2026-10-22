"""Tool registration: every tool is a plain function over the Mirror that returns
JSON-able data. The registry wraps it for the MCP server: validates dates, runs it
(the SDK already moves sync functions to a worker thread), serializes the result with
a size bound, and appends the provenance block assembled from every mirror read the
call made (served from the mirror, fetched just now, or stale and why).

Tool names, parameters and descriptions follow Taxuspt/garmin_mcp (MIT) so existing
clients keep working; the implementations answer from the DuckDB mirror.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import TextContent, ToolAnnotations

from ..garmin.client import AuthError, GarminError, NotFound, RateLimited, Unavailable
from ..mirror import GarminNotFound, Mirror, Provenance, collect_provenance, merge_provenance
from .profiles import resolve_tool_profile

log = logging.getLogger("garmin_mcp.tools")

CHARACTER_LIMIT = int(os.environ.get("GARMIN_MCP_CHARACTER_LIMIT", "60000"))

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)
IDEMPOTENT_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True)


class ToolFailure(ToolError):
    """A tool error with a plain message for the model (no traceback)."""


class NoData(Exception):
    """Garmin has nothing for what was asked: answered as plain text, not as an error (as upstream does)."""


def _parse_set(value: str | None) -> set[str]:
    if not value:
        return set()
    return {n.strip().lower() for n in value.split(",") if n.strip()}


@dataclass
class ToolFilter:
    """GARMIN_TOOL_PROFILE, GARMIN_ENABLED_TOOLS and GARMIN_DISABLED_TOOLS, as upstream:
    a profile narrows the set, the allowlist adds to it, the denylist removes; with no
    profile the allowlist alone selects, else the denylist alone excludes."""

    profile: frozenset[str] | None = None
    enabled: set[str] | None = None
    disabled: set[str] | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ToolFilter:
        env = os.environ if env is None else env
        enabled_value = env.get("GARMIN_ENABLED_TOOLS")
        enabled = _parse_set(enabled_value)
        if enabled_value and enabled_value.strip() and not enabled:
            raise ValueError("Invalid GARMIN_ENABLED_TOOLS: expected at least one tool name")
        return cls(resolve_tool_profile(env.get("GARMIN_TOOL_PROFILE")), enabled, _parse_set(env.get("GARMIN_DISABLED_TOOLS")))

    def allows(self, name: str) -> bool:
        name = name.lower()
        enabled = self.enabled or set()
        disabled = self.disabled or set()
        if self.profile is not None:
            return (name in self.profile or name in enabled) and name not in disabled
        if enabled:
            return name in enabled
        return name not in disabled


def json_text(data: Any) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if len(text) > CHARACTER_LIMIT:
        text = text[:CHARACTER_LIMIT] + f"\n\n[Response truncated at {CHARACTER_LIMIT} characters; narrow the request (fewer days, a summary tool, or garmin_query with LIMIT)]"
    return text


def error_text(err: Exception) -> str:
    if isinstance(err, GarminNotFound):
        return str(err)
    if isinstance(err, NotFound):
        return f"Not found on Garmin Connect: {err}"
    if isinstance(err, RateLimited):
        return f"Garmin Connect rate limit: {err}. The mirror had no copy to serve; try again later."
    if isinstance(err, AuthError):
        return f"Garmin authentication failed: {err}. Run garmin-mcp-auth and restart the server."
    if isinstance(err, Unavailable):
        return f"Garmin Connect unreachable: {err}"
    if isinstance(err, GarminError):
        return f"Garmin Connect rejected the request: {err}"
    return f"{type(err).__name__}: {err}"


@dataclass
class WriteResult:
    """What a write tool returns: Garmin's answer plus the write's state for retries."""

    data: Any
    write_committed: bool | str = True


class Registry:
    def __init__(self, server: MCPServer, mirror: Mirror, filters: ToolFilter | None = None) -> None:
        self.server = server
        self.mirror = mirror
        self.filters = filters or ToolFilter()
        self.registered: list[str] = []
        self.skipped: list[str] = []
        self.seen: set[str] = set()

    def tool(self, name: str | None = None, *, annotations: ToolAnnotations = READ, description: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name or fn.__name__
            self.seen.add(tool_name.lower())
            if not self.filters.allows(tool_name):
                self.skipped.append(tool_name)
                return fn
            wrapped = self._wrap(fn, tool_name, writes=not bool(annotations.read_only_hint))
            self.server.add_tool(wrapped, name=tool_name, description=description or inspect.getdoc(fn), annotations=annotations, structured_output=False)
            self.registered.append(tool_name)
            return fn

        return decorator

    def _wrap(self, fn: Callable[..., Any], name: str, writes: bool) -> Callable[..., Any]:
        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> list[TextContent]:
            with collect_provenance() as metas:
                try:
                    # Vendored upstream tools are `async def` that await nothing; drive them here, on the worker thread.
                    result = asyncio.run(fn(*args, **kwargs)) if inspect.iscoroutinefunction(fn) else fn(*args, **kwargs)
                except ToolError:
                    raise
                except (NoData, GarminNotFound) as err:
                    result = str(err)
                except (ValueError, TypeError) as err:
                    raise ToolFailure(_write_error(err, writes, metas) if writes else str(err)) from err
                except Exception as err:  # noqa: BLE001
                    log.info("tool %s failed: %s", name, err)
                    raise ToolFailure(_write_error(err, writes, metas) if writes else error_text(err)) from err
            if isinstance(result, WriteResult):
                payload: Any = result.data
                if isinstance(payload, dict) and "write_committed" not in payload:
                    payload = {**payload, "write_committed": result.write_committed}
                result = payload
            if isinstance(result, str):
                content = [TextContent(type="text", text=result[:CHARACTER_LIMIT])]
            else:
                content = [TextContent(type="text", text=json_text(result))]
            content.append(TextContent(type="text", text=merge_provenance(metas).text()))
            return content

        # The SDK builds the input schema from the signature; keep the original one.
        sig = inspect.signature(fn, eval_str=True)
        run.__signature__ = sig.replace(return_annotation=Any)  # type: ignore[attr-defined]
        run.__annotations__ = {p.name: p.annotation for p in sig.parameters.values() if p.annotation is not inspect.Parameter.empty}
        run.__annotations__["return"] = Any
        return run

    def unknown_filter_names(self) -> list[str]:
        f = self.filters
        configured = (f.enabled or set()) | (f.disabled or set())
        return sorted(configured - self.seen)


def _write_error(err: Exception, writes: bool, metas: list[Provenance]) -> str:
    """Error text for a write: says whether the write may have landed."""
    sent = any("accepted by Garmin" in n for m in metas for n in m.notes)
    if sent:
        committed: bool | str = True
    elif isinstance(err, (AuthError, RateLimited, ValueError, TypeError, GarminNotFound, NotFound)):
        committed = False
    elif isinstance(err, Unavailable):
        committed = "unknown"
    elif isinstance(err, GarminError):
        committed = False if (err.status or 0) < 500 else "unknown"
    else:
        committed = "unknown" if writes else False
    return json.dumps({"error": error_text(err), "write_committed": committed, "error_type": type(err).__name__}, indent=2)


# ----------------------------------------------------------------- argument helpers

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_date(value: str, name: str = "date") -> date:
    if not isinstance(value, str) or not _DATE_RE.match(value.strip()):
        raise ToolFailure(f"Invalid {name} {value!r}: use YYYY-MM-DD.")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as err:
        raise ToolFailure(f"Invalid {name} {value!r}: {err}. Use YYYY-MM-DD.") from err


def parse_range(start: str, end: str, max_days: int | None = None) -> tuple[date, date]:
    s, e = parse_date(start, "start_date"), parse_date(end, "end_date")
    if e < s:
        raise ToolFailure("end_date must be on or after start_date.")
    if max_days is not None and (e - s).days + 1 > max_days:
        raise ToolFailure(f"Date range too large ({(e - s).days + 1} days). Maximum is {max_days} days.")
    return s, e


def iso_days(s: date, e: date) -> list[str]:
    return [(s + timedelta(days=i)).isoformat() for i in range((e - s).days + 1)]


def activity_key(activity_id: int | str) -> str:
    key = str(activity_id).strip()
    if not key.isdigit():
        raise ToolFailure(f"Invalid activity_id {activity_id!r}: expected a numeric id.")
    return key


def drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def now_local_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


__all__ = [
    "Registry", "ToolFilter", "ToolFailure", "NoData", "WriteResult", "READ", "WRITE", "DESTRUCTIVE", "IDEMPOTENT_WRITE",
    "parse_date", "parse_range", "iso_days", "activity_key", "drop_none", "json_text", "CHARACTER_LIMIT",
]

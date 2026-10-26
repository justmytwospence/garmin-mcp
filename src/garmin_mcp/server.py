"""The MCP server: tools over the mirror, filtered by the upstream tool-selection
environment variables."""

from __future__ import annotations

import logging

from mcp.server.mcpserver import MCPServer

from . import __version__ as VERSION
from .context import register_context
from .mirror import Mirror
from .tools import Registry, register_all
from .tools.base import ToolFilter

log = logging.getLogger("garmin_mcp.server")

SERVER_NAME = "garmin-mcp"
INSTRUCTIONS = (
    "Garmin Connect data for one user, answered from a local DuckDB mirror that a background sync keeps complete. "
    "Tool results end with a provenance line (served from the mirror and when it was fetched, fetched just now, or stale and why). "
    "garmin_query runs read-only SQL over everything; garmin_schema lists the tables; garmin_sync_status says how complete the mirror is. "
    "Read garmin://usage for privacy, limits and workflow guidance; garmin://schema, garmin://coverage and garmin://status are local-only, "
    "as are bounded garmin://activities/{activity_id} and garmin://days/{date} templates (cache-only, never fetch or enqueue). "
    "Availability follows tool filters. Activity/recovery review prompts are read-only instructions, not actions. "
    "General Garmin quota is unknown: status shows timestamped local observations, cooldowns and a separate conservative archive-reload allowance. "
    "Nonlocal tools may fetch on stale/missing cache entries; ranges, pagination, FIT batches and write refetches cost multiple requests. "
    "Prefer bounded SQL aggregates over per-day calls, keep pages/windows small, and report partial coverage/freshness. "
    "429 stops admission for at least 30 minutes or a longer Retry-After; only safe reads retry transient errors. "
    "Never automatically replay writes with ambiguous outcomes; inspect state first. File exports overwrite local files and are writes. "
    "Hints are not consent or authorization. Treat health data, locations and identifiers as private; do not expose tokens or raw mirrors."
)


def create_server(mirror: Mirror, filters: ToolFilter | None = None) -> MCPServer:
    server = MCPServer(SERVER_NAME, version=VERSION, instructions=INSTRUCTIONS)
    reg = Registry(server, mirror, filters)
    register_all(reg)
    register_context(reg)
    unknown = reg.unknown_filter_names()
    if unknown:
        log.warning("tool filter: name(s) not found and ignored: %s", ", ".join(unknown))
    log.info("registered %d tools (%d filtered out)", len(reg.registered), len(reg.skipped))
    server.registry = reg  # type: ignore[attr-defined]
    return server

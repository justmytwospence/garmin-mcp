"""The MCP server: tools over the mirror, filtered by the upstream tool-selection
environment variables."""

from __future__ import annotations

import logging

from mcp.server.mcpserver import MCPServer

from .mirror import Mirror
from .tools import Registry, register_all
from .tools.base import ToolFilter

log = logging.getLogger("garmin_mcp.server")

SERVER_NAME = "garmin-mcp"
VERSION = "0.1.0"
INSTRUCTIONS = (
    "Garmin Connect data for one user, answered from a local DuckDB mirror that a background sync keeps complete. "
    "Tool results end with a provenance line (served from the mirror and when it was fetched, fetched just now, or stale and why). "
    "garmin_query runs read-only SQL over everything; garmin_schema lists the tables; garmin_sync_status says how complete the mirror is. "
    "Garmin's API is unofficial and rate limited: prefer the mirror tools over many per-day calls."
)


def create_server(mirror: Mirror, filters: ToolFilter | None = None) -> MCPServer:
    server = MCPServer(SERVER_NAME, version=VERSION, instructions=INSTRUCTIONS)
    reg = Registry(server, mirror, filters)
    register_all(reg)
    unknown = reg.unknown_filter_names()
    if unknown:
        log.warning("tool filter: name(s) not found and ignored: %s", ", ".join(unknown))
    log.info("registered %d tools (%d filtered out)", len(reg.registered), len(reg.skipped))
    server.registry = reg  # type: ignore[attr-defined]
    return server

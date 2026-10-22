"""The HTTP surface: the MCP Streamable HTTP endpoint at /mcp and /, GET /health, and
the snapshot at /snapshot.duckdb (bearer SNAPSHOT_TOKEN, byte ranges). An optional
MCP_BEARER_TOKEN guards the MCP endpoint; MCP_ALLOWED_HOSTS turns on the SDK's Host
header check (DNS-rebinding protection)."""

from __future__ import annotations

import hmac
import json
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .snapshot import SnapshotPublisher

MCP_PATHS = ("/mcp", "/")


def bearer_matches(token: str, header: str | None) -> bool:
    expected = f"Bearer {token}".encode()
    given = (header or "").encode()
    return len(given) == len(expected) and hmac.compare_digest(given, expected)


def transport_security(allowed_hosts: list[str]) -> TransportSecuritySettings:
    if not allowed_hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    hosts: list[str] = []
    for h in ["localhost", "127.0.0.1", *allowed_hosts]:
        hosts.append(h)
        if ":" not in h:
            hosts.append(f"{h}:*")
    origins = [f"{scheme}://{h}" for h in hosts for scheme in ("http", "https")]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


def build_app(
    server: MCPServer,
    *,
    snapshot: SnapshotPublisher | None,
    health: dict[str, Any] | None = None,
    allowed_hosts: list[str] | None = None,
    mcp_token: str = "",
    snapshot_token: str = "",
    stateless: bool = False,
) -> ASGIApp:
    inner = server.streamable_http_app(streamable_http_path="/mcp", host="0.0.0.0", transport_security=transport_security(allowed_hosts or []), stateless_http=stateless)
    health_body = health or {"status": "ok"}

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await inner(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if path == "/health" and method == "GET":
            await JSONResponse(health_body)(scope, receive, send)
            return
        if path == "/snapshot.duckdb" and method in ("GET", "HEAD"):
            request = Request(scope, receive)
            if not snapshot_token or snapshot is None:
                await Response("Snapshot serving is disabled (set SNAPSHOT_TOKEN)", status_code=404, media_type="text/plain")(scope, receive, send)
                return
            if not bearer_matches(snapshot_token, request.headers.get("authorization")):
                await Response("Unauthorized", status_code=401, media_type="text/plain")(scope, receive, send)
                return
            await snapshot.serve(request)(scope, receive, send)
            return
        if path in MCP_PATHS:
            if mcp_token:
                request = Request(scope, receive)
                if not bearer_matches(mcp_token, request.headers.get("authorization")):
                    await Response(json.dumps({"error": "unauthorized"}), status_code=401, media_type="application/json")(scope, receive, send)
                    return
            if path == "/":
                scope = dict(scope)
                scope["path"] = "/mcp"
                scope["raw_path"] = b"/mcp"
            await inner(scope, receive, send)
            return
        await Response("Not found", status_code=404, media_type="text/plain")(scope, receive, send)

    return app

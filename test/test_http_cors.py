"""CORS on the canonical export routes only."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from garmin_mcp.export import CanonicalExporter
from garmin_mcp.http import build_app

ORIGIN = "https://effortcurve.com"


def make(tmp_path: Path):
    out = tmp_path / "canonical-v1"
    out.mkdir()
    (out / "manifest.json").write_text('{"schema_version":1,"activities":[]}')
    exporter = CanonicalExporter(out, importer="test")
    from mcp.server.mcpserver import MCPServer

    return build_app(MCPServer("t"), snapshot=None, exporter=exporter, export_token="tok", mcp_token="mcp", export_cors_origins=[ORIGIN])


def run(app, fn):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            return await fn(c)

    return asyncio.run(go())


URL = "/export/canonical/v1/manifest.json"


def test_preflight_allowed(tmp_path):
    async def fn(c):
        r = await c.options(URL, headers={"origin": ORIGIN, "access-control-request-method": "GET"})
        assert r.status_code == 204
        h = r.headers
        assert h["access-control-allow-origin"] == ORIGIN
        assert "Authorization" in h["access-control-allow-headers"]
        assert "GET" in h["access-control-allow-methods"] and h["access-control-max-age"] == "86400" and h["vary"] == "Origin"
        assert "access-control-allow-private-network" not in h
        r = await c.options(URL, headers={"origin": ORIGIN, "access-control-request-private-network": "true"})
        assert r.headers["access-control-allow-private-network"] == "true"

    run(make(tmp_path), fn)


def test_preflight_disallowed(tmp_path):
    async def fn(c):
        r = await c.options(URL, headers={"origin": "https://evil.example"})
        assert r.status_code == 204 and "access-control-allow-origin" not in r.headers

    run(make(tmp_path), fn)


def test_get_carries_cors(tmp_path):
    async def fn(c):
        bad = await c.get(URL, headers={"origin": ORIGIN, "authorization": "Bearer nope"})
        assert bad.status_code == 401 and bad.headers["access-control-allow-origin"] == ORIGIN
        ok = await c.get(URL, headers={"origin": ORIGIN, "authorization": "Bearer tok"})
        assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == ORIGIN
        other = await c.get(URL, headers={"origin": "https://evil.example", "authorization": "Bearer tok"})
        assert "access-control-allow-origin" not in other.headers

    run(make(tmp_path), fn)


def test_other_routes_have_no_cors(tmp_path):
    async def fn(c):
        for path in ("/mcp", "/snapshot.duckdb", "/health"):
            r = await c.get(path, headers={"origin": ORIGIN})
            assert "access-control-allow-origin" not in r.headers

    run(make(tmp_path), fn)

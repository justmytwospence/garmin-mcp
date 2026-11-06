"""Signing in to the canonical export: built-in OAuth (PKCE) and OIDC JWTs."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.mcpserver import MCPServer

from garmin_mcp.export import CanonicalExporter
from garmin_mcp.export_auth import ExportAuth
from garmin_mcp.http import build_app

ORIGIN = "https://effortcurve.com"
CB = ORIGIN + "/oauth/callback"
BASE = "http://test/export/canonical/v1"


def b64(d: bytes) -> str:
    return base64.urlsafe_b64encode(d).rstrip(b"=").decode()


def make(tmp_path: Path, auth: ExportAuth):
    out = tmp_path / "canonical-v1"
    out.mkdir(exist_ok=True)
    (out / "manifest.json").write_text('{"schema_version":1,"activities":[]}')
    return build_app(MCPServer("t"), snapshot=None, exporter=CanonicalExporter(out, importer="t"), export_token="tok", export_cors_origins=[ORIGIN], export_auth=auth)


def run(app, fn):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            return await fn(c)

    return asyncio.run(go())


def q(verifier="v" * 43):
    return {"response_type": "code", "client_id": "effortcurve", "redirect_uri": CB, "state": "s1", "code_challenge": b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}


def test_metadata_and_cors(tmp_path):
    async def fn(c):
        r = await c.get("/export/canonical/v1/auth", headers={"origin": ORIGIN})
        m = r.json()
        assert r.headers["access-control-allow-origin"] == ORIGIN
        assert m["mode"] == "builtin" and m["client_id"] == "effortcurve"
        assert m["authorization_endpoint"] == BASE + "/oauth/authorize" and m["token_endpoint"] == BASE + "/oauth/token"

    run(make(tmp_path, ExportAuth("tok", [ORIGIN])), fn)


def test_authorize_page_and_bad_redirects(tmp_path):
    async def fn(c):
        r = await c.get("/export/canonical/v1/oauth/authorize", params=q())
        assert r.status_code == 200 and "default-src 'none'" in r.headers["content-security-policy"]
        assert f"form-action 'self' {ORIGIN}" in r.headers["content-security-policy"] and 'type="password"' in r.text
        for bad in ("https://evil.example/oauth/callback", ORIGIN + "/other", ORIGIN + "/oauth/callback?x=1"):
            r = await c.get("/export/canonical/v1/oauth/authorize", params={**q(), "redirect_uri": bad})
            assert r.status_code == 400

    run(make(tmp_path, ExportAuth("tok", [ORIGIN])), fn)


def test_wrong_token_and_deny(tmp_path):
    async def fn(c):
        r = await c.post("/export/canonical/v1/oauth/authorize", data={**q(), "token": "nope", "action": "approve"})
        assert r.status_code == 401 and "code=" not in r.text
        assert f"form-action 'self' {ORIGIN}" in r.headers["content-security-policy"]
        r = await c.post("/export/canonical/v1/oauth/authorize", data={**q(), "action": "deny"})
        assert r.status_code == 303 and "error=access_denied" in r.headers["location"]

    import time as _t

    orig = _t.sleep
    _t.sleep = lambda s: None
    try:
        run(make(tmp_path, ExportAuth("tok", [ORIGIN])), fn)
    finally:
        _t.sleep = orig


def flow(c, verifier="v" * 43):
    async def go():
        r = await c.post("/export/canonical/v1/oauth/authorize", data={**q(verifier), "token": "tok", "action": "approve"})
        assert r.status_code == 303
        loc = urlparse(r.headers["location"])
        qs = parse_qs(loc.query)
        assert qs["state"] == ["s1"]
        return qs["code"][0]

    return go()


def test_code_flow_access_refresh_and_legacy_token(tmp_path):
    clock = [1_000_000.0]
    auth = ExportAuth("tok", [ORIGIN], now=lambda: clock[0])

    async def fn(c):
        tok = lambda **d: c.post("/export/canonical/v1/oauth/token", data=d)  # noqa: E731
        code = await flow(c)
        bad = await tok(grant_type="authorization_code", code=code, redirect_uri=CB, code_verifier="w" * 43)
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
        bad = await tok(grant_type="authorization_code", code=code, redirect_uri=ORIGIN + "/x", code_verifier="v" * 43)
        assert bad.status_code == 400
        ok = await tok(grant_type="authorization_code", code=code, redirect_uri=CB, code_verifier="v" * 43)
        assert ok.status_code == 200
        t = ok.json()
        assert (await c.get("/export/canonical/v1/manifest.json", headers={"authorization": "Bearer " + t["access_token"]})).status_code == 200
        flipped = t["access_token"][:-1] + ("A" if t["access_token"][-1] != "A" else "B")
        assert (await c.get("/export/canonical/v1/manifest.json", headers={"authorization": "Bearer " + flipped})).status_code == 401
        assert (await c.get("/export/canonical/v1/manifest.json", headers={"authorization": "Bearer " + t["refresh_token"]})).status_code == 401
        assert (await c.get("/export/canonical/v1/manifest.json", headers={"authorization": "Bearer tok"})).status_code == 200
        assert (await c.get("/export/canonical/v1/manifest.json")).status_code == 401
        r = await tok(grant_type="refresh_token", refresh_token=t["refresh_token"])
        assert r.status_code == 200 and r.json()["access_token"] != t["access_token"]
        clock[0] += 91 * 86400
        assert (await tok(grant_type="refresh_token", refresh_token=t["refresh_token"])).status_code == 400
        code2 = None
        assert code2 is None

    run(make(tmp_path, auth), fn)


def test_expired_code(tmp_path):
    clock = [1_000_000.0]

    async def fn(c):
        code = await flow(c)
        clock[0] += 200
        r = await c.post("/export/canonical/v1/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": CB, "code_verifier": "v" * 43})
        assert r.status_code == 400

    run(make(tmp_path, ExportAuth("tok", [ORIGIN], now=lambda: clock[0])), fn)


class Jwks:
    def __init__(self, key):
        self.key = key

    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": self.key.public_key()})()


def test_oidc_mode(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    iss = "https://auth.example.com"
    disc = {"authorization_endpoint": iss + "/authorize", "token_endpoint": iss + "/api/oidc/token", "jwks_uri": iss + "/jwks"}
    auth = ExportAuth("tok", [ORIGIN], oidc_issuer=iss, fetch_json=lambda u: disc, jwks=Jwks(key))
    import time

    def mint(**over):
        claims = {"iss": iss, "exp": int(time.time()) + 600, "client_id": "effortcurve", **over}
        return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256")

    async def fn(c):
        get = lambda t: c.get("/export/canonical/v1/manifest.json", headers={"authorization": "Bearer " + t})  # noqa: E731
        assert (await get(mint())).status_code == 200
        assert (await get(mint(client_id=None, azp="effortcurve"))).status_code == 200
        assert (await get(mint(client_id="other"))).status_code == 401
        assert (await get(mint(exp=int(time.time()) - 10))).status_code == 401
        assert (await get(mint(iss="https://evil.example"))).status_code == 401
        m = (await c.get("/export/canonical/v1/auth")).json()
        assert m["mode"] == "oidc" and m["token_endpoint"] == disc["token_endpoint"] and m["client_id"] == "effortcurve"
        assert (await c.get("/export/canonical/v1/oauth/authorize", params=q())).status_code == 404

    run(make(tmp_path, auth), fn)

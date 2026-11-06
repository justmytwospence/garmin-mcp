"""Who may read the canonical export: the static EXPORT_TOKEN, tokens this server issues after an OAuth sign-in
(built-in mode: the user approves by entering EXPORT_TOKEN once on /oauth/authorize), or, with EXPORT_OIDC_ISSUER set,
JWTs from that identity provider issued to EXPORT_OIDC_CLIENT_ID (OIDC mode). The browser app runs the same
authorization-code + PKCE flow against either."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import secrets
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

CLIENT_ID = "effortcurve"
CODE_TTL, ACCESS_TTL, REFRESH_TTL = 120, 3600, 90 * 86400


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _default_fetch(url: str) -> dict[str, Any]:
    import requests

    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


class ExportAuth:
    def __init__(self, export_token: str, cors_origins: list[str], oidc_issuer: str = "", oidc_client_id: str = CLIENT_ID, fetch_json: Callable[[str], dict[str, Any]] | None = None, now: Callable[[], float] = time.time, jwks: Any = None) -> None:
        self.export_token = export_token
        self.cors_origins = set(cors_origins)
        self.issuer = oidc_issuer
        self.client_id = oidc_client_id
        self.fetch_json = fetch_json or _default_fetch
        self.now = now
        self._jwks = jwks  # a PyJWKClient-like object with get_signing_key_from_jwt
        self._disc: tuple[float, dict[str, Any]] | None = None
        self.mode = "oidc" if oidc_issuer else "builtin"

    # ------------------------------------------------------------ tokens
    def _key(self) -> bytes:
        return hmac.new(self.export_token.encode(), b"effortcurve-oauth-v1", hashlib.sha256).digest()

    def issue(self, kind: str, ttl: int, **extra: Any) -> str:
        payload = _b64(json.dumps({"t": kind, "exp": int(self.now()) + ttl, "n": secrets.token_hex(16), **extra}, separators=(",", ":")).encode())
        sig = _b64(hmac.new(self._key(), f"ec1.{payload}".encode(), hashlib.sha256).digest())
        return f"ec1.{payload}.{sig}"

    def check(self, token: str, kind: str) -> dict[str, Any] | None:
        try:
            prefix, payload, sig = token.split(".")
            if prefix != "ec1":
                return None
            want = hmac.new(self._key(), f"ec1.{payload}".encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(want, _unb64(sig)):
                return None
            data = json.loads(_unb64(payload))
            if data.get("t") != kind or float(data["exp"]) < self.now():
                return None
            return data
        except Exception:  # noqa: BLE001
            return None

    def allowed_redirect(self, uri: str) -> bool:
        try:
            u = urlparse(uri)
            if u.path != "/oauth/callback" or u.query or u.fragment or not u.hostname:
                return False
            if u.scheme == "http" and u.hostname != "localhost":
                return False
            if u.scheme not in ("http", "https"):
                return False
            return f"{u.scheme}://{u.netloc}" in self.cors_origins
        except ValueError:
            return False

    # ------------------------------------------------------------ OIDC
    def _discovery(self) -> dict[str, Any]:
        if self._disc and self._disc[0] > self.now():
            return self._disc[1]
        d = self.fetch_json(self.issuer.rstrip("/") + "/.well-known/openid-configuration")
        self._disc = (self.now() + 3600, d)
        return d

    def _jwk_client(self) -> Any:
        if self._jwks is None:
            import jwt

            self._jwks = jwt.PyJWKClient(self._discovery()["jwks_uri"], cache_keys=True, lifespan=3600)
        return self._jwks

    def metadata(self, base_url: str) -> dict[str, Any]:
        if self.mode == "oidc":
            d = self._discovery()
            return {"mode": "oidc", "issuer": self.issuer, "authorization_endpoint": d["authorization_endpoint"], "token_endpoint": d["token_endpoint"], "client_id": self.client_id, "scope": "openid offline_access"}
        return {"mode": "builtin", "issuer": base_url + "/oauth", "authorization_endpoint": base_url + "/oauth/authorize", "token_endpoint": base_url + "/oauth/token", "client_id": CLIENT_ID, "scope": "export"}

    def allows(self, header: str | None) -> bool:
        from .http import bearer_matches

        if not header or not header.startswith("Bearer "):
            return False
        if self.export_token and bearer_matches(self.export_token, header):
            return True
        tok = header[7:]
        if tok.startswith("ec1.") and self.export_token:
            return self.check(tok, "access") is not None
        if self.mode == "oidc":
            try:
                import jwt

                key = self._jwk_client().get_signing_key_from_jwt(tok)
                claims = jwt.decode(tok, getattr(key, "key", key), algorithms=["RS256", "ES256", "EdDSA"], issuer=self.issuer, options={"verify_aud": False})
                return claims.get("client_id") == self.client_id or claims.get("azp") == self.client_id
            except Exception:  # noqa: BLE001
                return False
        return False

    # ------------------------------------------------------------ built-in endpoints
    def _page(self, p: dict[str, str], error: str = "", status: int = 200) -> Response:
        origin = urlparse(p["redirect_uri"]).netloc
        scheme = urlparse(p["redirect_uri"]).scheme
        hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(v, quote=True)}">' for k, v in p.items())
        err = f'<p class="e">{html.escape(error)}</p>' if error else ""
        body = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in</title>
<style>body{{font:16px system-ui;max-width:26rem;margin:12vh auto;padding:0 1rem}}input[type=password]{{width:100%;padding:.5rem;margin:.5rem 0}}button{{padding:.5rem 1rem;margin-right:.5rem}}.e{{color:#b00020}}</style>
<h1>Effort Curve</h1><p><strong>{html.escape(scheme + '://' + origin)}</strong> wants to read your activities from this server.</p>{err}
<form method="post">{hidden}<label>Export token<input type="password" name="token" autocomplete="off" autofocus></label>
<button name="action" value="approve">Approve</button><button name="action" value="deny">Deny</button></form>"""
        return HTMLResponse(body, status_code=status, headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})

    @staticmethod
    def _bad(msg: str) -> Response:
        return HTMLResponse(f"<!doctype html><meta charset=utf-8><p>{html.escape(msg)}</p>", status_code=400)

    async def authorize(self, request: Request) -> Response:
        if self.mode != "builtin":
            return Response("Not found", status_code=404, media_type="text/plain")
        if request.method == "POST":
            form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        else:
            form = dict(request.query_params)
        p = {k: form.get(k, "") for k in ("response_type", "client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method")}
        if p["response_type"] != "code" or p["client_id"] != CLIENT_ID or p["code_challenge_method"] != "S256" or not p["code_challenge"] or not p["state"]:
            return self._bad("Invalid sign-in request.")
        if not self.allowed_redirect(p["redirect_uri"]):
            return self._bad("This site is not in EXPORT_CORS_ORIGINS on this server.")
        if request.method != "POST":
            return self._page(p)
        if form.get("action") == "deny":
            return RedirectResponse(p["redirect_uri"] + "?" + urlencode({"error": "access_denied", "state": p["state"]}), status_code=303)
        from .http import bearer_matches

        if not self.export_token or not bearer_matches(self.export_token, f"Bearer {form.get('token', '')}"):
            time.sleep(1)
            return self._page(p, "That is not this server's export token.", 401)
        code = self.issue("code", CODE_TTL, cc=p["code_challenge"], ru=p["redirect_uri"])
        return RedirectResponse(p["redirect_uri"] + "?" + urlencode({"code": code, "state": p["state"]}), status_code=303)

    async def token(self, request: Request) -> Response:
        if self.mode != "builtin":
            return Response("Not found", status_code=404, media_type="text/plain")
        h = {"Cache-Control": "no-store"}
        f = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        gt = f.get("grant_type")
        if gt == "authorization_code":
            c = self.check(f.get("code", ""), "code")
            ok = c is not None and c["ru"] == f.get("redirect_uri") and hmac.compare_digest(_b64(hashlib.sha256(f.get("code_verifier", "").encode()).digest()), c["cc"])
        elif gt == "refresh_token":
            ok = self.check(f.get("refresh_token", ""), "refresh") is not None
        else:
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400, headers=h)
        if not ok:
            return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=h)
        return JSONResponse({"access_token": self.issue("access", ACCESS_TTL), "token_type": "Bearer", "expires_in": ACCESS_TTL, "refresh_token": self.issue("refresh", REFRESH_TTL), "scope": "export"}, headers=h)

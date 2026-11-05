"""Garmin's official Personal API: a user-created API key is exchanged for a short-lived bearer token, and the
same service paths as Connect answer from personal.connectapi.garmin.com. Read-only and partial: only the datasets
in SUPPORTED_DATASETS (and the activity list and original FIT file) are served; the sync engine skips the rest."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import requests

from .client import AuthError, Response, Unsupported
from .datasets import ACTIVITY_FIT_PATH, ACTIVITY_LIST_PATH, BY_NAME

DEFAULT_BASE = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
SUPPORTED_DATASETS = frozenset({"activity", "daily_summary_v2", "steps_daily", "hrv", "hrv_summary", "body_battery", "weigh_ins", "userprofile_settings"})
SUPPORTED_PATHS = frozenset({ACTIVITY_LIST_PATH, ACTIVITY_FIT_PATH, *(BY_NAME[n].path for n in SUPPORTED_DATASETS)})
REFRESH_WINDOW_S = 3600
FIX = "Create a new key at https://connect.garmin.com/app/settings/personalApiToken, set GARMIN_PERSONAL_API_KEY, and restart."


def _exp(jwt: str) -> float:
    try:
        payload = jwt.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"])
    except (IndexError, KeyError, ValueError, TypeError) as err:
        raise AuthError("Garmin's token response had no expiry") from err


class PersonalToken:
    """Fetches and caches the bearer token for one API key."""

    def __init__(self, key: str, cache_file: Path, base: str = DEFAULT_BASE, session: Any = None, clock: Any = time.time) -> None:
        self.key = key
        self.cache_file = Path(cache_file)
        self.base = base.rstrip("/")
        self.session = session or requests
        self.clock = clock
        self.fingerprint = hashlib.sha256(f"{self.base}\0{key}".encode()).hexdigest()

    def _cached(self) -> str | None:
        try:
            c = json.loads(self.cache_file.read_text())
        except (OSError, ValueError):
            return None
        if c.get("fingerprint") == self.fingerprint and float(c.get("expires_at", 0)) - self.clock() > REFRESH_WINDOW_S:
            return str(c["access_token"])
        return None

    def get(self, force: bool = False) -> str:
        if not force and (tok := self._cached()):
            return tok
        r = self.session.post(self.base + TOKEN_PATH, headers={"Authorization": f"ApiKey {self.key}", "Accept": "*/*"}, timeout=60)
        if r.status_code in (401, 403):
            raise AuthError(f"Garmin rejected the Personal API key (HTTP {r.status_code})", r.status_code)
        if r.status_code >= 400:
            raise AuthError(f"Garmin's token endpoint answered HTTP {r.status_code}", r.status_code)
        tok = r.text.strip()
        exp = _exp(tok)
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.cache_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"fingerprint": self.fingerprint, "access_token": tok, "expires_at": exp}, f)
        os.chmod(self.cache_file, 0o600)
        return tok


class PersonalApiTransport:
    display_name: str | None = "personal-api"
    auth_fix_text = FIX
    partial = True

    def __init__(self, token: PersonalToken, session: Any = None) -> None:
        self.token = token
        self.session = session or requests

    def supports(self, path_template: str) -> bool:
        return path_template in SUPPORTED_PATHS

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None, binary: bool = False, headers: dict[str, str] | None = None, files: Any = None) -> Response:
        if method.upper() != "GET":
            raise Unsupported(f"{method} {path} is not available with the Personal API transport")
        accept = "application/zip, application/octet-stream" if binary else "application/json"
        for attempt in (0, 1):
            h = {**(headers or {}), "Authorization": f"Bearer {self.token.get(force=attempt == 1)}", "Accept": accept}
            r = self.session.get(self.token.base + path, params=params, headers=h, timeout=60)
            if r.status_code != 401 or attempt == 1:
                break
        return Response(r.status_code, r.content, r.headers.get("content-type", ""), dict(r.headers))

    def call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        raise Unsupported(f"{method_name} is not available with the Personal API transport")

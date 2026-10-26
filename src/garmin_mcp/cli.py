"""Entry point: open the mirror, log in (in the background), start the sync engine and
the snapshot publisher, and serve the MCP server over stdio (default) or Streamable
HTTP (MCP_TRANSPORT=http). `garmin-mcp import-export <zip>` ingests a Garmin account
data export into the FIT tables without any API call.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .db.database import Database, DatabaseOptions, exit_on_fatal
from .env import env_bool, env_int
from .garmin.auth import LoginManager, PendingTransport, is_cn, token_dir
from .garmin.client import GarminClient, RequestSample
from .mirror import Mirror
from .server import SERVER_NAME, VERSION, create_server
from .snapshot import SnapshotPublisher
from .strava_sync import StravaSyncConfig
from .sync.engine import SyncEngine
from .tools.base import ToolFilter

log = logging.getLogger("garmin_mcp")


@dataclass
class Runtime:
    db: Database
    client: GarminClient
    engine: SyncEngine
    mirror: Mirror
    snapshot: SnapshotPublisher
    login: LoginManager | None
    sync_enabled: bool
    db_path: str

    def close(self) -> None:
        self.snapshot.stop()
        self.engine.stop()
        self.db.close()


def default_db_path(env: dict[str, str] | None = None) -> str:
    """Where the mirror lives when GARMIN_DB_PATH is unset: the user's data directory
    (the image sets GARMIN_DB_PATH=/data/garmin.duckdb)."""
    env = os.environ if env is None else env
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(env.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return str(base / "garmin-mcp" / "garmin.duckdb")


def validate_settings(env: dict[str, str]) -> None:
    """Parse every setting that can be wrong, so a bad value is one line on stderr, not a traceback."""
    ToolFilter.from_env(env)
    StravaSyncConfig.from_env(env)
    env_bool("GARMIN_SYNC", True, env)
    env_bool("MCP_STATELESS_HTTP", False, env)
    is_cn(env)
    env_int("DUCKDB_THREADS", 0, env, minimum=1)
    env_int("PORT", 8000, env, minimum=1)
    transport = (env.get("MCP_TRANSPORT") or "stdio").strip().lower()
    if transport not in ("stdio", "http"):
        raise ValueError(f'MCP_TRANSPORT must be "stdio" or "http", got "{transport}"')
    if (env.get("GARMIN_HISTORY_START") or "").strip():
        try:
            date.fromisoformat(env["GARMIN_HISTORY_START"].strip())
        except ValueError as err:
            raise ValueError(f"GARMIN_HISTORY_START must be a date (YYYY-MM-DD), got {env['GARMIN_HISTORY_START']!r}") from err


def open_runtime(env: dict[str, str] | None = None, *, start: bool = True) -> Runtime:
    env = os.environ if env is None else env
    db_path = env.get("GARMIN_DB_PATH") or default_db_path(env)
    data_dir = str(Path(db_path).resolve().parent)
    snapshot_dir = env.get("GARMIN_SNAPSHOT_DIR") or str(Path(data_dir) / "snapshot")
    threads = env_int("DUCKDB_THREADS", 0, env, minimum=1)
    db = Database.open(DatabaseOptions(db_path, snapshot_dir, env.get("DUCKDB_MEMORY_LIMIT") or None, threads or None))
    # A fatal DuckDB error leaves the mirror unusable: end the process so the container restarts.
    db.on_fatal = exit_on_fatal

    def record(s: RequestSample) -> None:
        try:
            db.run("INSERT INTO request_log VALUES (?, ?, ?, ?, ?, ?, ?)", [s.observed_at, s.method, s.path, s.status, s.priority, s.ms, s.bytes])
        except Exception:  # noqa: BLE001
            log.exception("request_log insert failed")

    client = GarminClient(PendingTransport(), on_sample=record)
    snapshot = SnapshotPublisher(db)
    engine = SyncEngine(db, client, data_dir, on_change=snapshot.mark_dirty, history_start=env.get("GARMIN_HISTORY_START") or None, strava=StravaSyncConfig.from_env(env))
    mirror = Mirror(db, engine, client, tokenstore=token_dir(env))
    sync_enabled = env_bool("GARMIN_SYNC", True, env)
    login = LoginManager(client, token_dir(env), email=env.get("GARMIN_EMAIL") or None, password=env.get("GARMIN_PASSWORD") or None, cn=is_cn(env))
    rt = Runtime(db, client, engine, mirror, snapshot, login, sync_enabled, db_path)
    if start:
        login.start()
        if sync_enabled:
            engine.start()
            snapshot.start()
    return rt


def health_body(rt: Runtime) -> dict[str, Any]:
    strava = rt.engine.strava_sync
    return {"status": "ok", "name": SERVER_NAME, "version": VERSION, "sync": "on" if rt.sync_enabled else "off", "strava_sync": ",".join(strava.config.parts) if strava else "off"}


def serve(args: argparse.Namespace) -> int:
    env = os.environ
    try:
        validate_settings(env)
    except ValueError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    rt = open_runtime(env)
    server = create_server(rt.mirror, ToolFilter.from_env(env))
    transport = (env.get("MCP_TRANSPORT") or "stdio").strip().lower()
    sync_label = "on" if rt.sync_enabled else "off"
    try:
        if transport == "stdio":
            print(f"{SERVER_NAME} {VERSION} running via stdio (mirror: {rt.db_path}, sync {sync_label})", file=sys.stderr)
            server.run(transport="stdio")
        else:
            import uvicorn

            from .http import build_app

            port = env_int("PORT", 8000, env, minimum=1)
            host = env.get("MCP_HOST") or "0.0.0.0"
            allowed = [h.strip() for h in (env.get("MCP_ALLOWED_HOSTS") or "").split(",") if h.strip()]
            mcp_token = env.get("MCP_BEARER_TOKEN") or ""
            if not mcp_token:
                log.warning("MCP_BEARER_TOKEN is not set: anyone who can reach http://%s:%d/mcp can read this account's data and write to it. Set it, or bind MCP_HOST to a private address.", host, port)
            app = build_app(
                server,
                snapshot=rt.snapshot,
                health=health_body(rt),
                allowed_hosts=allowed,
                mcp_token=mcp_token,
                snapshot_token=env.get("SNAPSHOT_TOKEN") or "",
                stateless=env_bool("MCP_STATELESS_HTTP", False, env),
            )
            print(f"{SERVER_NAME} {VERSION} listening on http://{host}:{port}/mcp (mirror: {rt.db_path}, sync {sync_label})", file=sys.stderr)
            uvicorn.run(app, host=host, port=port, log_level=(env.get("LOG_LEVEL") or "info").lower(), lifespan="on")
    finally:
        rt.close()
    return 0


def import_export(args: argparse.Namespace) -> int:
    from .garmin.import_export import import_zip

    env = os.environ
    try:
        validate_settings(env)
    except ValueError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    rt = open_runtime(env, start=False)
    try:
        summary = import_zip(rt.engine, Path(args.zip), limit=args.limit)
    finally:
        rt.close()
    print(f"imported {summary['activity_files']} activity FIT files and {summary['wellness_files']} wellness files ({summary['skipped']} skipped, {summary['errors']} errors)")
    for line in summary["notes"]:
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("auth", "garmin-mcp-auth"):
        # The image's entrypoint is garmin-mcp, so `docker compose run --rm garmin-mcp garmin-mcp-auth` lands here.
        from .auth_cli import main as auth_main

        return auth_main(argv[1:])
    logging.basicConfig(level=(os.environ.get("LOG_LEVEL") or "INFO").upper(), format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    parser = argparse.ArgumentParser(prog="garmin-mcp", description="Garmin Connect MCP server backed by a DuckDB mirror", epilog="`garmin-mcp auth [--check]` runs garmin-mcp-auth (the one-time interactive login).")
    parser.add_argument("--version", action="version", version=f"{SERVER_NAME} {VERSION}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="serve MCP (default)")
    ie = sub.add_parser("import-export", help="ingest a Garmin account data export (zip) into the mirror's FIT tables without API calls")
    ie.add_argument("zip")
    ie.add_argument("--limit", type=int, default=None, help="stop after this many files (for a trial run)")
    args = parser.parse_args(argv)
    if args.command == "import-export":
        return import_export(args)
    def on_signal(signum: int, _frame: Any) -> None:
        raise SystemExit(0)

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, on_signal)
    return serve(args)


if __name__ == "__main__":
    sys.exit(main())

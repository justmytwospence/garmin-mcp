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
from pathlib import Path
from typing import Any

from .db.database import Database, DatabaseOptions, exit_on_fatal
from .garmin.auth import LoginManager, is_cn, token_dir
from .garmin.client import GarminClient, RequestSample
from .mirror import Mirror
from .server import SERVER_NAME, VERSION, create_server
from .snapshot import SnapshotPublisher
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


def env_flag(name: str, default: str = "on") -> bool:
    return (os.environ.get(name) or default).strip().lower() not in ("off", "0", "false", "no")


def open_runtime(env: dict[str, str] | None = None, *, start: bool = True) -> Runtime:
    env = os.environ if env is None else env
    db_path = env.get("GARMIN_DB_PATH") or "./state/garmin.duckdb"
    data_dir = str(Path(db_path).resolve().parent)
    snapshot_dir = env.get("GARMIN_SNAPSHOT_DIR") or str(Path(data_dir) / "snapshot")
    threads = env.get("DUCKDB_THREADS")
    db = Database.open(DatabaseOptions(db_path, snapshot_dir, env.get("DUCKDB_MEMORY_LIMIT") or None, int(threads) if threads else None))
    # A fatal DuckDB error leaves the mirror unusable: end the process so the container restarts.
    db.on_fatal = exit_on_fatal

    def record(s: RequestSample) -> None:
        try:
            db.run("INSERT INTO request_log VALUES (?, ?, ?, ?, ?, ?, ?)", [s.observed_at, s.method, s.path, s.status, s.priority, s.ms, s.bytes])
        except Exception:  # noqa: BLE001
            log.exception("request_log insert failed")

    from .garmin.auth import PendingTransport

    client = GarminClient(PendingTransport(), on_sample=record)
    snapshot = SnapshotPublisher(db)
    engine = SyncEngine(db, client, data_dir, on_change=snapshot.mark_dirty, history_start=env.get("GARMIN_HISTORY_START"))
    mirror = Mirror(db, engine, client)
    sync_enabled = env_flag("GARMIN_SYNC")
    login = LoginManager(client, token_dir(env), email=env.get("GARMIN_EMAIL") or None, password=env.get("GARMIN_PASSWORD") or None, cn=is_cn(env))
    rt = Runtime(db, client, engine, mirror, snapshot, login, sync_enabled, db_path)
    if start:
        login.start()
        if sync_enabled:
            engine.start()
            snapshot.start()
    return rt


def health_body(rt: Runtime) -> dict[str, Any]:
    return {"status": "ok", "name": SERVER_NAME, "version": VERSION, "sync": "on" if rt.sync_enabled else "off"}


def serve(args: argparse.Namespace) -> int:
    env = os.environ
    try:
        filters = ToolFilter.from_env(env)
    except ValueError as err:
        print(str(err), file=sys.stderr)
        return 1
    rt = open_runtime(env)
    server = create_server(rt.mirror, filters)
    transport = (env.get("MCP_TRANSPORT") or "stdio").strip().lower()
    sync_label = "on" if rt.sync_enabled else "off"
    try:
        if transport == "stdio":
            print(f"{SERVER_NAME} {VERSION} running via stdio (mirror: {rt.db_path}, sync {sync_label})", file=sys.stderr)
            server.run(transport="stdio")
        elif transport == "http":
            import uvicorn

            from .http import build_app

            port = int(env.get("PORT") or 8000)
            host = env.get("MCP_HOST") or "0.0.0.0"
            allowed = [h.strip() for h in (env.get("MCP_ALLOWED_HOSTS") or "").split(",") if h.strip()]
            app = build_app(
                server,
                snapshot=rt.snapshot,
                health=health_body(rt),
                allowed_hosts=allowed,
                mcp_token=env.get("MCP_BEARER_TOKEN") or "",
                snapshot_token=env.get("SNAPSHOT_TOKEN") or "",
                stateless=(env.get("MCP_STATELESS_HTTP") or "").lower() in ("1", "true", "yes", "on"),
            )
            print(f"{SERVER_NAME} {VERSION} listening on http://{host}:{port}/mcp (mirror: {rt.db_path}, sync {sync_label})", file=sys.stderr)
            uvicorn.run(app, host=host, port=port, log_level=(env.get("LOG_LEVEL") or "info").lower(), lifespan="on")
        else:
            print(f'ERROR: MCP_TRANSPORT must be "stdio" or "http", got "{transport}".', file=sys.stderr)
            return 1
    finally:
        rt.close()
    return 0


def import_export(args: argparse.Namespace) -> int:
    from .garmin.import_export import import_zip

    env = os.environ
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

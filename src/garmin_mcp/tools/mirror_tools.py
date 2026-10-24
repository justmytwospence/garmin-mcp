"""garmin_query, garmin_schema and garmin_sync_status: the mirror itself as a tool."""

from __future__ import annotations

import threading
import time
from typing import Any

import duckdb

from ..db.projections import drift
from ..mirror import Mirror, Provenance, jsonable, note_provenance
from .base import NoData, READ, Registry, ToolFailure

QUERY_TIMEOUT_S = 30.0
MAX_ROWS = 5000
# The drift check parses the newest stored payloads of every type; its answer is reused for an hour.
DRIFT_TTL_S = 3600.0


class DriftCache:
    def __init__(self, mirror: Mirror, ttl: float = DRIFT_TTL_S) -> None:
        self.mirror = mirror
        self.ttl = ttl
        self._at = 0.0
        self._value: list[dict] | None = None
        self._lock = threading.Lock()

    def get(self) -> list[dict]:
        with self._lock:
            if self._value is None or time.monotonic() - self._at > self.ttl:
                with self.mirror.db.write() as tx:
                    self._value = drift(tx)
                self._at = time.monotonic()
            return self._value

_READ_ONLY_TYPES = {"SELECT", "EXPLAIN"}


class ReadOnlyQuery:
    """One statement on its own cursor: parsed first, run only if it is a SELECT or
    EXPLAIN (DESCRIBE, SHOW, SUMMARIZE and FROM-first queries parse as SELECT), interrupted
    after the timeout. File access is already disabled database-wide."""

    def __init__(self, mirror: Mirror) -> None:
        self.mirror = mirror
        self._conn: duckdb.DuckDBPyConnection | None = None
        self._lock = threading.Lock()

    def connection(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            self._conn = self.mirror.db.connect()
        return self._conn

    @staticmethod
    def check(sql: str) -> None:
        try:
            statements = duckdb.extract_statements(sql)
        except duckdb.Error as err:
            raise ToolFailure(f"SQL parse error: {err}") from err
        if len(statements) != 1:
            raise ToolFailure(f"Send exactly one statement (got {len(statements)}).")
        kind = str(statements[0].type).rsplit(".", 1)[-1].upper()
        if kind not in _READ_ONLY_TYPES:
            raise ToolFailure(f"Only read-only queries are allowed (got {kind}).")

    def run(self, sql: str, max_rows: int) -> dict[str, Any]:
        self.check(sql)
        with self._lock:
            conn = self.connection()
            timer = threading.Timer(QUERY_TIMEOUT_S, conn.interrupt)
            timer.start()
            try:
                cur = conn.execute(sql)
                columns = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchmany(max_rows + 1)
            except duckdb.InterruptException as err:
                raise ToolFailure(f"Query interrupted after {QUERY_TIMEOUT_S:.0f} s; add filters or LIMIT.") from err
            except duckdb.Error as err:
                raise ToolFailure(f"Query failed: {err}") from err
            finally:
                timer.cancel()
        out = [dict(zip(columns, (jsonable(v) for v in r), strict=True)) for r in rows[:max_rows]]
        return {"columns": columns, "rows": out, "row_count": len(out), "truncated": len(rows) > max_rows}


def register(reg: Registry) -> None:
    m = reg.mirror
    query = ReadOnlyQuery(m)
    drift_cache = DriftCache(m)

    @reg.tool(annotations=READ)
    def garmin_query(sql: str, max_rows: int = 200) -> Any:
        """Run a read-only DuckDB SQL query against the local mirror of the user's Garmin Connect data. Costs no Garmin requests. Use garmin_schema first to see the tables.

        Main tables: activity (one row per activity, summary and detail merged), activity_lap, activity_typed_split, activity_time_in_zone,
        activity_weather, activity_gear, activity_exercise_set; fit_session/fit_lap/fit_record (the original FIT file, full-resolution
        samples with heart_rate, power, cadence, speed, altitude, position); daily_summary, heart_rate_sample, stress_sample,
        body_battery_sample, respiration_sample, spo2_day, floors_interval, steps_interval, sleep, sleep_level, sleep_sample, hrv, hrv_reading,
        training_readiness, training_status, max_metrics, endurance_score, hill_score, fitness_age, weigh_in, blood_pressure, hydration,
        user_profile, user_settings, device, gear, heart_rate_zones, power_zones, personal_record, badge, goal, workout, calendar_item, course.
        raw_current holds every payload as JSON (column body) for fields not in a typed table. Units: meters, seconds, m/s, bpm, watts.

        Args:
            sql: One DuckDB SELECT statement (DESCRIBE, SUMMARIZE and EXPLAIN also work).
            max_rows: Maximum rows to return (default 200, max 5000).
        """
        max_rows = max(1, min(int(max_rows), MAX_ROWS))
        result = query.run(sql, max_rows)
        note_provenance(Provenance("mirror", None, notes=["SQL over the local mirror; no Garmin request"]))
        if result.pop("truncated"):
            result["note"] = f"Truncated to {max_rows} rows; aggregate or add LIMIT/OFFSET."
        return result

    @reg.tool(annotations=READ)
    def garmin_schema(table: str | None = None) -> Any:
        """List the mirror's tables and views with their descriptions and estimated row counts, or, given a table name, its columns with types, units and descriptions. Use before garmin_query.

        Args:
            table: A table or view name; omit to list them all.
        """
        note_provenance(Provenance("mirror", None, notes=["catalog of the local mirror; no Garmin request"]))
        db = m.db
        if not table:
            tables = db.all(
                """SELECT table_name AS name, 'table' AS kind, estimated_size::BIGINT AS approx_rows, comment
                   FROM duckdb_tables() WHERE schema_name = 'main' AND NOT internal AND NOT temporary
                   UNION ALL
                   SELECT view_name, 'view', NULL, comment FROM duckdb_views() WHERE schema_name = 'main' AND NOT internal
                   ORDER BY kind, name"""
            )
            return {"tables": tables}
        columns = db.all(
            "SELECT column_name AS name, data_type AS type, comment FROM duckdb_columns() WHERE schema_name = 'main' AND table_name = ? ORDER BY column_index",
            [table],
        )
        if not columns:
            raise NoData(f"No table or view named {table}. Call garmin_schema without arguments to list them.")
        meta = db.one(
            "SELECT comment FROM duckdb_tables() WHERE schema_name = 'main' AND table_name = ? UNION ALL SELECT comment FROM duckdb_views() WHERE schema_name = 'main' AND view_name = ?",
            [table, table],
        )
        return {"table": table, "comment": (meta or {}).get("comment"), "columns": columns}

    @reg.tool(annotations=READ)
    def garmin_sync_status() -> Any:
        """How complete and fresh the local mirror of Garmin Connect is: coverage per dataset, the sync queue by kind, last run of each periodic task, recent errors, pacing and backoff state, the archive reload quota, the login state, and any fields Garmin returns that the schema does not yet map."""
        note_provenance(Provenance("mirror", None, notes=["sync bookkeeping of the local mirror; no Garmin request"]))
        status = m.engine.status()
        status["login"] = login_state(m)
        status["sync_worker"] = "running" if m.engine._running else "stopped"
        status["unmapped_fields"] = drift_cache.get()
        return jsonable(status)


def login_state(m: Mirror) -> dict[str, Any]:
    s = m.client.state
    if s.auth_error:
        return {"state": "failed", "error": s.auth_error, "since": s.auth_error_at.isoformat() if s.auth_error_at else None, "fix": "Run garmin-mcp-auth to log in (MFA prompt), then restart the server. Background sync is stopped until then."}
    try:
        dn = m.client.transport.display_name
    except Exception:  # noqa: BLE001
        dn = None
    return {"state": "ready" if dn else "pending", "display_name": dn}

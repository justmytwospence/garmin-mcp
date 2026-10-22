"""Migrations, applied in order and recorded in schema_migration. Never edit a shipped
migration: append a new one. The typed tables are generated from tables.py and
fit/tables.py, so a column added there needs an ALTER TABLE migration here as well (the
schema test catches a table whose live columns differ from its definition).
"""

from __future__ import annotations

from ..fit.tables import FIT_TABLES
from .tables import TABLES, table_ddl

CORE: list[str] = [
    """CREATE TABLE IF NOT EXISTS raw_payload (
    object_type VARCHAR NOT NULL,
    object_key VARCHAR NOT NULL,
    content_hash VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    body JSON NOT NULL,
    PRIMARY KEY (object_type, object_key, content_hash)
  )""",
    "COMMENT ON TABLE raw_payload IS 'Every distinct payload Garmin Connect returned, exactly as received (the source of truth; typed tables are rebuilt from it). A new row is added only when the content changes, so this is also the edit history of each object.'",
    "COMMENT ON COLUMN raw_payload.object_type IS 'Dataset name (see garmin_sync_status for the registry): activity, activity_details, sleep, heart_rates, daily_summary, user_settings, ...'",
    "COMMENT ON COLUMN raw_payload.object_key IS 'Key of the object: activity id, calendar date (YYYY-MM-DD), device id, uuid, or self for singletons.'",
    "COMMENT ON COLUMN raw_payload.content_hash IS 'SHA-256 of the response body.'",
    "COMMENT ON COLUMN raw_payload.fetched_at IS 'When this version was first fetched.'",
    "COMMENT ON COLUMN raw_payload.body IS 'Response body as JSON.'",
    """CREATE TABLE IF NOT EXISTS raw_latest (
    object_type VARCHAR NOT NULL,
    object_key VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    content_hash VARCHAR,
    fetched_at TIMESTAMPTZ,
    checked_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (object_type, object_key)
  )""",
    "COMMENT ON TABLE raw_latest IS 'The current version of every object: which raw_payload row is live, and when Garmin was last asked about it.'",
    "COMMENT ON COLUMN raw_latest.status IS 'ok, not_found (Garmin answered 404/204 or the day had no data), or archived (an intraday day Garmin has offloaded; a reload request is needed).'",
    "COMMENT ON COLUMN raw_latest.checked_at IS 'Last time the object was fetched, whether or not it had changed.'",
    """CREATE VIEW IF NOT EXISTS raw_current AS
    SELECT l.object_type, l.object_key, l.status, l.checked_at, p.fetched_at, p.body
    FROM raw_latest l LEFT JOIN raw_payload p USING (object_type, object_key, content_hash)""",
    "COMMENT ON VIEW raw_current IS 'The live payload of every object (body is NULL for not_found).'",
    """CREATE TABLE IF NOT EXISTS raw_file (
    kind VARCHAR NOT NULL,
    key VARCHAR NOT NULL,
    path VARCHAR NOT NULL,
    sha256 VARCHAR NOT NULL,
    size BIGINT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    parser_version INTEGER,
    parsed_at TIMESTAMPTZ,
    parse_error VARCHAR,
    PRIMARY KEY (kind, key)
  )""",
    "COMMENT ON TABLE raw_file IS 'Manifest of binary files kept on disk under the data directory: original activity FIT zips (kind activity_fit, key activity id) and daily wellness FIT zips (kind wellness_fit, key date). parser_version/parsed_at say which decoder produced the fit_* and monitoring_* rows.'",
    """CREATE TABLE IF NOT EXISTS sync_job (
    kind VARCHAR NOT NULL,
    key VARCHAR NOT NULL,
    priority INTEGER NOT NULL,
    sort_key BIGINT NOT NULL DEFAULT 0,
    not_before TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error VARCHAR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, key)
  )""",
    "COMMENT ON TABLE sync_job IS 'Durable work queue of the sync worker. Lower priority runs first (<10 interactive, <20 incremental, >=20 backfill); within a priority, higher sort_key (newer date) first.'",
    """CREATE TABLE IF NOT EXISTS sync_state (
    name VARCHAR PRIMARY KEY,
    value VARCHAR,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
  )""",
    "COMMENT ON TABLE sync_state IS 'Scheduler bookkeeping: last run of each periodic task, the last seen device upload time, the auth state, the pacing state.'",
    """CREATE TABLE IF NOT EXISTS request_log (
    observed_at TIMESTAMPTZ NOT NULL,
    method VARCHAR,
    path VARCHAR,
    status INTEGER,
    priority VARCHAR,
    ms INTEGER,
    bytes BIGINT
  )""",
    "COMMENT ON TABLE request_log IS 'Every request sent to Garmin Connect with its status, latency and size. Pruned after 30 days.'",
    """CREATE TABLE IF NOT EXISTS sync_log (
    logged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind VARCHAR,
    key VARCHAR,
    outcome VARCHAR,
    detail VARCHAR
  )""",
    "COMMENT ON TABLE sync_log IS 'Outcome of each sync job (ok, changed, not_found, error, rate_limited). Pruned after 30 days.'",
    """CREATE TABLE IF NOT EXISTS reload_request (
    calendar_date DATE NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL,
    status VARCHAR,
    response JSON,
    refetched_at TIMESTAMPTZ,
    PRIMARY KEY (calendar_date, requested_at)
  )""",
    "COMMENT ON TABLE reload_request IS 'Archive reload requests sent to Garmin for days whose intraday data was offloaded (POST wellness/epoch/request/{date}). Garmin allows about 30 per 24 hours; the engine uses at most 25. status is what Garmin answered (COMPLETE, PENDING, DENIED, ...); refetched_at is when the intraday datasets were fetched again.'",
]

MIGRATIONS: list[list[str]] = [
    [*CORE, *[s for t in TABLES for s in table_ddl(t)], *[s for t in FIT_TABLES for s in table_ddl(t)]],
]

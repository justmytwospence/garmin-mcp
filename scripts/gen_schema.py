"""Generate docs/data/schema.md: entity-relationship diagrams and the column reference
of the mirror, from a freshly migrated empty database (no Garmin access, no data).

    uv run python scripts/gen_schema.py            # write the page
    uv run python scripts/gen_schema.py --check    # exit 1 when the committed page is stale

Joins are logical: DuckDB foreign keys are not declared, so the relationships below are
derived from column names (activity_id, fit_file_id, calendar_date, ...) plus a short list
of named links, grouped the way the data is used.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data" / "schema.md"

SYSTEM = {"schema_migration"}
RAW = ["raw_payload", "raw_latest", "raw_file", "reload_request", "sync_job", "sync_log", "sync_state", "request_log"]

# Day-keyed tables hang off daily_summary ("same day").
DAY_PARENTS = {
    "heart_rate_day", "stress_day", "body_battery_day", "respiration_day", "floors_day", "hrv", "sleep", "spo2_day", "intensity_minutes_day",
    "steps_interval", "steps_daily", "all_day_event", "hydration", "lifestyle_logging", "body_battery_event", "daily_summary_v2", "sleep_stats", "nap",
    "wellness_metric", "training_readiness", "training_status", "max_metrics", "endurance_score", "hill_score", "cycling_ability", "fitness_age",
    "running_tolerance", "race_prediction", "weigh_in",
}
NAMED_LINKS: list[tuple[str, str, str, str | None]] = [
    ("fit_file", "key", "activity", "activity FIT (kind=activity_fit)"),
    ("fit_file", "key", "raw_file", "decoded from"),
    ("activity", "device_id", "device", None),
    ("device_settings", "device_id", "device", None),
    ("activity_gear", "gear_uuid", "gear", None),
    ("activity", "course_id", "course", None),
    ("activity", "associated_workout_id", "workout", None),
    ("calendar_item", "workout_id", "workout", None),
    ("workout", "training_plan_id", "training_plan", None),
    ("calendar_item", "training_plan_id", "training_plan", None),
    ("heart_rate_sample", "calendar_date", "heart_rate_day", None),
    ("stress_sample", "calendar_date", "stress_day", None),
    ("body_battery_sample", "calendar_date", "body_battery_day", None),
    ("respiration_sample", "calendar_date", "respiration_day", None),
    ("floors_interval", "calendar_date", "floors_day", None),
    ("hrv_reading", "calendar_date", "hrv", None),
    ("sleep_level", "calendar_date", "sleep", None),
    ("sleep_movement", "calendar_date", "sleep", None),
    ("sleep_sample", "calendar_date", "sleep", None),
    ("fit_file", "key", "daily_summary", "wellness FIT (kind=wellness_fit)"),
    ("reload_request", "calendar_date", "daily_summary", "archive reload"),
    ("raw_latest", "content_hash", "raw_payload", "current version"),
    ("sync_log", "kind", "sync_job", "outcome of"),
    ("body_battery_event", "activity_id", "activity", None),
]
ACCOUNT = [
    "user_profile", "user_settings", "device", "device_settings", "device_last_used", "sensor", "heart_rate_zones", "power_zones", "gear", "goal",
    "badge", "challenge", "workout", "training_plan", "calendar_item", "calendar_event", "course", "blood_pressure", "snapshot",
]
SAMPLES = ["heart_rate_sample", "stress_sample", "body_battery_sample", "respiration_sample", "floors_interval", "hrv_reading", "sleep_level", "sleep_movement", "sleep_sample", "reload_request"]


def load():
    from garmin_mcp.db.database import Database, DatabaseOptions

    tmp = Path(tempfile.mkdtemp(prefix="garmin-mcp-schema-"))
    db = Database.open(DatabaseOptions(str(tmp / "garmin.duckdb"), str(tmp / "snapshot")))
    try:
        cols: dict[str, list[tuple[str, str, str | None]]] = {}
        for r in db.all("SELECT table_name, column_name, data_type, comment FROM duckdb_columns() WHERE schema_name = 'main' AND NOT internal ORDER BY table_name, column_index"):
            cols.setdefault(r["table_name"], []).append((r["column_name"], r["data_type"], r["comment"]))
        pk: dict[str, list[str]] = {}
        for r in db.all("SELECT table_name, constraint_column_names FROM duckdb_constraints() WHERE constraint_type = 'PRIMARY KEY'"):
            pk[r["table_name"]] = list(r["constraint_column_names"])
        comments = {r["table_name"]: r["comment"] for r in db.all("SELECT table_name, comment FROM duckdb_tables() WHERE schema_name = 'main' AND NOT internal")}
        views = {r["view_name"]: (r["comment"], r["sql"]) for r in db.all("SELECT view_name, comment, sql FROM duckdb_views() WHERE schema_name = 'main' AND NOT internal")}
    finally:
        db.close()
    for t in list(cols):
        if t in SYSTEM or t not in comments:  # system tables and view columns
            cols.pop(t, None)
            comments.pop(t, None)
    return cols, pk, comments, views


def mermaid_type(t: str) -> str:
    t = t.replace("TIMESTAMP WITH TIME ZONE", "timestamptz").replace("[]", "_list").replace(" ", "_").lower()
    return re.sub(r"\W", "", t)


def links(cols: dict[str, list]) -> list[tuple[str, str, str, str | None]]:
    out: list[tuple[str, str, str, str | None]] = []
    for t, cs in cols.items():
        names = [n for n, _, _ in cs]
        if t.startswith("fit_") and "fit_file_id" in names and t != "fit_file":
            out.append((t, "fit_file_id", "fit_file", None))
        elif "activity_id" in names and t != "activity":
            out.append((t, "activity_id", "activity", None))
    out += NAMED_LINKS
    out += [(t, "calendar_date", "daily_summary", "same day") for t in sorted(DAY_PARENTS) if t in cols]
    return out


def diagram(cols, pk, tables: list[str], fks) -> str:
    lines = ["erDiagram"]
    for t in tables:
        if t not in cols:
            continue
        fkcols = {c for (a, c, _, _) in fks if a == t}
        lines.append(f"  {t} {{")
        for n, ty, _ in cols[t]:
            tags = []
            if n in pk.get(t, []):
                tags.append("PK")
            if n in fkcols:
                tags.append("FK")
            if tags:
                lines.append(f"    {mermaid_type(ty)} {n} {','.join(tags)}")
        lines.append(f'    _ more "{len(cols[t])} columns total"')
        lines.append("  }")
    seen = set()
    for a, c, b, label in fks:
        if a in tables and b in tables and a in cols and b in cols and (a, b, label or c) not in seen:
            seen.add((a, b, label or c))
            lines.append(f'  {b} ||--o{{ {a} : "{label or c}"')
    return "\n".join(lines)


def groups(cols):
    wfit = [t for t in cols if t.startswith("fit_") and "calendar_date" in [n for n, _, _ in cols[t]] and "activity_id" not in [n for n, _, _ in cols[t]]]
    afit = [t for t in cols if t.startswith("fit_") and t not in wfit]
    act = ["activity", "device", "gear", "course", "workout", "raw_file"] + sorted(t for t in cols if t.startswith("activity_")) + ["personal_record", "body_battery_event"]
    daily = ["daily_summary"] + sorted(DAY_PARENTS) + SAMPLES
    return [
        ("activities", "Activities", "One row per activity with its summary and detail merged, plus the per-activity tables (laps, splits, zones, weather, gear, sets, the encoded track) and what links to an activity.", act),
        ("activity-fit", "Activity FIT files", "The original FIT file of each activity, decoded: session, laps, lengths, splits, every record (one row per sample), events, HRV intervals, device info, and messages without a typed table.", ["activity", "raw_file"] + sorted(afit)),
        ("daily", "Daily wellness, sleep and training", "Everything keyed by calendar day. The *_day tables summarise one intraday dataset and the *_sample tables hold its series; the other tables are one row per day.", daily),
        ("wellness-fit", "Wellness FIT files", "The daily wellness bundles Garmin's devices upload, decoded: monitoring (steps, heart rate), stress, respiration, SpO2, sleep stages and assessment, HRV status and values, naps.", ["daily_summary", "raw_file", "fit_file"] + sorted(t for t in wfit if t not in ("fit_file", "fit_device_info", "fit_event", "fit_message"))),
        ("account", "Account, devices and planning", "The profile and settings, devices, sensors, gear, zones, goals, badges, challenges, workouts, training plans, the calendar, courses, blood pressure, and the snapshot payloads kept whole.", ACCOUNT),
        ("raw", "Raw store and sync", "Every payload as received (raw_payload, with raw_latest pointing at the current version), the file manifest, and the sync engine's queue, log and state.", RAW),
    ]


def render() -> str:
    cols, pk, comments, views = load()
    fks = links(cols)
    out = [
        "<!-- Generated by scripts/gen_schema.py; do not edit by hand. -->",
        "# Schema reference",
        "",
        f"{len(cols)} tables and {len(views)} views, generated from a freshly migrated mirror. Each diagram shows a table's primary-key and join columns plus its column count; joins are logical (DuckDB foreign keys are not declared). Column comments carry the unit; the same text comes back from the `garmin_schema` tool and from `DESCRIBE` on the snapshot.",
        "",
        "Units follow Garmin Connect: meters, seconds, meters per second, degrees Celsius, watts, bpm, kcal. Timestamps named `*_gmt` are UTC (`TIMESTAMPTZ`); `*_local` are wall-clock in the user's or the activity's time zone (`TIMESTAMP`). FIT tables use the FIT SDK's units after scale and offset; positions are degrees.",
        "",
        "## Groups",
        "",
    ]
    gs = groups(cols)
    placed: set[str] = set()
    for slug, title, _, tables in gs:
        out.append(f"- [{title}](#{slug}): " + ", ".join(f"`{t}`" for t in tables if t in cols))
        placed.update(t for t in tables if t in cols)
    unplaced = sorted(set(cols) - placed)
    if unplaced:
        raise SystemExit(f"tables in no group: {unplaced}")
    out.append("")
    described: set[str] = set()
    for slug, title, blurb, tables in gs:
        out += [f"## {title} {{: #{slug} }}", "", blurb, "", "```mermaid", diagram(cols, pk, tables, fks), "```", ""]
        for t in tables:
            if t not in cols or t in described:
                continue
            described.add(t)
            out += [f"### `{t}`", "", comments.get(t) or "", "", f"Primary key: {', '.join(f'`{k}`' for k in pk.get(t, [])) or 'none'}.", "", f'??? abstract "{len(cols[t])} columns"', "", "    | Column | Type | Description |", "    |---|---|---|"]
            for n, ty, c in cols[t]:
                desc = (c or "").replace("|", "\\|").replace("\n", " ")
                out.append(f"    | `{n}` | {ty.lower()} | {desc} |")
            out.append("")
    out += ["## Views", ""]
    for v, (c, sql) in sorted(views.items()):
        out += [f"### `{v}`", "", c or "", "", "```sql", sql.strip(), "```", ""]
    return "\n".join(out).rstrip() + "\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        current = OUT.read_text() if OUT.exists() else ""
        if current != text:
            print(f"{OUT.relative_to(ROOT)} is stale: run `uv run python scripts/gen_schema.py`", file=sys.stderr)
            return 1
        print(f"{OUT.relative_to(ROOT)} is current")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(ROOT)} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

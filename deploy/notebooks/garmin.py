import marimo

__generated_with = "0.25.1"
app = marimo.App(width="medium", app_title="Garmin mirror")


@app.cell
def _():
    import os
    from datetime import date, timedelta

    import altair as alt
    import duckdb
    import marimo as mo
    import polars as pl

    return alt, date, duckdb, mo, os, pl, timedelta


@app.cell
def _(mo):
    mo.md(r"""
    # Garmin mirror

    Three views over the read-only snapshot that garmin-mcp publishes next to its DuckDB mirror: recovery (sleep, HRV, resting heart rate), training load, and the per-second records of recent activities decoded from their FIT files. Every chart is a plain SQL query over the snapshot, so the same questions work in `garmin_query` or any DuckDB client.
    """)
    return


@app.cell
def _(duckdb, os):
    SNAPSHOT = os.environ.get("GARMIN_SNAPSHOT") or "snapshot/garmin.duckdb"
    con = duckdb.connect(SNAPSHOT, read_only=True)
    return SNAPSHOT, con


@app.cell
def _(con, mo):
    _overview = con.execute(
        """
        SELECT
          (SELECT count(*) FROM activity WHERE deleted_at IS NULL) AS activities,
          (SELECT count(*) FROM daily_summary) AS days,
          (SELECT min(calendar_date) FROM daily_summary) AS first_day,
          (SELECT max(calendar_date) FROM daily_summary) AS last_day,
          (SELECT count(*) FROM raw_latest WHERE object_type = 'activity_fit' AND status = 'ok') AS fit_files,
          (SELECT max(checked_at)::TIMESTAMP FROM raw_latest) AS last_fetch
        """
    ).fetchone()
    mo.md(
        f"""
        **{_overview[0]:,} activities** ({_overview[4]:,} with their FIT file) and **{_overview[1]:,} days** of wellness data from {_overview[2]} to {_overview[3]}. The snapshot was last fed by a Garmin fetch at {_overview[5]:%Y-%m-%d %H:%M} UTC.
        """
    )
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## Recovery

    Sleep score, overnight HRV against its balanced range, and resting heart rate over the last six months. The three charts share the time axis: drag to pan, scroll to zoom.
    """)
    return


@app.cell
def _(con, date, pl, timedelta):
    RECOVERY_START = date.today() - timedelta(days=180)
    recovery = pl.from_arrow(
        con.execute(
            """
            SELECT d.calendar_date,
                   s.sleep_score,
                   h.last_night_avg AS hrv,
                   h.weekly_avg AS hrv_weekly,
                   h.baseline_balanced_low AS hrv_low,
                   h.baseline_balanced_upper AS hrv_high,
                   d.resting_heart_rate,
                   d.last_seven_days_avg_resting_heart_rate AS resting_heart_rate_7d
            FROM daily_summary d
            LEFT JOIN sleep s USING (calendar_date)
            LEFT JOIN hrv h USING (calendar_date)
            WHERE d.calendar_date >= ?
            ORDER BY d.calendar_date
            """,
            [RECOVERY_START],
        ).arrow()
    )
    return RECOVERY_START, recovery


@app.cell
def _(alt, mo, recovery):
    _x = alt.X("calendar_date:T", title=None)
    _zoom = alt.selection_interval(bind="scales", encodings=["x"])
    _sleep = (
        alt.Chart(recovery.drop_nulls("sleep_score"))
        .mark_line(point=True, color="#4c78a8")
        .encode(_x, alt.Y("sleep_score:Q", title="Sleep score", scale=alt.Scale(domain=[0, 100])), tooltip=["calendar_date:T", "sleep_score:Q"])
        .properties(width=700, height=160, title="Sleep score")
    )
    _band = (
        alt.Chart(recovery.drop_nulls("hrv_low"))
        .mark_area(color="#b7e4c7", opacity=0.5)
        .encode(_x, alt.Y("hrv_low:Q", title="ms", scale=alt.Scale(zero=False)), alt.Y2("hrv_high:Q"))
    )
    _hrv = (
        alt.Chart(recovery.drop_nulls("hrv"))
        .mark_line(point=True, color="#2a9d8f")
        .encode(_x, alt.Y("hrv:Q", title="ms", scale=alt.Scale(zero=False)), tooltip=["calendar_date:T", "hrv:Q", "hrv_weekly:Q"])
    )
    _hrv_weekly = alt.Chart(recovery.drop_nulls("hrv_weekly")).mark_line(color="#264653", strokeDash=[4, 4]).encode(_x, alt.Y("hrv_weekly:Q"))
    _rhr = (
        alt.Chart(recovery.drop_nulls("resting_heart_rate"))
        .mark_line(point=True, color="#e76f51")
        .encode(_x, alt.Y("resting_heart_rate:Q", title="bpm", scale=alt.Scale(zero=False)), tooltip=["calendar_date:T", "resting_heart_rate:Q", "resting_heart_rate_7d:Q"])
        .properties(height=160, title="Resting heart rate (7-day average dashed)")
    )
    _rhr7 = alt.Chart(recovery.drop_nulls("resting_heart_rate_7d")).mark_line(color="#9c2f1c", strokeDash=[4, 4]).encode(_x, alt.Y("resting_heart_rate_7d:Q"))
    recovery_chart = alt.vconcat(
        _sleep.add_params(_zoom),
        (_band + _hrv + _hrv_weekly).properties(width=700, height=160, title="Overnight HRV, weekly average dashed, balanced range shaded"),
        (_rhr + _rhr7).properties(width=700),
    ).resolve_scale(x="shared")
    mo.ui.altair_chart(recovery_chart)
    return (recovery_chart,)


@app.cell
def _(mo):
    mo.md(r"""
    ## Training load

    Garmin's acute (7-day) and chronic (4-week) training load with the optimal range it derives from the chronic load, and the morning training readiness score. Load values come from the daily training status payload, readiness from the training readiness dataset.
    """)
    return


@app.cell
def _(RECOVERY_START, con, pl):
    load = pl.from_arrow(
        con.execute(
            """
            SELECT t.calendar_date,
                   TRY_CAST(t.acute_training_load->>'dailyTrainingLoadAcute' AS DOUBLE) AS acute,
                   TRY_CAST(t.acute_training_load->>'dailyTrainingLoadChronic' AS DOUBLE) AS chronic,
                   TRY_CAST(t.acute_training_load->>'minTrainingLoadChronic' AS DOUBLE) AS optimal_low,
                   TRY_CAST(t.acute_training_load->>'maxTrainingLoadChronic' AS DOUBLE) AS optimal_high,
                   r.readiness
            FROM training_status t
            LEFT JOIN (SELECT calendar_date, max(score) AS readiness FROM training_readiness GROUP BY 1) r USING (calendar_date)
            WHERE t.calendar_date >= ? AND t.acute_training_load IS NOT NULL
            ORDER BY t.calendar_date
            """,
            [RECOVERY_START],
        ).arrow()
    )
    return (load,)


@app.cell
def _(alt, load, mo):
    _x = alt.X("calendar_date:T", title=None)
    _band = alt.Chart(load.drop_nulls("optimal_low")).mark_area(color="#cfe8ff", opacity=0.6).encode(_x, alt.Y("optimal_low:Q", title="Load"), alt.Y2("optimal_high:Q"))
    _acute = alt.Chart(load.drop_nulls("acute")).mark_line(color="#1d3557").encode(_x, alt.Y("acute:Q"), tooltip=["calendar_date:T", "acute:Q", "chronic:Q"])
    _chronic = alt.Chart(load.drop_nulls("chronic")).mark_line(color="#457b9d", strokeDash=[4, 4]).encode(_x, alt.Y("chronic:Q"))
    _readiness = (
        alt.Chart(load.drop_nulls("readiness"))
        .mark_bar(color="#a8dadc")
        .encode(_x, alt.Y("readiness:Q", title="Readiness", scale=alt.Scale(domain=[0, 100])), tooltip=["calendar_date:T", "readiness:Q"])
        .properties(width=700, height=120, title="Training readiness")
    )
    load_chart = alt.vconcat(
        (_band + _acute + _chronic).properties(width=700, height=200, title="Acute load (solid), chronic load (dashed), optimal range (shaded)"),
        _readiness,
    ).resolve_scale(x="shared")
    mo.ui.altair_chart(load_chart)
    return (load_chart,)


@app.cell
def _(mo):
    mo.md(r"""
    ## Activity records

    The per-second samples of the six most recent activities that have a FIT file in the mirror: heart rate, power, speed and altitude over elapsed time, straight from `fit_record`. One tab per activity.
    """)
    return


@app.cell
def _(con, pl):
    recent = pl.from_arrow(
        con.execute(
            """
            SELECT a.activity_id, a.activity_name, a.activity_type, a.start_time_local, a.distance, a.duration
            FROM activity a
            JOIN raw_latest r ON r.object_type = 'activity_fit' AND r.status = 'ok' AND r.object_key = a.activity_id::VARCHAR
            WHERE a.deleted_at IS NULL AND EXISTS (SELECT 1 FROM fit_record f WHERE f.activity_id = a.activity_id)
            ORDER BY a.start_time_local DESC
            LIMIT 6
            """
        ).arrow()
    )
    return (recent,)


@app.cell
def _(alt, con, mo, pl, recent):
    def _records(activity_id):
        return pl.from_arrow(
            con.execute(
                """
                SELECT epoch(timestamp) - epoch(min(timestamp) OVER ()) AS elapsed_s,
                       heart_rate, power, speed * 3.6 AS speed_kmh, altitude
                FROM fit_record WHERE activity_id = ? ORDER BY timestamp
                """,
                [activity_id],
            ).arrow()
        )

    def _panel(field, title, color):
        return (
            alt.Chart()
            .mark_line(color=color)
            .encode(alt.X("elapsed_s:Q", title="elapsed (s)"), alt.Y(f"{field}:Q", title=title, scale=alt.Scale(zero=False)))
            .properties(width=700, height=110)
        )

    def _draw(row):
        _r = _records(row["activity_id"])
        _layers = [("heart_rate", "bpm", "#e63946"), ("power", "W", "#f4a261"), ("speed_kmh", "km/h", "#2a9d8f"), ("altitude", "m", "#6c757d")]
        _charts = [_panel(f, t, c).properties(data=_r.drop_nulls(f)) for f, t, c in _layers if _r[f].drop_nulls().len() > 0]
        _title = f"{row['activity_name'] or row['activity_type']} ({row['activity_type']}), {row['start_time_local']:%Y-%m-%d}, {(row['distance'] or 0) / 1000:.1f} km, {(row['duration'] or 0) / 60:.0f} min, {_r.height:,} records"
        return mo.vstack([mo.md(f"**{_title}**"), alt.vconcat(*_charts).resolve_scale(x="shared")])

    activity_tabs = mo.ui.tabs({f"{row['start_time_local']:%b %d} {row['activity_type']}": _draw(row) for row in recent.iter_rows(named=True)}) if recent.height else mo.md("No activity has a decoded FIT file yet.")
    activity_tabs
    return (activity_tabs,)


@app.cell
def _(mo):
    mo.md(r"""
    ## Your own questions

    `garmin_schema` lists every table with column descriptions and units; the same catalog is in the snapshot: `SELECT table_name, comment FROM duckdb_tables()`. Attach the snapshot from any DuckDB client with `ATTACH 'snapshot/garmin.duckdb' AS garmin (READ_ONLY)`, or over HTTP from the server's `/snapshot.duckdb` route.
    """)
    return


if __name__ == "__main__":
    app.run()

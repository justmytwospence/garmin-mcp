# Wellness export

garmin-mcp can write the mirror's non-activity data (sleep, overnight HRV, heart rate, stress, respiration, SpO2, Body Battery, steps, altitude and the daily summaries) in the `wellness-v1` format of [Wellness Lab](https://github.com/justmytwospence/wellness-lab): a manifest plus Parquet files, one per table and one per series and month. A reader needs no knowledge of the mirror's tables. The contract is Wellness Lab's [`schema/SPEC.md`](https://github.com/justmytwospence/wellness-lab/blob/main/schema/SPEC.md); `test/fixtures/wellness-v1.json` lists every column and the tests compare DuckDB's `DESCRIBE` of each file with it.

## Turning it on

| Setting | Default | Meaning |
|---|---|---|
| `GARMIN_EXPORT` | `off` | a comma list of `canonical` and `wellness`; `wellness` writes this export after every snapshot publish (and once at startup when a snapshot exists) |
| `GARMIN_EXPORT_DIR` | `<data dir>/export` | where it is written; the export itself is `wellness-v1/` inside it |
| `EXPORT_TOKEN` | unset | bearer token for the HTTP route; without it the route is disabled |
| `GARMIN_SNAPSHOT_WELLNESS_DELAY_MINUTES` | `30` | how long after last night's sleep or HRV day lands the snapshot is republished (at most one such publish a day) |

The exporter reads the published snapshot over its own read-only connection, so it never blocks the mirror. It rewrites only the files whose content changed (a series month by its row count, time range and value sum; a table by the hash of its bytes), so a routine publish writes a few files.

## Layout and URLs

```
wellness-v1/manifest.json                      GET /export/wellness/v1/manifest.json
wellness-v1/days.parquet                       one row per calendar day
wellness-v1/sleeps.parquet  stages.parquet  activities.parquet  tags.parquet  devices.parquet
wellness-v1/series/<name>/<YYYY-MM>.parquet    hr, stress, body_battery, resp, spo2, steps, hrv5, sleep_move, altitude
wellness-v1/rr/garmin-<activity id>.parquet    beat-to-beat intervals of activities recorded with Log HRV
```

All need `Authorization: Bearer <EXPORT_TOKEN>`. Each series takes the FIT wellness files where the mirror has them and the API's intraday tables only for days without, so history reaches back as far as the wellness FIT files do. Altitude comes from Garmin's undocumented monitoring message 279 (`raw / 5 - 500` metres). `manifest.json` lists every file with its rows, `sha256` and fingerprint; a reader downloads only the files whose hash changed.

## Signing in from a browser app

The wellness export shares the canonical export's sign-in ([canonical export](canonical-export.md#signing-in-from-a-browser-app)): the same `EXPORT_CORS_ORIGINS`, built-in OAuth page or OIDC issuer, and `Bearer EXPORT_TOKEN` for scripts. Its `/export/wellness/v1/auth` answers with client id `wellness-lab`; the OIDC client list `EXPORT_OIDC_CLIENT_ID` defaults to `effortcurve,wellness-lab`, so one provider client registration per app is enough.

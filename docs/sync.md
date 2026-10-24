# How sync works

The sync engine keeps the mirror complete and fresh with as few Garmin requests as the job allows. It is a durable job queue in the mirror itself (`sync_job`), one worker thread that drains it within Garmin's limits, a planner that turns every gap in the mirror into jobs, and a scheduler for the periodic checks. Nothing here needs supervision; `garmin_sync_status` shows all of it.

## One request at a time

Every request to Garmin goes through one client, strictly sequentially:

- Background work keeps about **1.5 s between requests** (with up to 0.5 s of jitter). Tool calls get a shorter gap (0.5 s) and go first: a user waiting for an answer is never queued behind the backfill.
- A **429** stops all background work for **30 minutes**; a tool call during that time is answered from the mirror, marked stale, or refused if the mirror has nothing.
- **5xx and network errors** are retried three times with exponential backoff (4, 8, 16 s), then the job is rescheduled with a growing delay (2, 4, 8, ... minutes, capped at a day).
- An **authentication failure** stops everything. The server never re-logs in on its own, so a revoked token cannot turn into a stream of login attempts against Garmin's SSO; `garmin_sync_status` reports the problem and the fix, and tools keep serving the mirror.

Every request is recorded in `request_log` (method, path, status, latency, size; pruned after 30 days), so the rate the server actually sustains is visible.

## What runs first

Jobs have a priority (lower runs first) and, within a priority, newer dates run first:

| Priority | Work |
|---|---|
| 5 | Refreshes queued by tools after a write |
| 10 | The device poll, the recent window after a device sync, listing new activities |
| 15-19 | Range datasets: daily summaries, steps, sleep statistics, HRV, readiness, VO2 max, Body Battery, endurance and hill scores, fitness age (28 days per request, so coverage comes fast) |
| 20-29 | Per-day datasets (heart rate, stress, sleep, respiration, SpO2, floors, intensity minutes, training status, events, naps...) and per-activity datasets (detail, splits, typed splits, weather, zones, gear, exercise sets, polyline), the FIT files |
| 30-45 | Snapshots with no urgency (devices, gear, zones, records, badges, goals, workouts, courses, calendar months), activity chart details |
| 50 | Archive reloads and their re-fetches |

Days older than a month get +10, older than about 13 months +20, so recent history fills in first at every level. Priorities below 10 are the interactive rate class, below 20 incremental, the rest backfill.

A first run on an account with a few years of history therefore goes: profile and settings, the complete activity list (100 per page), daily summaries for every 28-day window, then day by day and activity by activity, newest first. At 1.5 s per request, a day costs about a dozen requests and an activity about ten plus its FIT file, so a year of daily use is a few days of syncing. Tools work from the first minute: whatever the mirror lacks is fetched on demand.

## Staying current

- Every **15 minutes** the engine polls the last-used device. A new upload time means a fresh sync from the watch: it refreshes the last three days of every daily dataset and lists new activities.
- Every **15 minutes** it also lists recent activities (stopping at the first page it already knows), and every **7 days** the whole list, which catches edits and deletions (an activity missing from the full list is marked `deleted_at`, its data kept).
- Every **6 hours** the last three days are refreshed regardless.
- Every **hour** the planner runs: it compares the mirror with what should exist (every day from the history start, every activity's datasets, every snapshot past its refresh interval) and queues what is missing. It costs no requests and is idempotent, so a server that was down for a week simply catches up.
- Snapshots (profile, devices, gear, zones, records, badges...) have per-dataset refresh intervals from 15 minutes to a week.

## The archive model

Garmin keeps the per-sample series of a day (heart rate every two minutes, stress and Body Battery every three, sleep stages and overnight series, respiration, SpO2, floors, HRV readings, Body Battery events) online for about six months and then offloads them; the day's summaries stay. Asking for an offloaded day returns an empty series, which the mirror records as `sample_count = 0` on the day's row.

Garmin can bring a day back on request (`POST wellness/epoch/request/{date}`), about 30 requests per 24 hours. The engine spends at most 25 a day, oldest days first, re-fetches each day a few hours later, and shows the quota in `garmin_sync_status` (`reload_quota`). `request_reload` spends one on a day of your choice.

**Why reloads are rare.** The daily wellness FIT files Garmin's devices upload stay downloadable for every day, and they carry the same series at higher resolution: monitoring records with heart rate and steps, per-minute stress and Body Battery, respiration, SpO2, sleep stages and the sleep assessment, HRV status and values, naps. The engine downloads them for every day in the history (`wellness_fit` jobs) and decodes them into the `fit_*` tables, so an archived day is usually complete without touching the reload quota. A reload is requested only for a day whose wellness FIT came back missing or without monitoring data; days whose FIT is still queued wait. [Data](data/index.md#fit-tables) shows where each series lives.

## FIT files

For every activity that is not manual, the original FIT file is downloaded (a zip from Garmin, kept as-is under `fit/activities/<id>.zip`) and decoded with Garmin's FIT SDK into typed tables: `fit_session`, `fit_lap`, `fit_length`, `fit_split`, `fit_record` (every sample: position, speed, altitude, heart rate, cadence, power, temperature, running dynamics, cycling dynamics), `fit_event`, `fit_hrv` (beat-to-beat intervals), `fit_device_info`, `fit_time_in_zone`. Fields the schema does not name land in each row's `extra` JSON column; messages without a typed table land whole in `fit_message`. Nothing from a file is dropped. The daily wellness bundles go through the same decoder into the `fit_monitoring*`, `fit_stress_level`, `fit_respiration_rate`, `fit_spo2`, `fit_sleep_*`, `fit_hrv_*` and `fit_nap_event` tables.

The activity chart series Garmin serves over the API (`activity_details`, a megabyte per activity at reduced resolution) are redundant with the FIT file and sit at the lowest priority.

Garmin's [account data export](operations.md#importing-garmins-account-export) contains the same FIT files; importing it decodes them without any API call and the sync skips those downloads.

## Freshness

A tool call reads the mirror when the stored copy is fresh enough for its kind and fetches first otherwise:

| Data | Served from the mirror when younger than |
|---|---|
| Today and yesterday | 15 minutes |
| The last week | 6 hours |
| Older days | always (they do not change) |
| An activity from the last two days | 15 minutes |
| Older activities | always, unless the listing shows a newer edit |
| Snapshots (profile, devices, gear, zones...) | the dataset's refresh interval, a day to a week |
| Pass-through reports (nutrition, Garmin's progress and trend reports) | 10 minutes to an hour |

If Garmin cannot be asked (backoff after a 429, an outage, a lost login) and a copy exists, the copy is served and marked stale. Writes (activity edits, workouts, weigh-ins...) go to Garmin first; the affected objects are then re-fetched into the mirror, never guessed, and the result says `write_committed: true`, `false` or `"unknown"` (a 5xx or timeout after the request went out).

## Provenance

Every tool result ends with a line saying where it came from:

```
[served from the local mirror (fetched from Garmin 2026-10-23T06:12:40+00:00)]
[fetched from Garmin Connect just now]
[served from the local mirror (fetched ...)] [stale: Garmin could not be asked: Backing off after a 429 for another 1720 s]
[3 of 7 parts fetched from Garmin just now; the rest from the mirror (oldest 2026-10-18T05:00:12+00:00)]
```

The three mirror tools always say so: `[SQL over the local mirror; no Garmin request]`.

## Unmapped fields

Every response is stored whole, so a field Garmin adds later is never lost; it just has no column yet. `garmin_sync_status` lists such fields under `unmapped_fields` (from the newest 200 payloads of each type, checked hourly). They remain readable through `raw_current` with DuckDB's JSON operators until a release maps them.

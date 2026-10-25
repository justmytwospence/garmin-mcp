# Strava sync

An optional background task for people who also run [strava-mcp](https://github.com/justmytwospence/strava-mcp). It is off unless `STRAVA_SYNC` is set, and it never talks to Strava: it reads strava-mcp's read-only snapshot, so it needs no Strava credentials and uses none of Strava's API quota.

It does two things Garmin and Strava do not do for each other:

- **Titles.** When you name or rename an activity on Strava, the Garmin activity gets the same name. Garmin sends a new activity to Strava once, and a later rename on Strava never comes back.
- **Courses.** When you unstar a Strava route, the course Strava's sync made for it is deleted from Garmin Connect. Strava's sync adds a course when you star a route but never removes it, so courses for routes you no longer want pile up in Garmin Connect and on the watch.

## Setting it up

```sh
STRAVA_SYNC=titles,courses
STRAVA_SNAPSHOT=/strava/strava.duckdb          # strava-mcp's snapshot/strava.duckdb, mounted read-only
# or, over HTTP:
# STRAVA_SNAPSHOT=http://strava-mcp:8000/snapshot.duckdb
# STRAVA_SNAPSHOT_TOKEN=<strava-mcp's SNAPSHOT_TOKEN>
STRAVA_SYNC_COURSES=dry-run                    # report orphaned courses, delete none (the default)
```

Then check `strava_sync_plan`, do the first cleanup with `strava_sync_apply` (below), and set `STRAVA_SYNC_COURSES=on`. Every variable is on the [configuration page](configuration.md#strava-sync-optional).

The engine runs the sync every 15 minutes as the job `strava_sync:self`, behind the device poll and the activity listing. Its Garmin requests are paced like every other request, and it stops for Garmin's 429 backoff like everything else.

## Titles

A Strava activity is paired with the Garmin activity that started within 60 seconds of it, and only when the pairing is unambiguous. Strava's `external_id` (`garmin_ping_…`) is not the Garmin activity id, so the start time is what links them. Then:

- Strava's automatic names ("Morning Run", "Afternoon Trail Run") are ignored, so Garmin's own automatic name stays.
- When the Strava name differs from what the sync last acted on, the Garmin activity is renamed. Its current Garmin name is fetched first.
- **A Garmin name you edited by hand wins.** When the Garmin name differs from the name the sync last wrote, the sync leaves it alone, reports a conflict once, and acts again only after the Strava name changes again.
- Only activities that started on or after `STRAVA_SYNC_TITLES_SINCE` are considered. By default that is the day the sync first ran, so turning it on never renames your history. Set an earlier date to backfill; renames then go at most `STRAVA_SYNC_MAX_RENAMES` (20) per run.

How quickly a rename arrives depends on strava-mcp: its webhook receives the rename at once, and its snapshot is republished within the hour.

## Courses

Only courses that Strava's own route sync created are considered. Garmin records the app that made a course, and Strava's is the consumer id `3527224f-080a-4b66-8b72-0a2c3e111af0`. Courses you made in Garmin Connect, or that another app (Ride with GPS, Komoot) sent, are never touched.

A Strava-made course is kept while it matches a starred Strava route:

1. a link recorded on an earlier run, so a route renamed on Strava stays matched,
2. the exact route name, which is what Strava gives the course, or
3. the same length (within 2%) and the same start (within 250 m), for a route renamed before the sync ever saw it.

When several courses match the same route (Strava makes a new course when you edit a route), one matched by name or by an earlier link beats one matched only by length and start, then the newest is kept; the other copies count as duplicates. Every other Strava-made course is orphaned.

Orphans are deleted only when all of these hold:

- `STRAVA_SYNC_COURSES=on`.
- Strava's route list in the snapshot was refreshed within `STRAVA_SYNC_MAX_AGE_HOURS` (48).
- The snapshot lists at least one starred route. An empty list would otherwise delete every Strava-made course.
- There are no more than `STRAVA_SYNC_MAX_DELETES` (10) orphans. Above the cap, nothing is deleted and the run says so, so a broken snapshot cannot empty your course library.

!!! warning "Deleted courses cannot be restored"
    Garmin Connect has no trash for courses. Look at `strava_sync_plan` before turning deletions on, and keep the route starred on Strava if you want the course.

## Tools

| Tool | What it does |
|---|---|
| `strava_sync_status` | Settings, the last run's result, the ledger's counts and recent actions. No Garmin request. |
| `strava_sync_plan` | What a run would do now: each rename, each conflict, each course kept (with its route and how it matched) or deleted (and why). Re-fetches the course list first. |
| `strava_sync_apply` | Runs one part (`titles` or `courses`) now with no per-run cap, and for courses even in dry-run mode. Shows a preview until called with `confirm=true`. Use it for the first cleanup of a long backlog. |

The tools are registered only when `STRAVA_SYNC` is set.

## What it keeps

The table `strava_link` is the sync's ledger. It records which Strava activity is which Garmin activity and the name last written, and which route each kept course follows. Every rename, conflict and deletion is also logged in `sync_log` with kind `strava_sync`, kept for 30 days:

```sql
SELECT logged_at, key, outcome, detail FROM sync_log WHERE kind = 'strava_sync' ORDER BY logged_at DESC;
```

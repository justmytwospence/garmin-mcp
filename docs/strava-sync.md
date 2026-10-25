# Strava sync

An optional background task for people who also run [strava-mcp](https://github.com/justmytwospence/strava-mcp). It is off unless `STRAVA_SYNC` is set, and it never talks to Strava: it reads what strava-mcp already mirrors, so it needs no Strava credentials and uses none of Strava's API quota.

It does two things Garmin and Strava do not do for each other:

- **Titles.** When you name or rename an activity on Strava, the Garmin activity gets the same name. Garmin sends a new activity to Strava once, and a later rename on Strava never comes back.
- **Courses.** When you unstar a Strava route, the course Strava's sync made for it is deleted from Garmin Connect. Strava's sync adds a course when you star a route but never removes it, so courses for routes you no longer want pile up in Garmin Connect and on the watch.

## Setting it up

In garmin-mcp's `.env`:

```sh
STRAVA_SYNC=titles,courses
STRAVA_EXPORT_URL=http://strava-mcp:8000/export/sync   # strava-mcp's live export (older releases: /export/garmin-sync)
STRAVA_EXPORT_TOKEN=<strava-mcp's SNAPSHOT_TOKEN>
STRAVA_SYNC_COURSES=dry-run                             # report orphaned courses, delete none (the default)
```

The export is a few kilobytes of JSON that strava-mcp computes live from its mirror: `{generated_at, route_list_checked_at, activity_list_checked_at, starred_routes: [{id, name, distance, start}], activities: [{id, name, start_date, elapsed_time}]}`, with `?since=<ISO date>` limiting the activities and strava-mcp's `SNAPSHOT_TOKEN` as the bearer token. Without it, `STRAVA_SNAPSHOT` reads strava-mcp's snapshot instead (`snapshot/strava.duckdb` mounted read-only, or its `http(s)://…/snapshot.duckdb` URL with the same token); that works with any strava-mcp version, but the snapshot is republished at most hourly, so changes take up to an hour longer.

Then check `strava_sync_plan`, do the first cleanup with `strava_sync_apply` (below), and set `STRAVA_SYNC_COURSES=on`. Every variable is on the [configuration page](configuration.md#strava-sync-optional).

### Reaching strava-mcp from another compose project

When the two servers run as separate compose projects, they are on separate networks and `http://strava-mcp:8000` does not resolve. Put them on one shared network: create it once (`docker network create mcp-shared`), declare it in both projects, and keep the declaration out of the tracked files with a `compose.override.yaml` next to each `compose.yaml` (compose merges it automatically):

```yaml
# compose.override.yaml, in the garmin-mcp project and the same in strava-mcp's
services:
  garmin-mcp:           # strava-mcp: the strava-mcp service
    networks:
      - default
      - mcp-shared
networks:
  mcp-shared:
    external: true
```

The export is then at `http://strava-mcp:8000/export/sync` (the service name on the shared network); on one host without a shared network, `http://host.docker.internal:<strava-mcp's port>/export/sync` works on Docker Desktop, and the host's LAN address everywhere.

### Reading the snapshot file instead

To use the snapshot file rather than the export, mount strava-mcp's data volume read-only into the garmin-mcp container and point `STRAVA_SNAPSHOT` at it:

```yaml
# compose.override.yaml in the garmin-mcp project
services:
  garmin-mcp:
    environment:
      STRAVA_SNAPSHOT: /strava/snapshot/strava.duckdb
    volumes:
      - strava-data:/strava:ro
volumes:
  strava-data:
    external: true
    name: strava-mcp_strava-data   # <project>_<volume>, see `docker volume ls`
```

strava-mcp replaces the file atomically, so the sync attaches it afresh on every read.

### Localized default names

Strava names activities automatically ("Morning Run", "Afternoon Trail Run"), and the sync never copies those onto Garmin. The built-in pattern recognises the English ones; Strava localizes them to the account's language (an Italian account sees "Passeggiata mattutina"), so set `STRAVA_DEFAULT_NAME_PATTERN` to a regular expression for yours: look at a few activities you have not renamed on Strava for the exact wording (the time of day and the sport, in some order), and anchor the pattern the way the default does. The pattern is validated at start, and `strava_sync_status` shows the one in use.

The engine runs the sync every 2 minutes as the job `strava_sync:self`, behind the device poll and the activity listing. A run that finds nothing to do sends no Garmin request: renames are decided from the mirror, and Garmin's course list is fetched only when the starred routes changed, before any deletion, and otherwise hourly. Its Garmin requests are paced like every other request, and it stops for Garmin's 429 backoff like everything else.

## How fast

| Change on Strava | Reaches strava-mcp | Reaches Garmin Connect |
|---|---|---|
| Rename an activity | at once (webhook) | within about 2 minutes |
| Name a just-uploaded activity | at once | within a few minutes: when the mirror has not listed the Garmin activity yet, the sync asks for the listing once |
| Unstar a route | within an hour by default: strava-mcp polls the route list (Strava sends no event); `STRAVA_ROUTES_INTERVAL_MINUTES=15` on strava-mcp's side brings it to 15 minutes | within about 2 minutes after that |

With `STRAVA_SNAPSHOT` add up to an hour to each. The watch picks up a course change at its next sync with Garmin Connect.

## Titles

A Strava activity is paired with the Garmin activity that started within 60 seconds of it, and only when the pairing is unambiguous. Strava's `external_id` (`garmin_ping_…`) is not the Garmin activity id, so the start time is what links them. Then:

- Strava's automatic names ("Morning Run", "Afternoon Trail Run") are ignored, so Garmin's own automatic name stays.
- When the Strava name differs from what the sync last acted on, the Garmin activity is renamed. Its current Garmin name is fetched first.
- **A Garmin name you edited by hand wins.** When the Garmin name differs from the name the sync last wrote, the sync leaves it alone, reports a conflict once, and acts again only after the Strava name changes again.
- Only activities that started on or after `STRAVA_SYNC_TITLES_SINCE` are considered. By default that is the day the sync first ran, so turning it on never renames your history. Set an earlier date to backfill; renames then go at most `STRAVA_SYNC_MAX_RENAMES` (20) per run.

## Courses

Only courses that Strava's own route sync created are considered. Garmin records the app that made a course, and Strava's is the consumer id `3527224f-080a-4b66-8b72-0a2c3e111af0`. Courses you made in Garmin Connect, or that another app (Ride with GPS, Komoot) sent, are never touched.

A Strava-made course is kept while it matches a starred Strava route:

1. a link recorded on an earlier run, so a route renamed on Strava stays matched,
2. the exact route name, which is what Strava gives the course, or
3. the same length (within 2%) and the same start (within 250 m), for a route renamed before the sync ever saw it.

When several courses match the same route (Strava makes a new course when you edit a route), one matched by name or by an earlier link beats one matched only by length and start, then the newest is kept; the other copies count as duplicates. Every other Strava-made course is orphaned.

Orphans are deleted only when all of these hold:

- `STRAVA_SYNC_COURSES=on`.
- strava-mcp refreshed Strava's route list within `STRAVA_SYNC_MAX_AGE_HOURS` (48).
- strava-mcp lists at least one starred route. An empty list would otherwise delete every Strava-made course.
- There are no more than `STRAVA_SYNC_MAX_DELETES` (10) orphans. Above the cap, nothing is deleted and the run says so, so a bad read of Strava data cannot empty your course library.

!!! warning "Deleted courses cannot be restored"
    Garmin Connect has no trash for courses. Look at `strava_sync_plan` before turning deletions on, and keep the route starred on Strava if you want the course.

## Tools

| Tool | What it does |
|---|---|
| `strava_sync_status` | Settings, the last run's result, the ledger's counts and recent actions. No Garmin request. |
| `strava_sync_plan` | What a run would do now: each rename, each conflict, each course kept (with its route and how it matched) or deleted (and why). Re-fetches the course list first. |
| `strava_sync_apply` | Runs one part (`titles` or `courses`) now with no per-run cap, and for courses even in dry-run mode. Shows a preview until called with `confirm=true`. Use it for the first cleanup of a long backlog. |

The tools are registered only when `STRAVA_SYNC` is set (<!-- count:tools-strava -->3<!-- /count --> more on top of the <!-- count:tools -->160<!-- /count -->).

## What it keeps

The table `strava_link` is the sync's ledger. It records which Strava activity is which Garmin activity and the name last written, and which route each kept course follows. Every rename, conflict and deletion is also logged in `sync_log` with kind `strava_sync`, kept for 30 days:

```sql
SELECT logged_at, key, outcome, detail FROM sync_log WHERE kind = 'strava_sync' ORDER BY logged_at DESC;
```

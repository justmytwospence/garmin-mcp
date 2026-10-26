# MCP context and API budget

Read `garmin://usage` with MCP `resources/read` first. This static guide lists every Garmin resource URI template, examples and prompt names even in clients that cannot list resource templates or ignore server instructions. Existing static `workout://templates/*` and `workout://reference/structure` resources remain available.

All resources and prompt retrieval are local-only: no Garmin calls, writes, jobs, login probes or archive reloads. Personal-data resources follow the matching tool filter: schema requires `garmin_schema`, status/coverage require `garmin_sync_status`, activity/day templates and prompts require `garmin_query`. The guide is always available; it does not override those filters.

## Resources

| URI | Content and bounds |
|---|---|
| `garmin://usage` | Static usage, privacy, batching, retry and discovery guide |
| `garmin://schema` | Local table/view catalog with approximate row counts |
| `garmin://schema/{table}` | One table's columns, types, units and comments; example `garmin://schema/activity` |
| `garmin://status` | Same local state as `garmin_sync_status`, including login, cached drift, pacing and API-budget observations |
| `garmin://coverage` | Local coverage, dataset check times, queue and last runs |
| `garmin://activities/{activity_id}` | Cached summary and first 50 laps; example `garmin://activities/123` |
| `garmin://days/{date}` | Cached daily summary, sleep, HRV and newest 10 readiness samples; example `garmin://days/2026-01-01` |

Templates are deliberately **cache-only**, never refreshing a stale/missing entry. They disclose source timestamps, gaps and truncation. Resource `generated_at` is not a freshness guarantee. Sleep is attributed to the wake-up day. Missing/null does not prove no activity or illness. Activity templates omit raw tracks and high-frequency samples. JSON is character-bounded; oversized context returns an explicit partial/omission notice, not broken JSON. Use the schema tool for one table or bounded SQL with explicit columns for more.

## Prompts

`prompts/get` returns instructions, not fetched data or actions:

- `activity_review`, arguments `{"activity_id":"123"}`: review one activity from cached summary/laps, then bounded SQL if necessary. No exports or account writes.
- `recovery_review`, arguments `{"date":"2026-01-01"}`: review one day's sleep, HRV, stress and readiness with a short local baseline. No archive reloads or medical diagnosis.

Following a prompt with optional stale/missing-data tool fetches can cost upstream requests; both prompts prefer local reads and require disclosure of additional cost.

## Quotas and safe retries

General Garmin API quota, remaining requests and reset time are **unknown**. `pacing` in sync status reports timestamped local observations, not authoritative quota headers. `observed_at` is null until a request has been attempted; `cooldown_until` is a UTC timestamp, while `backoff_remaining_s` is calculated from a monotonic clock. Local counts include each transport attempt/retry once. Library token refreshes, explicit 401 recovery and compound methods can add HTTP calls; other clients' calls are not counted.

Requests are sequential, with interactive priority: normally a 0.5-second interactive gap or a 1.5-second background gap plus 0–0.5 seconds jitter. A 429 stops new admission for at least 30 minutes, or a longer `Retry-After` delta/date when available. Only GET/HEAD/OPTIONS retry transient network/5xx failures (up to three retries with backoff and available `Retry-After`). Writes and library CALLs are not replayed after ambiguous failures; library method retries are disabled. Check current state before manually repeating a write with `write_committed="unknown"`.

The archive-reload allowance is separate: a conservative **local** limit of 25 per rolling 24 hours, not a guaranteed upstream allowance or general quota. Status includes the local ledger and `next_local_slot_at` when full. Admission is serialized; a reload is reserved before sending so a lost response or process exit cannot cause an automatic repeat. Uncertain outcomes remain `pending_outcome`/`outcome_unknown` in the ledger. Read-only follow-up checks coverage; do not assume the reload failed or clear the reservation merely to retry.

Other tools are cache-first, not necessarily zero-call. Ranges, pagination, multi-activity FIT analysis, batches and write refetches can multiply requests. Prefer filtered SQL aggregates, small windows and small pages. `garmin_query` executes one statement in a read-only transaction on its actual cursor, always rolls back and closes it, serializes shared-instance access, times out after 30 seconds and returns at most 5000 rows (default 200). Read-only transactions enforce database safety; wrapped non-read statements are also rejected to prevent filesystem writes into allowed snapshot storage.

## Filesystem writes and privacy

`download_activity_file` and `download_course_gpx` are destructive-capable filesystem writes: existing exports are replaced. FIT downloads can fetch a missing original; GPX/TCX/CSV can call Garmin. Caller-selected paths cannot overwrite mirror/WAL, FIT storage, snapshots, token directories, `.env` or download configuration files. Symlink traversal is rejected; exports use directory descriptors and atomic replacement so existing hardlinks are not truncated. Use a normal writable export directory, outside protected storage, without symlink components. `set_fit_download_dir` changes local configuration only.

Hints describe behavior; they do not enforce authorization, human approval or API spending caps. Existing confirmation inputs still apply. Do not share mirrors, tokens, FIT files, location data or account identifiers unnecessarily. Tools retain their compatible text/provenance blocks; bounded JSON results also include structured data, without claiming a stable output schema for heterogeneous legacy results.

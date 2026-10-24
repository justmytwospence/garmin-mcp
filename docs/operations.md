# Operations

## Memory

DuckDB sizes its buffer pool to 80% of the host's RAM unless told otherwise, which in a container means the kernel's OOM killer. The image sets `DUCKDB_MEMORY_LIMIT=512MB` and `DUCKDB_THREADS=2`, and `compose.yaml` caps the container at 1.5 GB (`GARMIN_MCP_MEMORY`). Measured on a mirror of seven years (2.3 GB file, 74,000 payloads, 1,800 activities with FIT files, 7.7 million `fit_record` rows), peak RSS of the process:

| Operation | Peak RSS |
|---|---|
| Idle, mirror open | 75 MB |
| Planner run | 125 MB |
| Projecting one activity, its splits or its chart details | 150-190 MB |
| Decoding a 10-hour activity FIT file (36,000 records) | 300-480 MB |
| `garmin_sync_status` (includes the unmapped-field check) | 650-720 MB |
| Publishing a snapshot (checkpoint and file copy) | no more than the operation before it |
| A long session: status, planner, projections, FIT decode, queries, snapshot | about 800 MB, whatever the limit between 384 MB and 768 MB |

Two things sit outside DuckDB's limit: the Python process (about 75 MB) and the memory DuckDB's JSON parser and string vectors use while scanning stored payloads (a few hundred MB, released after the query). The buffer pool itself fills up to the limit and stays there. Rule of thumb: **container memory = 100 MB + `DUCKDB_MEMORY_LIMIT` + 300 MB**, with the limit between 384 MB and a few GB depending on how big the queries you run through `garmin_query` are. Queries that need more than the limit spill to `duckdb-tmp/` on disk rather than failing, so a small limit costs speed, not correctness.

Two operations used to break this budget and were changed in 1.0: the snapshot was built with `COPY FROM DATABASE`, which rebuilt every table through the buffer pool (1.5 GB RSS and 160 s on this mirror, out of memory below 1 GB) and is now a checkpoint plus a file copy; and the unmapped-field check parsed every stored payload and now samples the newest 200 per type, once an hour.

Disk: the live file is somewhat larger than the snapshot (DuckDB does not return freed blocks); budget about 3x the snapshot size plus the FIT files (roughly 150 KB per activity, 50 KB per wellness day).

## Upgrades

```sh
docker compose pull
docker compose up -d
```

The image tag in `compose.yaml` follows the major version (`GARMIN_MCP_VERSION=1`); pin `1.2.3` if you prefer explicit upgrades. Releases are tagged `vX.Y.Z` and the [changelog](changelog.md) says what changed. Schema migrations run at start and are recorded in `schema_migration`; a release that changes a projection rebuilds the affected typed tables from the raw store, and one that changes the FIT decoder decodes the stored files again, without fetching anything. Downgrading across a migration is not supported: keep a backup of the snapshot before a major upgrade.

python-garminconnect, which speaks Garmin's private API, moves fast as Garmin changes things. A release that bumps it is the usual fix when a dataset starts failing; `garmin_sync_status` shows which (`recent_errors`).

## Login problems

Every way a login can fail is classified into a short kind, shown with the fix by `garmin-mcp-auth`, in the server log at start, in tool errors and in `garmin_sync_status` under `login`:

```json
"login": {"state": "failed", "kind": "expired", "error": "The stored tokens in /data/tokens were rejected by Garmin: ...", "fix": "Run `docker compose run --rm garmin-mcp garmin-mcp-auth` ..."}
```

While the login state is `failed`, background sync is stopped, nothing is fetched on demand, and every tool keeps answering from the mirror, marked stale. The server never retries a login on its own: the fix is always a person running the auth command, then a restart.

| Kind | What happened | Fix |
|---|---|---|
| `no_tokens` | `garmin_tokens.json` is missing from the token directory (first run, or a new volume) | `docker compose run --rm garmin-mcp garmin-mcp-auth`, then `docker compose restart garmin-mcp` |
| `expired` | Garmin rejected the stored tokens (401/403): they expired, were revoked by a password change or a sign-out of all devices, or could not be refreshed | Same two commands. Tokens normally refresh themselves for months; this happens after a password change or a long downtime |
| `mfa_required` | A credential login (`GARMIN_EMAIL`/`GARMIN_PASSWORD`) hit an MFA prompt, which the server cannot answer | Run `garmin-mcp-auth` interactively; it prompts for the code |
| `mfa_rejected` | The code typed into `garmin-mcp-auth` was not accepted (expired, or an older email's code) | Run it again and type the newest code within a minute |
| `wrong_password` | Garmin rejected the email or password | Check them at connect.garmin.com (the account may also be locked there). For an account on Garmin Connect China, set `GARMIN_IS_CN=true` first |
| `region_mismatch` | The tokens were created for garmin.com but `GARMIN_IS_CN=true` selects garmin.cn, or the reverse | Set `GARMIN_IS_CN` the same way for the server and `garmin-mcp-auth`, restart, or log in again on the right domain |
| `rate_limited` | Garmin is rate limiting logins from this network (HTTP 429), usually after several attempts in a row | Wait at least 30 minutes, longer after repeated attempts, or use another network; `garmin-mcp-auth` refuses to try credentials while this lasts |
| `legacy_tokens` | The directory holds `oauth1_token.json`/`oauth2_token.json` from an older python-garminconnect | Log in again with `garmin-mcp-auth`; the old files are ignored |
| `unreachable` | Garmin Connect could not be reached (network, DNS, or Garmin's bot protection blocking a hosting provider's address) | Check connectivity from the host, wait, then `garmin-mcp-auth --check` |

Useful commands:

```sh
docker compose run --rm garmin-mcp garmin-mcp-auth --check    # verify the stored tokens, exit 0 when they work
docker compose run --rm garmin-mcp garmin-mcp-auth            # log in (keeps working tokens unless --force-reauth)
docker compose run --rm garmin-mcp garmin-mcp-auth --force-reauth
docker compose restart garmin-mcp                             # the server reads the tokens at start only
docker compose logs garmin-mcp | grep -i "login"
```

`garmin-mcp-auth` needs a terminal for the prompts; `docker compose run` allocates one. Set `GARMIN_IS_CN=true` in `.env` before running it for an account on garmin.cn. Garmin usually emails the MFA code; some accounts use an authenticator app. A login from a new IP address can also trigger a security email from Garmin; that is normal.

### Why not just put the password in `.env`?

`GARMIN_EMAIL`/`GARMIN_PASSWORD` are honoured once, at start, when no tokens exist and only if Garmin does not ask for MFA; most accounts do. Even then the server stops at the first MFA prompt rather than retrying, because repeated logins are what gets an address rate-limited or an account locked. The token login is the designed path: one interactive login, then months of silent refreshes.

## Other troubleshooting

**Tools say "Garmin Connect rate limit" or results are marked stale with "Backing off after a 429".** Garmin returned 429; all background work pauses for 30 minutes and tool calls serve the mirror. It clears by itself. If it recurs daily, the account is probably also used by another client that polls Garmin (a second instance, a third-party app): the budget is shared per account.

**`garmin_sync_status` shows `queue` with thousands of jobs.** Normal during the initial backfill of a long history; the queue drains at about one job per 1.5 s, newest dates first. `coverage` tells how far it has come. Set `GARMIN_HISTORY_START` to a later date to mirror less.

**A dataset keeps failing (`recent_errors`).** Garmin changed or removed an endpoint, or the account lacks the feature (no running tolerance without a compatible watch). Failed jobs retry with growing delays (minutes to a day) and stop costing anything in between; the data that does arrive is unaffected. Report it with the error text.

**`unmapped_fields` lists keys.** Garmin added fields the schema does not map yet. The values are safe in `raw_payload` and readable through `raw_current`; open an issue with the list so a release can add the columns.

**The container restarts with exit status 70.** DuckDB reported a fatal error (`Failed to rollback transaction. Cannot continue operation`, or an internal assertion); the database instance is unusable from that point, so the server logs the error and exits so that Docker restarts it (`restart: unless-stopped`). The database file is consistent: DuckDB rolls back the incomplete transaction at the next open. If it repeats, the usual causes are disk full (check `df` on the volume and the `duckdb-tmp/` spill directory) or memory pressure; raise `GARMIN_MCP_MEMORY` and `DUCKDB_MEMORY_LIMIT` together.

**Out-of-memory kill (exit 137).** The container limit is below what DuckDB was told it may use; see [Memory](#memory).

**`Could not set lock on file`.** Two processes opened the live file: a stdio server and the Docker service on the same `GARMIN_DB_PATH`, or a notebook that opened `garmin.duckdb` instead of `snapshot/garmin.duckdb`. Point other readers at the snapshot.

**The snapshot is old.** It is published hourly when something changed, every six hours during a backfill, and only while `GARMIN_SYNC=on`. `GET /snapshot.duckdb` answers 404 until the first one exists (a few minutes after the first start).

**Behind a reverse proxy, every request is refused with 421 or a host error.** Set `MCP_ALLOWED_HOSTS` to the hostname clients use, comma-separated.

## Importing Garmin's account export

Garmin's account data export (garmin.com, Account, *Manage Your Data*, *Export Your Data*; a zip that arrives by email after a day or two) contains the original FIT files of every activity and every day's wellness files. Importing it decodes them into the mirror without a single API call and makes the sync skip those downloads, which shortens a first backfill of many years from days to hours:

```sh
docker compose stop garmin-mcp                                    # the mirror has one writer
docker compose run --rm -v "$PWD/export.zip:/export.zip:ro" garmin-mcp import-export /export.zip
docker compose start garmin-mcp
```

Activity files are keyed by the activity id in their file name (`<id>_ACTIVITY.fit`) or, failing that, matched to a mirrored activity by start time (within two minutes), so run the import after the activity list has been synced at least once; unmatched files are reported and skipped. Wellness files are keyed by the day of their first monitoring timestamp. Files already in the mirror with the same content are skipped, so the import can be repeated. `--limit N` tries the first N files for a dry run.

The same command accepts a single `.fit` file or a zip of them, which is how to add files from other sources.

## Logs

The server logs to stderr (`docker compose logs garmin-mcp`), one line per login, sync job failure, snapshot, rate limit and tool failure; `LOG_LEVEL=debug` adds each request. `request_log` in the mirror has every request to Garmin for the last 30 days, `sync_log` the outcome of every job.

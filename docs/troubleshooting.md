# Troubleshooting

Most problems show up in one of three places: `garmin-mcp-auth` (logins), `garmin_sync_status` (the sync: `login`, `coverage`, `queue`, `recent_errors`, `unmapped_fields`, `reload_quota`, `pacing`), and the server log (`docker compose logs garmin-mcp`). Start there, then find the symptom below.

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


## Rate limits

**Tools say "Garmin Connect rate limit" or results are marked stale with "Backing off after a 429".** Garmin returned 429; all background work pauses for 30 minutes and tool calls serve the mirror. It clears by itself. If it recurs daily, the account is probably also used by another client that polls Garmin (a second instance, a third-party app): the budget is shared per account.

**Tools answer from the mirror but the result says "stale".** The mirror had a copy and Garmin could not be asked (backoff, login failed, unreachable); the provenance line says which. The data is served as it was last fetched.

## Sync

**`garmin_sync_status` shows `queue` with thousands of jobs.** Normal during the initial backfill of a long history; the queue drains at about one job per 1.5 s, newest dates first. `coverage` tells how far it has come. Set `GARMIN_HISTORY_START` to a later date to mirror less.

**A dataset keeps failing (`recent_errors`).** Garmin changed or removed an endpoint, or the account lacks the feature (no running tolerance without a compatible watch). Failed jobs retry with growing delays (minutes to a day) and stop costing anything in between; the data that does arrive is unaffected. Report it with the error text.

**`unmapped_fields` lists keys.** Garmin added fields the schema does not map yet. The values are safe in `raw_payload` and readable through `raw_current`; open an issue with the list so a release can add the columns.

**Days older than about six months have `sample_count = 0`.** Garmin keeps a day's per-sample series online for about six months and then offloads them; the summaries stay. The mirror recovers those days from the daily wellness FIT files (the `fit_*` tables carry the same series at higher resolution) and spends Garmin's small reload quota (about 30 a day, shown as `reload_quota`) only on days the FIT files do not cover; `request_reload` spends one on a day of your choice. [How sync works](sync.md#the-archive-model) explains it.

## Memory and crashes

**Out-of-memory kill (exit 137).** The container limit is below what DuckDB was told it may use; see [Memory](operations.md#memory).

**The container restarts with exit status 70.** DuckDB reported a fatal error (`Failed to rollback transaction. Cannot continue operation`, or an internal assertion); the database instance is unusable from that point, so the server logs the error and exits so that Docker restarts it (`restart: unless-stopped`). The database file is consistent: DuckDB rolls back the incomplete transaction at the next open. If it repeats, the usual causes are disk full (check `df` on the volume and the `duckdb-tmp/` spill directory) or memory pressure; raise `GARMIN_MCP_MEMORY` and `DUCKDB_MEMORY_LIMIT` together.

**`Could not set lock on file`.** Two processes opened the live file: a stdio server and the Docker service on the same `GARMIN_DB_PATH`, or a notebook that opened `garmin.duckdb` instead of `snapshot/garmin.duckdb`. Point other readers at the snapshot.

## Snapshot and HTTP

**The snapshot is old.** It is published 15 minutes after a new activity, six hours after other changes (`GARMIN_SNAPSHOT_INTERVAL_HOURS`), twelve during a backfill, and only while `GARMIN_SYNC=on`. `GET /snapshot.duckdb` answers 404 until the first one exists (a few minutes after the first start).

**Behind a reverse proxy, every request is refused with 421 or a host error.** Set `MCP_ALLOWED_HOSTS` to the hostname clients use, comma-separated.

**`401 unauthorized` on `/mcp`.** The request did not carry `Authorization: Bearer <MCP_BEARER_TOKEN>`, or carried a different one; `/snapshot.duckdb` uses `SNAPSHOT_TOKEN` instead. Behind the OAuth profile the gateway adds the header itself.

**The Strava sync reports a problem.** `strava_sync_status` lists it under `problems`: strava-mcp's export unreachable or answering 401 (check `STRAVA_EXPORT_URL`, `STRAVA_EXPORT_TOKEN` and that both containers share a network), a route list older than `STRAVA_SYNC_MAX_AGE_HOURS`, or no starred routes at all. See [Strava sync](strava-sync.md#setting-it-up).

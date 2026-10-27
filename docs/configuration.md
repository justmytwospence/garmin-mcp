# Configuration

Everything is an environment variable. In Docker, set them in `.env`: `compose.yaml` hands the whole file to the server (`env_file`), and `.env.example` lists every one with a comment, so a line left commented out keeps the default. Defaults that differ in the image are noted below. A value that cannot be parsed stops the server at start with one line naming the variable.

## Garmin account

| Variable | Default | Meaning |
|---|---|---|
| `GARMINTOKENS` | `~/.garminconnect` (`/data/tokens` in the image) | Directory where `garmin-mcp-auth` stores `garmin_tokens.json` and the server reads it |
| `GARMIN_IS_CN` | `false` | `true` for Garmin Connect China (garmin.cn). Must be the same for `garmin-mcp-auth` and the server; tokens of one region do not work on the other |
| `GARMIN_EMAIL`, `GARMIN_PASSWORD` | unset | First-login fallback used only when no tokens exist and Garmin does not ask for MFA. Never needed once tokens exist; `garmin-mcp-auth` is the normal way |
| `GARMIN_HISTORY_START` | first activity or account creation | Earliest date the mirror backfills, `YYYY-MM-DD` |
| `GARMIN_SYNC` | `on` | `off` serves the mirror and fetches on demand only: no background sync, no snapshots. Booleans accept `on`/`off`, `true`/`false`, `yes`/`no`, `1`/`0` |

## Storage

| Variable | Default | Meaning |
|---|---|---|
| `GARMIN_DB_PATH` | the user data directory: `~/.local/share/garmin-mcp/garmin.duckdb` (Linux, or `$XDG_DATA_HOME`), `~/Library/Application Support/garmin-mcp/garmin.duckdb` (macOS), `%LOCALAPPDATA%\garmin-mcp\garmin.duckdb` (Windows); `/data/garmin.duckdb` in the image | The mirror. FIT files go to `fit/` next to it, DuckDB's spill files to `duckdb-tmp/`. One process at a time |
| `GARMIN_SNAPSHOT_INTERVAL_HOURS` | `6` | How long routine changes (wellness days, refetches) wait before the snapshot is republished. Each publish copies the whole mirror, gigabytes after a few years of FIT data |
| `GARMIN_SNAPSHOT_ACTIVITY_DELAY_MINUTES` | `15` | How soon a new or re-downloaded activity is published (several downloads in a row share one copy) |
| `GARMIN_SNAPSHOT_DIR` | `snapshot/` next to the mirror | Where the read-only snapshot `garmin.duckdb` is published |
| `DUCKDB_MEMORY_LIMIT` | DuckDB's default, 80% of RAM (`512MB` in the image) | DuckDB's buffer pool limit. Always set it in a container; see [Operations](operations.md#memory) |
| `DUCKDB_THREADS` | all cores (`2` in the image) | DuckDB worker threads |

## Serving

| Variable | Default | Meaning |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` (`http` in the image) | `stdio` for a client that spawns the process, `http` for Streamable HTTP |
| `PORT`, `MCP_HOST` | `8000`, `0.0.0.0` | HTTP bind |
| `MCP_BEARER_TOKEN` | unset (open; the server warns at start) | Require `Authorization: Bearer <token>` on `/mcp` (constant-time compare). Required by `compose.yaml` |
| `SNAPSHOT_TOKEN` | unset (not served) | Bearer token for `GET /snapshot.duckdb` (byte ranges supported, so DuckDB's httpfs can attach it) |
| `MCP_ALLOWED_HOSTS` | any | Comma-separated hostnames allowed in the `Host` header, with `localhost` and `127.0.0.1` always allowed. Set it behind a reverse proxy; it turns on the SDK's DNS-rebinding protection |
| `MCP_STATELESS_HTTP` | `false` | Serve each request without a session id (for clients or proxies that cannot keep one) |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |

## Tools

| Variable | Default | Meaning |
|---|---|---|
| `GARMIN_TOOL_PROFILE` | all | `all`, `minimal`, `health`, `activities`, `workouts`, `training`, `nutrition`, or several comma-separated. The mirror tools are in every profile. [Tools reference](reference/tools.md#profiles) lists each |
| `GARMIN_ENABLED_TOOLS` | unset | Comma-separated allowlist: adds to the profile, or alone selects exactly these |
| `GARMIN_DISABLED_TOOLS` | unset | Comma-separated denylist |
| `GARMIN_MCP_CHARACTER_LIMIT` | `60000` | Tool results longer than this are truncated with a note |
| `GARMIN_FIT_DOWNLOAD_DIR`, `GARMIN_FIT_CONFIG` | unset, `~/.garminconnect_fit_config.json` (`/data/downloads`, `/data/fit_config.json` in the image) | Where `download_activity_file` and `download_course_gpx` save files, and the config file `set_fit_download_dir` writes |

## Strava sync (optional)

Off unless `STRAVA_SYNC` is set. See [Strava sync](strava-sync.md) for what it does and its safeguards.

| Variable | Default | Meaning |
|---|---|---|
| `STRAVA_SYNC` | unset (off) | `titles`, `courses`, or `titles,courses` (`all` and `on` mean both) |
| `STRAVA_EXPORT_URL` | unset | [strava-mcp](https://github.com/justmytwospence/strava-mcp)'s live export, `http://<host>:8000/export/sync` (older strava-mcp releases: `/export/garmin-sync`). Set this or `STRAVA_SNAPSHOT` |
| `STRAVA_SNAPSHOT` | unset | Instead of the export: the path of a strava-mcp snapshot (`snapshot/strava.duckdb`, mounted read-only), or its `http(s)://…/snapshot.duckdb` URL. Up to an hour behind |
| `STRAVA_EXPORT_TOKEN` | unset | Bearer token for the export or a snapshot URL (strava-mcp's `SNAPSHOT_TOKEN`). The older name `STRAVA_SNAPSHOT_TOKEN` still works |
| `STRAVA_SYNC_COURSES` | `dry-run` | `on` deletes orphaned Strava-made courses; `dry-run` only reports them |
| `STRAVA_SYNC_TITLES_SINCE` | the day the sync first ran | Rename only activities that started on or after this date, `YYYY-MM-DD` |
| `STRAVA_SYNC_MAX_RENAMES` | `20` | Renames per run; the rest wait for the next run |
| `STRAVA_SYNC_MAX_DELETES` | `10` | Above this many orphans a run deletes nothing and reports it |
| `STRAVA_SYNC_MAX_AGE_HOURS` | `48` | Course deletions are skipped when strava-mcp's route list is older |
| `STRAVA_SYNC_INTERVAL_MINUTES` | `2` | How often the engine runs the sync (a run with nothing to do sends no Garmin request) |
| `STRAVA_COURSE_CONSUMERS` | Strava's consumer id | Comma-separated Garmin consumer ids whose courses count as Strava-made |
| `STRAVA_DEFAULT_NAME_PATTERN` | `^(Morning\|Lunch\|Afternoon\|Evening\|Night) \S.*$` | Regular expression for Strava's automatic activity names, which are never copied to Garmin. The default matches the English ones; Strava localizes them, so set it for an account in another language (validated at start) |

## Compose-only

These shape the stack rather than the server; `compose.yaml` reads them from `.env`.

| Variable | Default | Meaning |
|---|---|---|
| `GARMIN_MCP_VERSION` | `1` | Image tag (`X.Y.Z`, `X.Y` or `X`) |
| `GARMIN_MCP_PORT` | `8000` | Host port for `/mcp` |
| `GARMIN_MCP_MEMORY` | `1536M` | Container memory limit; raise together with `DUCKDB_MEMORY_LIMIT` |
| `OIDC_ISSUER`, `OIDC_JWKS_URL`, `OIDC_CLIENT_ID`, `PUBLIC_URL`, `OAUTH_PORT`, `MCP_BUFFER` | | [OAuth profile](authentication.md#oauth-21-with-your-own-provider) |
| `GRAFANA_ADMIN_USER`, `GRAFANA_ADMIN_PASSWORD`, `GRAFANA_PORT`, `GRAFANA_MEMORY`, `GRAFANA_DUCKDB_PLUGIN_VERSION` | `admin`, `admin`, `3001`, `1024M`, `0.4.6` | [Grafana profile](extras.md#grafana) |
| `NOTEBOOKS_PORT`, `NOTEBOOKS_MEMORY` | `2718`, `1024M` | [Notebooks profile](extras.md#notebooks) |
| `BACKUP_DIR`, `BACKUP_CRON`, `BACKUP_RETENTION_DAYS` | `./backups`, `0 4 * * *`, `14` | [Backup profile](extras.md#backups) |

## The data directory

```
/data
  garmin.duckdb        the live mirror: open by this process only, never copy or back up while it runs
  garmin.duckdb.wal    its write-ahead log (transient)
  duckdb-tmp/          DuckDB spill files (transient)
  snapshot/
    garmin.duckdb      read-only copy, replaced atomically: what everything else reads
  fit/
    activities/<id>.zip      original activity FIT files
    wellness/<date>.zip      daily wellness FIT bundles
  tokens/
    garmin_tokens.json       python-garminconnect's tokens (owner-only)
    garmin-mcp-auth.json     which Garmin domain they belong to
  downloads/                 files saved by download_activity_file and download_course_gpx
  fit_config.json            the download directory set_fit_download_dir chose
```

Outside Docker the same layout sits in the user data directory (`GARMIN_DB_PATH`'s folder) and the tokens in `~/.garminconnect`.

Back up `tokens/`, `fit/` and `snapshot/`. The live file can be rebuilt from `raw_payload` and the FIT files, but the FIT files cannot be re-downloaded quickly (one request every 1.5 s) and the tokens cannot be re-created without a new MFA login.

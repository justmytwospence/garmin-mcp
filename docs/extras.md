# Extras

Three compose profiles read the snapshot the server publishes; a fourth puts OAuth in front of the server ([Authentication](authentication.md#oauth-21-with-your-own-provider)). Each is self-contained and optional: `docker compose --profile grafana --profile notebooks --profile backup up -d` starts them all, `docker compose up -d` without profiles starts none. If you already run Grafana, marimo or a backup tool, the [last section](#bring-your-own) shows how to point them at the data.

## Grafana

```sh
docker compose --profile grafana up -d
```

Grafana (`grafana/grafana:13.0-ubuntu`) with [MotherDuck's DuckDB datasource](https://github.com/motherduckdb/grafana-duckdb-datasource) on `http://localhost:3001` (admin / admin, change it at the first sign-in or set `GRAFANA_ADMIN_PASSWORD`). The data volume is mounted read-only, the datasource opens the snapshot read-only, and a dashboard is provisioned in the *Garmin* folder:

- Recovery: resting heart rate with its 7-day average, overnight HRV against the balanced range, sleep score, sleep stages, Body Battery, stress.
- Activity and training: steps, acute and chronic training load with the optimal band, training readiness, VO2 max (running and cycling), weekly activity hours stacked by type.
- Sync health: age of the last fetch, queue size, errors and rate limits in the last 24 hours, FIT coverage, Garmin requests per day, recent sync errors.

Every panel is a plain SQL query over the snapshot with Grafana's time macros (`$__timeFilter(calendar_date::TIMESTAMP)`), so copy a panel and change the query to add your own; the dashboard is editable in the UI (`allowUiUpdates`), and `deploy/grafana/provisioning/dashboards/garmin.json` is the source to commit changes back to.

Notes:

- The plugin needs glibc, hence the Ubuntu image; it is unsigned, so `GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS` allow-lists it. It is downloaded from the GitHub release before the first start (`GF_PLUGINS_PREINSTALL_SYNC`, 140 MB) into the `grafana-data` volume.
- The datasource path is a DuckDB DSN with `access_mode=read_only`; the service's entrypoint creates two symlinks under `/var/lib/grafana/garmin/` so that plugin versions before 0.4.6 can both parse the DSN and watch the file. Plugin **0.4.6** (October 2026) adds a read-only option and reopens the file when the server replaces the snapshot; until you set `GRAFANA_DUCKDB_PLUGIN_VERSION=0.4.6`, Grafana keeps the snapshot it opened at start and shows a new one after `docker compose restart grafana`.
- The snapshot is replaced atomically by rename, so a running query keeps reading the file it opened and never sees a half-written one.

## Notebooks

```sh
docker compose --profile notebooks up -d
```

A [marimo](https://marimo.io) app on `http://localhost:2718` serving `deploy/notebooks/garmin.py` over the snapshot (image built from `deploy/notebooks/Dockerfile`: marimo 0.25 plus DuckDB, Polars and Altair, pinned):

- Recovery: sleep score, overnight HRV against the balanced range, resting heart rate, six months, shared zoom.
- Training load: acute and chronic load with Garmin's optimal range, training readiness.
- Activity records: heart rate, power, speed and altitude per second for the six most recent activities with a FIT file, one tab each, straight from `fit_record`.

The notebook is bind-mounted, so edits apply on the next page load without a rebuild. To work on it interactively, open it with `marimo edit` against a local copy of the snapshot (`GARMIN_SNAPSHOT=/path/to/garmin.duckdb`); `marimo export html deploy/notebooks/garmin.py` produces a static page with the charts embedded, which the tabs and the Altair zoom survive. The app opens a new read-only connection on each page load, so it picks up a replaced snapshot by itself.

## Backups

```sh
docker compose --profile backup up -d
```

[offen/docker-volume-backup](https://github.com/offen/docker-volume-backup) archives, nightly at 04:00 UTC (`BACKUP_CRON`), the parts of the data volume that are worth keeping into `./backups/garmin-mcp-<timestamp>.tar.gz` (`BACKUP_DIR`), pruning archives older than 14 days (`BACKUP_RETENTION_DAYS`):

| Path in the archive | What | Why |
|---|---|---|
| `backup/garmin-data/tokens/` | `garmin_tokens.json`, the domain marker | Re-creating them needs a new MFA login |
| `backup/garmin-data/fit/` | Original FIT files, one zip per activity and per wellness day | Re-downloading a few years takes days at one request per 1.5 s |
| `backup/garmin-data/snapshot/garmin.duckdb` | The last published snapshot | A complete, consistent copy of the mirror |

The live `garmin.duckdb`, its write-ahead log and DuckDB's temp directory are excluded (`BACKUP_EXCLUDE_REGEXP`): a copy of a database that is being written to is not a backup. The snapshot is at most an hour behind (six during a backfill), and everything it lacks is fetched again from Garmin on the next sync.

To send archives to S3, WebDAV, SSH, Azure Blob or Dropbox instead of `./backups`, add the tool's `AWS_*`, `WEBDAV_*`, `SSH_*`, ... variables to the `backup` service; see its README. Encryption (`GPG_PASSPHRASE`) and notifications are configured the same way.

### Restore

With the stack stopped (`docker compose down`), unpack the archive into the volume and promote the snapshot to the live file:

```sh
docker compose down
docker run --rm -v garmin-mcp_garmin-data:/data -v "$PWD/backups:/backups:ro" alpine sh -c '
  cd /data &&
  tar xzf /backups/garmin-mcp-<timestamp>.tar.gz --strip-components=2 &&
  cp snapshot/garmin.duckdb garmin.duckdb && rm -f garmin.duckdb.wal &&
  chown -R 1000:1000 /data'
docker compose up -d
```

(`garmin-mcp_garmin-data` is the volume name compose derives from the project directory; `docker volume ls` shows it.) The server opens the promoted file, the planner notices whatever changed since the snapshot and fetches it; the FIT files on disk are matched by hash and not downloaded again. To restore on a new machine, create the volume by starting the stack once, stop it, then run the same commands.

## Bring your own

The data is a DuckDB file. Anything that reads DuckDB reads it, two ways:

**Mount the volume**, read-only, into your own Grafana, JupyterLab, marimo or scripts, and open `/data/snapshot/garmin.duckdb` with `read_only=True` (DuckDB refuses a second writer, and the file is replaced by rename, so keep connections short-lived or reopen when the file's inode changes). Only `snapshot/` is needed; `tokens/` should not be reachable from other containers.

```yaml
services:
  myjupyter:
    volumes:
      - garmin-mcp_garmin-data:/garmin:ro
volumes:
  garmin-mcp_garmin-data:
    external: true
```

**Attach over HTTP** from anywhere, with `SNAPSHOT_TOKEN` set on the server. DuckDB's httpfs reads byte ranges, so a query fetches only the blocks it needs:

```sql
INSTALL httpfs; LOAD httpfs;
CREATE SECRET garmin (TYPE http, EXTRA_HTTP_HEADERS MAP {'Authorization': 'Bearer <SNAPSHOT_TOKEN>'});
ATTACH 'https://garmin.example.com/snapshot.duckdb' AS garmin (READ_ONLY);
```

Detach and attach again to see a newer snapshot. For a dashboard that runs many small queries, a local copy is faster: `curl -H 'Authorization: Bearer <SNAPSHOT_TOKEN>' -o garmin.duckdb https://garmin.example.com/snapshot.duckdb` (the `Last-Modified` header tells you whether it changed).

**Back up** `tokens/`, `fit/` and `snapshot/` with whatever you use for volumes; never the live `garmin.duckdb`.

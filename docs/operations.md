# Operations

Running the server day to day: memory, upgrades, importing Garmin's export, logs. Symptoms and their fixes are on the [Troubleshooting](troubleshooting.md) page.

## Memory

DuckDB sizes its buffer pool to 80% of the host's RAM unless told otherwise, which in a container means the kernel's OOM killer. The image sets `DUCKDB_MEMORY_LIMIT=512MB` and `DUCKDB_THREADS=2`, and `compose.yaml` caps the container at 1.5 GB (`GARMIN_MCP_MEMORY`). Measured on a reference setup, a mirror of seven years (2.3 GB file, 74,000 payloads, 1,800 activities with FIT files, 7.7 million `fit_record` rows), peak RSS of the process:

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

Two operations used to break this budget and were changed in 1.0: the snapshot was built with `COPY FROM DATABASE`, which rebuilt every table through the buffer pool (1.5 GB RSS and 160 s on that mirror, out of memory below 1 GB) and is now a checkpoint plus a file copy; and the unmapped-field check parsed every stored payload and now samples the newest 200 per type, once an hour.

Disk: the live file is somewhat larger than the snapshot (DuckDB does not return freed blocks); budget about 3x the snapshot size plus the FIT files (roughly 150 KB per activity, 50 KB per wellness day).

## Upgrades

```sh
docker compose pull
docker compose up -d
```

The image tag in `compose.yaml` follows the major version (`GARMIN_MCP_VERSION=1`); pin `1.2.3` if you prefer explicit upgrades. Releases are tagged `vX.Y.Z` and the [changelog](changelog.md) says what changed. Schema migrations run at start and are recorded in `schema_migration`; a release that changes a projection rebuilds the affected typed tables from the raw store, and one that changes the FIT decoder decodes the stored files again, without fetching anything. Downgrading across a migration is not supported: keep a backup of the snapshot before a major upgrade.

python-garminconnect, which speaks Garmin's private API, moves fast as Garmin changes things. A release that bumps it is the usual fix when a dataset starts failing; `garmin_sync_status` shows which (`recent_errors`).

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

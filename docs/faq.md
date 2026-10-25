# FAQ

## Is my data sent to an LLM provider?

The server sends nothing anywhere on its own: it talks to Garmin Connect to fill the mirror, and answers MCP clients that connect to it. What those clients do with the answers is up to the client. When a client such as Claude Code, ChatGPT or Cursor calls a tool, the result goes into the conversation and therefore to whatever model that client uses, hosted or local. Narrow what a client can see with the [tool profiles](reference/tools.md#profiles) and `GARMIN_DISABLED_TOOLS`, and treat the mirror, the snapshot and the FIT files as the health data they are: never attach them to an issue.

## Can this get my Garmin account locked or rate-limited?

The design is built around not doing that. Requests go out one at a time, about 1.5 s apart, a 429 pauses all background work for 30 minutes, and the server never logs in on its own: you log in once with `garmin-mcp-auth` (with the MFA code), and python-garminconnect refreshes the tokens in place for months. Repeated logins are what gets an address rate-limited or an account locked, which is why `GARMIN_EMAIL`/`GARMIN_PASSWORD` are a one-shot fallback and never retried. If another client also polls your account (a second instance, a third-party app), the budget is shared: see [Troubleshooting](troubleshooting.md#rate-limits).

## Can I mirror several accounts?

One account per server: one mirror, one token store, one process. Run a second instance (another compose project with its own `.env`, volume and port, or a second `GARMIN_DB_PATH` and `GARMINTOKENS` outside Docker) and give each client the server it should talk to.

## Garmin Connect China?

Set `GARMIN_IS_CN=true` before running `garmin-mcp-auth` and keep it set for the server; tokens from garmin.com and garmin.cn are not interchangeable, and a mismatch is reported as `region_mismatch`.

## How long does the first sync take?

The profile, devices and the activity list come first, then the per-range daily summaries, so the recent months are there within hours. Each day's datasets and each activity's detail and FIT file follow, newest first, at about one request every 1.5 s: a few years of history take about a week. Tools work from the first minute and fetch on demand what the mirror does not have yet. Garmin's [account export](operations.md#importing-garmins-account-export) supplies every FIT file at once and shortens a long backfill to hours; `GARMIN_HISTORY_START` limits how far back the mirror goes.

## Does it write to Garmin?

Only when you ask it to. The sync only reads. Writes happen through the write tools you call (renaming or deleting an activity, uploading a workout, logging weight or hydration, and so on; the [tools reference](reference/tools.md) marks each as read, write or destructive) and, if you enable it, the optional [Strava sync](strava-sync.md), which renames activities and deletes orphaned Strava-made courses within documented caps and a dry-run default.

## Where is my data, and how do I delete it?

In Docker, everything is in the `garmin-data` volume (`/data`): the mirror, the snapshot, the FIT files and the tokens. `docker compose down -v` removes the containers and the volume; `docker image rm ghcr.io/justmytwospence/garmin-mcp` the image. Outside Docker, delete the data directory (`~/.local/share/garmin-mcp`, `~/Library/Application Support/garmin-mcp`, or wherever `GARMIN_DB_PATH` pointed) and `~/.garminconnect` (the tokens), then `uv tool uninstall garmin-mirror-mcp`. Nothing is stored anywhere else. Your data on Garmin Connect is untouched either way.

## Does it run on arm64, a Raspberry Pi, a NAS?

The image is built for amd64 and arm64, so a Raspberry Pi 4 or 5 (64-bit OS), an Apple Silicon Mac and most ARM servers run it. The compose file caps the container at 1.5 GB and DuckDB at 512 MB; a machine with 2 GB of RAM is enough for the server alone, the Grafana and notebook profiles want more. See [Memory](operations.md#memory).

## Does the mirror replace a backup of my Garmin data?

It is a faithful local copy of what Garmin returned (every response verbatim, plus the original FIT files), which is more than Garmin's own export contains for most datasets, and the `backup` profile archives it nightly. It is still a copy of a live account: the [snapshot](data/index.md#the-snapshot) is the file to keep.

## Which tools does it have, and are they the same as upstream?

<!-- count:tools -->160<!-- /count --> tools: the <!-- count:tools-upstream -->157<!-- /count --> of [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp), with the same names, parameters and descriptions, answered from the mirror, plus `garmin_query`, `garmin_schema` and `garmin_sync_status`. The [Strava sync](strava-sync.md) adds <!-- count:tools-strava -->3<!-- /count --> more when it is enabled. The [tools reference](reference/tools.md) lists them all.

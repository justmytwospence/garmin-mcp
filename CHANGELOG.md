# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [semantic versioning](https://semver.org/).

## [Unreleased]

### Fixed

- Out-of-memory errors on a large mirror: the activity listing, activity re-fetches and the Strava sync failed with "Out of Memory Error" under the image's limits, so new activities stopped reaching the mirror. Reading any payload body by type and key made DuckDB read the whole body column a 2,048-row vector at a time, megabyte-sized activity details included, and the activity projection joins three such reads (a 20-row projection needed more than 1.5 GB). Scoped projections and the mirror's reads now fetch bodies by hash through a new `content_hash` index (a migration adds it on start), and a course detail replaces its row with a delete and an insert instead of `INSERT OR REPLACE`. On a 7 GB mirror, projecting 100 activity summaries now takes 0.6 s and under 350 MB of process memory.

## [1.4.0] - 2026-10-26

### Added

- Local MCP usage/schema/coverage/status resources, bounded cache-only activity/day templates and read-only activity/recovery review prompts. The guide lists URI examples and prompt names for clients without template discovery; tool descriptions also carry API-budget guidance.
- Offline wire-contract, SQL/export and fake-clock API-budget regressions; release publication now requires successful lint, tests and generated-documentation checks.

### Fixed

- Agent SQL now runs inside `BEGIN TRANSACTION READ ONLY` on the actual executing cursor, with serialized access, guaranteed rollback/close and timeout cleanup. Wrapped non-read statements are rejected too, including `EXPLAIN ANALYZE COPY` filesystem writes.
- Filesystem export tools advertise destructive-capable writes and reject mirror, snapshot, FIT, authentication/configuration and symlink destinations, using safe atomic replacement for legitimate exports.
- Complete explicit tool hints distinguish local reads/configuration from upstream operations, replacement edits and append-only writes. Compatible text/provenance blocks are retained alongside bounded structured JSON results.
- Retry accounting records each attempt once before backoff; `Retry-After` is honored where available, ambiguous writes are not replayed and library method retries are disabled. Timestamped status distinguishes unknown upstream quota from local counts, pacing and the separate conservative reload allowance. Reload admission is serialized and lost outcomes are reserved across restarts.

## [1.3.0] - 2026-10-25

### Added

- `STRAVA_DEFAULT_NAME_PATTERN`: the regular expression for Strava's automatic activity names, which the Strava sync never copies onto Garmin. The built-in pattern matches the English names ("Morning Run"); Strava localizes them, so accounts in other languages set their own. Validated at start and shown by `strava_sync_status`.
- The server warns at start when it serves HTTP without `MCP_BEARER_TOKEN`.
- A documentation site with a home page, an FAQ, a troubleshooting page, and client setup for Claude Code, Claude Desktop, claude.ai, Cursor, VS Code, Codex, ChatGPT, Gemini CLI and pi. Generated pages keep the tool and table counts quoted in the prose current.
- A `vX.Y.Z` tag now also creates the GitHub Release (notes from this file) and publishes the package to PyPI.

### Changed

- The distribution is `garmin-mirror-mcp` on PyPI (`uvx --from garmin-mirror-mcp garmin-mcp`); the `garmin-mcp` and `garmin-mcp-auth` commands and the `garmin_mcp` package keep their names. The name `garmin-mcp` belongs to another project on PyPI.
- Outside Docker the mirror defaults to the user's data directory (`~/.local/share/garmin-mcp`, `~/Library/Application Support/garmin-mcp`, `%LOCALAPPDATA%\garmin-mcp`) instead of `./state` under the working directory. Set `GARMIN_DB_PATH` to keep an existing location.
- The image sets `GARMIN_FIT_DOWNLOAD_DIR=/data/downloads` and `GARMIN_FIT_CONFIG=/data/fit_config.json`, so downloaded files and the download-directory setting live on the volume.
- `compose.yaml` hands the whole `.env` to the server (`env_file`), so every documented variable reaches it; `.env.example` lists each one with its default. Before, `MCP_STATELESS_HTTP`, `GARMIN_FIT_CONFIG` and the `STRAVA_SYNC_*` caps were silently ignored in Docker.
- The Strava sync's bearer token is `STRAVA_EXPORT_TOKEN` (`STRAVA_SNAPSHOT_TOKEN` still works) and the documented export path is strava-mcp's `/export/sync`.
- Boolean settings (`GARMIN_SYNC`, `GARMIN_IS_CN`, `MCP_STATELESS_HTTP`) accept `on`/`off`, `true`/`false`, `yes`/`no` and `1`/`0`; any other value, like a bad `GARMIN_HISTORY_START`, `DUCKDB_THREADS` or `GARMIN_MCP_CHARACTER_LIMIT`, stops the server at start with one line naming the variable instead of a traceback.
- `LICENSE` is the plain MIT text; the attribution of the code vendored from Taxuspt/garmin_mcp moved to `NOTICE`, which the package ships.

### Fixed

- `add_weigh_in_with_timestamps` without timestamps dated the weigh-in in the container's clock (UTC in the image); it now uses the profile's time zone, like the rest of the sync.

## [1.2.0] - 2026-10-25

### Changed

- The Strava sync can read strava-mcp's live export (`STRAVA_EXPORT_URL`, strava-mcp 4.2+) instead of its hourly snapshot, and runs every 2 minutes (was 15): a rename on Strava reaches Garmin in about 2 minutes, an unstarred route's course goes within about 17. A run with nothing to do sends no Garmin request; the course list is fetched when the starred routes change, before any deletion, and otherwise hourly. A just-named Strava activity the mirror has not listed yet asks for the activity listing once.
- The Strava sync ignores activities deleted on Strava.

## [1.1.0] - 2026-10-25

### Added

- Optional Strava sync (`STRAVA_SYNC`), reading a strava-mcp snapshot rather than Strava itself. It renames Garmin activities to their Strava names, leaving Strava's automatic names and hand edits on Garmin alone. It also deletes courses Strava's route sync made once their route is unstarred, as a dry run until `STRAVA_SYNC_COURSES=on`, with a per-run cap and guards against a stale or empty snapshot. New tools `strava_sync_status`, `strava_sync_plan` and `strava_sync_apply`, registered only when it is enabled. See the Strava sync page.

## [1.0.1] - 2026-10-24

### Fixed

- After a rename (or a description, type or event-type edit), the mirror could keep the old value, because the name always came from the activity list even when the re-fetched detail was newer. Edited fields now come from whichever payload is newer.

## [1.0.0] - 2026-10-24

The first release meant for other people: a self-hosted compose stack with documentation. Everything below the "Changed" heading summarises the 0.1.x line it grew out of.

### Added

- Compose stack with optional, self-contained profiles: `oauth` (agentgateway as an OAuth 2.1 resource server for any OIDC provider), `grafana` (MotherDuck's DuckDB datasource over the read-only snapshot with a provisioned dashboard), `notebooks` (a marimo app over the snapshot), `backup` (offen/docker-volume-backup archiving tokens, FIT files and the snapshot). `.env.example` documents every variable.
- Documentation site (MkDocs Material, GitHub Pages): quickstart, configuration, authentication with provider notes for Authelia, Authentik, Keycloak and Pocket ID, client setup for Claude Code, Claude Desktop, claude.ai, Cursor and pi, how sync works, the data model with a generated schema reference and ERDs, extras, operations, and a generated reference of all 160 tools.
- Every login failure (no tokens, expired or revoked tokens, wrong password, MFA required, MFA code rejected, Garmin's login rate limit, region mismatch, legacy tokens, unreachable) is classified into a plain message and the exact command that fixes it, shown by `garmin-mcp-auth`, in the server log, in tool errors and in `garmin_sync_status`. `garmin-mcp-auth` records which Garmin domain the tokens belong to, so a `GARMIN_IS_CN` mismatch is explained, and `docker compose run --rm garmin-mcp garmin-mcp-auth` works through the image's entrypoint.
- A fatal DuckDB error (`Failed to rollback transaction. Cannot continue operation`, internal assertions) ends the process with status 70 so the container restarts, instead of every later statement failing.
- Stored FIT files decoded by an older parser version are decoded again from disk, so a release that adds FIT columns needs no downloads.
- `CONTRIBUTING.md`, `SECURITY.md`, issue templates.

### Changed

- Snapshots are published by `CHECKPOINT` plus a file copy instead of `COPY FROM DATABASE`, which rebuilt every table through the buffer pool (1.5 GB RSS and minutes on a 2 GB mirror, out of memory under 1 GB).
- The unmapped-field check behind `garmin_sync_status` samples the newest 200 payloads per type (fewer for large ones) instead of parsing every stored body, and its answer is reused for an hour.
- The image's `DUCKDB_MEMORY_LIMIT` default is 512 MB (was 384 MB), sized for the 1.5 GB container limit `compose.yaml` sets; memory is documented with measurements.
- The README is a short introduction; the schema diagrams moved to the documentation site.

## [0.1.4] - 2026-10-24

- Archive reloads are requested only for days the wellness FIT files do not cover.

## [0.1.3] - 2026-10-23

- FIT sessions, laps, splits and lengths without a `message_index` fall back to their position.

## [0.1.2] - 2026-10-23

- The server reports the installed package version.
- Projections scope both sides of the raw join, so each job reads only its own payloads.

## [0.1.1] - 2026-10-22

- Wide projections parse each payload once, so a page of activities fits the memory limit.

## [0.1.0] - 2026-10-22

Initial release: the DuckDB mirror (raw store, projections into typed tables, FIT decoding), the paced sync engine with its planner and archive reloads, cache-first tools under the upstream names plus `garmin_query`, `garmin_schema` and `garmin_sync_status`, the snapshot publisher with HTTP byte-range serving, the auth CLI, the account-export import, the Docker image and the CI and release workflows.

[Unreleased]: https://github.com/justmytwospence/garmin-mcp/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/justmytwospence/garmin-mcp/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/justmytwospence/garmin-mcp/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/justmytwospence/garmin-mcp/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/justmytwospence/garmin-mcp/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/justmytwospence/garmin-mcp/compare/v0.1.4...v1.0.0
[0.1.4]: https://github.com/justmytwospence/garmin-mcp/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/justmytwospence/garmin-mcp/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/justmytwospence/garmin-mcp/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/justmytwospence/garmin-mcp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/justmytwospence/garmin-mcp/releases/tag/v0.1.0

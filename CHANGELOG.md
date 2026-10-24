# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [semantic versioning](https://semver.org/).

## [Unreleased]

## [1.0.0] - unreleased

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

[Unreleased]: https://github.com/justmytwospence/garmin-mcp/compare/v0.1.4...HEAD
[1.0.0]: https://github.com/justmytwospence/garmin-mcp/compare/v0.1.4...HEAD
[0.1.4]: https://github.com/justmytwospence/garmin-mcp/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/justmytwospence/garmin-mcp/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/justmytwospence/garmin-mcp/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/justmytwospence/garmin-mcp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/justmytwospence/garmin-mcp/releases/tag/v0.1.0

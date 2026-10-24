# Security

## What this server holds

The mirror is a complete copy of one person's Garmin Connect account: location traces, heart rate, sleep, weight, and the tokens that give access to the account. Treat the data volume accordingly.

- The token file (`tokens/garmin_tokens.json`) is written owner-only and is never read by SQL: DuckDB's file access is restricted to the snapshot directory before any tool runs (`allowed_directories`, `enable_external_access = false`, locked).
- `garmin_query` accepts one `SELECT`/`EXPLAIN` statement per call on a separate cursor, interrupted after 30 s, with no file or network access from SQL.
- `/mcp` requires `MCP_BEARER_TOKEN` when set (constant-time compare; `compose.yaml` requires it). `/snapshot.duckdb` requires its own `SNAPSHOT_TOKEN` and is off without one. `/health` is open and carries no data.
- `MCP_ALLOWED_HOSTS` turns on the MCP SDK's Host-header check (DNS-rebinding protection); set it behind a reverse proxy.
- The OAuth profile keeps the server off the public network: only agentgateway is exposed, it validates the provider's JWTs, and it forwards with the backend bearer from a compose secret.
- The container runs as an unprivileged user with `no-new-privileges`; the Grafana and notebook profiles mount the data volume read-only.

Expose the server to the internet only behind TLS, with either the bearer token or the OAuth profile. Do not share a snapshot: it contains everything.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private vulnerability reporting on the repository (Security, *Report a vulnerability*), or email the maintainer at the address on the GitHub profile. Include what you found, how to reproduce it, and the version. You should hear back within a week; fixes ship as a patch release with a changelog entry crediting you unless you prefer otherwise.

## Scope

In scope: this repository's code, the image, the compose stack and its profile configurations. Out of scope: Garmin Connect itself, python-garminconnect, agentgateway, Grafana, marimo and other upstream software (report those upstream), and deployments that disabled the protections above.

# Canonical export

garmin-mcp can write every activity's recorded samples in a small, stable format meant for apps that draw effort curves or maps: one manifest plus one Parquet file per activity. It follows the canonical activity schema v1 of the curves app, so a reader needs no knowledge of the mirror's tables.

## Turning it on

| Setting | Default | Meaning |
|---|---|---|
| `GARMIN_EXPORT` | `off` | `canonical` writes the export after every snapshot publish (and once at startup when a snapshot exists) |
| `GARMIN_EXPORT_DIR` | `<data dir>/export` | where it is written; the export itself is `canonical-v1/` inside it |
| `EXPORT_TOKEN` | unset | bearer token for the HTTP route; without it the route is disabled |

The exporter reads the published snapshot over its own read-only connection, so it never blocks the mirror. It rewrites only activities whose FIT data changed (a fingerprint of the record count and FIT file ids) and removes deleted ones, so a routine publish writes almost nothing.

## Layout and URLs

```
canonical-v1/manifest.json                   GET /export/canonical/v1/manifest.json
canonical-v1/activities/garmin-<id>.parquet  GET /export/canonical/v1/activities/garmin-<id>.parquet
```

Both need `Authorization: Bearer <EXPORT_TOKEN>`.

A browser app on another origin needs CORS: set `EXPORT_CORS_ORIGINS` to its origin(s). The export routes then answer the preflight (including Private Network Access) and carry `Access-Control-Allow-Origin` on every response, 401 and 404 included. No other route gets CORS headers.

`manifest.json` has `schema_version` (1), `importer`, `generated_at` and `activities`, one entry per activity: `id` (`garmin:<id>`), `sources`, `start_time` (UTC, the first record), `utc_offset_min`, `sport` (canonical), `source_sport` (Garmin's type), `name`, `device`, `indoor`, `manual`, `cadence_unit`, `hr_sensor` (a heart-rate strap or armband was connected), `n` (rows), `sha256` and `fingerprint` of the file, `updated_at` and `file`.

Each Parquet file (snappy) has one row per recorded second: `t` (INTEGER seconds from the first record), `lat`, `lng` (INTEGER degrees x 1e7), `alt` and `dist` (DOUBLE metres), `hr`, `power`, `cadence` (FLOAT; cadence as the FIT file counts it, one foot's cycles on foot). Missing values are nulls. When a second has several records, the one with the greatest distance is kept, and among equal distances the earliest record.

## Signing in from a browser app

Effort Curve connects with an OAuth 2.1 authorization-code flow (PKCE), so the user never pastes a token into the app. `GET /export/canonical/v1/auth` (no authentication, CORS) describes the sign-in: `{mode, issuer, authorization_endpoint, token_endpoint, client_id, scope}`.

- **Built-in** (default): `/export/canonical/v1/oauth/authorize` shows a one-page approval where the user enters `EXPORT_TOKEN` once; `/export/canonical/v1/oauth/token` issues signed access (1 h) and refresh (90 days, rotated) tokens. The redirect must be `<origin>/oauth/callback` with the origin in `EXPORT_CORS_ORIGINS`. Changing `EXPORT_TOKEN` revokes every token.
- **OIDC**: set `EXPORT_OIDC_ISSUER` (and optionally `EXPORT_OIDC_CLIENT_ID`, default `effortcurve`). The app signs in at your provider and the export accepts its JWTs whose `client_id` or `azp` is that client. Register a public PKCE client with the redirect `https://effortcurve.com/oauth/callback`, scopes `openid offline_access`, grants `authorization_code` and `refresh_token`, and allow the app's origin in the provider's CORS settings for the token endpoint (Authelia: `identity_providers.oidc.cors.endpoints: [token, revocation, userinfo]` and `allowed_origins`).

`Bearer EXPORT_TOKEN` keeps working for proxies and scripts.

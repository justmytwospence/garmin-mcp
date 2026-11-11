# Canonical export

garmin-mcp can write every activity's recorded samples in a small, stable format meant for apps that draw effort curves or maps: one manifest plus one Parquet file per activity. It follows the canonical activity schema v1 of the curves app, so a reader needs no knowledge of the mirror's tables.

## Turning it on

| Setting | Default | Meaning |
|---|---|---|
| `GARMIN_EXPORT` | `off` | `canonical` writes the export after every snapshot publish (and once at startup when a snapshot exists) |
| `GARMIN_EXPORT_WEATHER` | `on` | model the weather along each outdoor activity's track (see below); `off` leaves `weather` null |
| `GARMIN_EXPORT_DIR` | `<data dir>/export` | where it is written; the export itself is `canonical-v1/` inside it |
| `EXPORT_TOKEN` | unset | bearer token for the HTTP route; without it the route is disabled |

The exporter reads the published snapshot over its own read-only connection, so it never blocks the mirror. It rewrites only activities whose FIT data changed (a fingerprint of the record count and FIT file ids) and removes deleted ones, so a routine publish writes almost nothing.

## Layout and URLs

```
canonical-v1/manifest.json                   GET /export/canonical/v1/manifest.json
canonical-v1/segments.json                   GET /export/canonical/v1/segments.json
canonical-v1/activities/garmin-<id>.parquet  GET /export/canonical/v1/activities/garmin-<id>.parquet
```

All need `Authorization: Bearer <EXPORT_TOKEN>`.

A browser app on another origin needs CORS: set `EXPORT_CORS_ORIGINS` to its origin(s). The export routes then answer the preflight (including Private Network Access) and carry `Access-Control-Allow-Origin` on every response, 401 and 404 included. No other route gets CORS headers.

`manifest.json` has `schema_version` (1), `importer`, `generated_at` and `activities`, one entry per activity: `id` (`garmin:<id>`), `sources`, `start_time` (UTC, the first record), `utc_offset_min`, `sport` (canonical), `source_sport` (Garmin's type), `name`, `device`, `indoor`, `manual`, `cadence_unit`, `hr_sensor` (a heart-rate strap or armband was connected), `tags` (`["race"]` when Garmin's event type is race, else `[]`), `weather` (modelled weather along the track, or null; see Weather below), `segment_efforts` (see Segments below), `n` (rows), `sha256` and `fingerprint` of the file, `updated_at` and `file`.

Each Parquet file (snappy) has one row per recorded second: `t` (INTEGER seconds from the first record), `lat`, `lng` (INTEGER degrees x 1e7), `alt` and `dist` (DOUBLE metres), `hr`, `power`, `cadence` (FLOAT; cadence as the FIT file counts it, one foot's cycles on foot). Missing values are nulls. When a second has several records, the one with the greatest distance is kept, and among equal distances the earliest record.

## Segments

Each entry's `segment_efforts` lists the segments the watch timed live: one effort per FIT `segment_lap` message with status `end` and a `uuid` (a lap with status `fail`, or without a uuid, is skipped), `{segment, t0, t1, from: "device", src: "garmin:<activity id>"}`, sorted by `t0` then `segment`. `t0` and `t1` are integer seconds from the entry's `start_time` (the first record, the same zero as the Parquet `t`) with `0 <= t0 < t1`. `segment` is `strava:<uuid>` when the lap's manufacturer is `strava` and `garmin:<uuid>` otherwise. The list is `[]` when there are none; there is no `pr_rank` (only Strava ranks efforts).

`segments.json`, written beside `manifest.json`, is `{schema_version: 1, importer, generated_at, segments}`, with each referenced segment once, sorted by id: `id`, `name` and the `start` and `end` points (`[lat, lng]` degrees, 6 decimals) from its newest lap, `sport` (that of its first effort's activity), `distance` (the median of its laps' total distance, metres), `starred` (false), `from` (`"device"`) and `created_from` (null).

## Weather

Each entry's `weather` is the modelled weather along the track, or `null` for indoor and manual activities, activities without a track, and with `GARMIN_EXPORT_WEATHER=off`. It is Open-Meteo's model at one point every 10 minutes of the track, asked for at the recorded altitude (`t` seconds from `start_time`, `elev` m, `temp` and `dew` degrees C, `wind` m/s, `wind_dir` degrees), not a sensor reading; the algorithm is the curves app's schema SPEC (Weather), which `test/fixtures/weather-v1-fixture.json` pins. Positions, altitudes and times go to Open-Meteo's free API; the export asks for at most 2,000 locations a publish, newest activities first, reads the limits Open-Meteo reports in its `X-RateLimit-*` headers and pauses when less than a tenth of the tightest window (minute, hour or day) is left or when it answers 429 (for its `Retry-After`; a pause longer than two minutes ends the run and is kept in `weather-v1.json`), so other clients on the same IP address are adapted to, and so a large history fills in over a few days. The results are kept in `weather-v1.json` beside the export directory (never served), keyed by each activity's fingerprint, so an unchanged activity is never asked about again. A failure is logged and never fails the export. The data is [Open-Meteo.com](https://open-meteo.com) (CC BY 4.0, non-commercial use on the free tier).

## Signing in from a browser app

Effort Curve connects with an OAuth 2.1 authorization-code flow (PKCE), so the user never pastes a token into the app. `GET /export/canonical/v1/auth` (no authentication, CORS) describes the sign-in: `{mode, issuer, authorization_endpoint, token_endpoint, client_id, scope}`.

- **Built-in** (default): `/export/canonical/v1/oauth/authorize` shows a one-page approval where the user enters `EXPORT_TOKEN` once; `/export/canonical/v1/oauth/token` issues signed access (1 h) and refresh (90 days, rotated) tokens. The redirect must be `<origin>/oauth/callback` with the origin in `EXPORT_CORS_ORIGINS`. Changing `EXPORT_TOKEN` revokes every token.
- **OIDC**: set `EXPORT_OIDC_ISSUER` (and optionally `EXPORT_OIDC_CLIENT_ID`, default `effortcurve`). The app signs in at your provider and the export accepts its JWTs whose `client_id` or `azp` is that client. Register a public PKCE client with the redirect `https://effortcurve.com/oauth/callback`, scopes `openid offline_access`, grants `authorization_code` and `refresh_token`, and allow the app's origin in the provider's CORS settings for the token endpoint (Authelia: `identity_providers.oidc.cors.endpoints: [token, revocation, userinfo]` and `allowed_origins`).

`Bearer EXPORT_TOKEN` keeps working for proxies and scripts.

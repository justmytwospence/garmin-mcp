---
hide:
  - navigation
  - toc
---

# Your Garmin data, mirrored and ready to ask

**garmin-mcp** is an [MCP](https://modelcontextprotocol.io) server for Garmin Connect that answers from a complete local copy of your account instead of calling Garmin for every question. A paced background sync fills one [DuckDB](https://duckdb.org) file with everything Garmin Connect returns for you and keeps it fresh; the tools answer from it in milliseconds, and the whole thing is yours to query with SQL.

[Get started](quickstart.md){ .md-button .md-button--primary }
[GitHub](https://github.com/justmytwospence/garmin-mcp){ .md-button }

<!-- screenshots -->

## What you get

<div class="grid cards" markdown>

-   :material-database:{ .lg .middle } **A complete mirror**

    ---

    90 datasets: every activity with laps, splits, zones, weather, gear and sets, the original FIT files decoded to per-second records, every day's heart rate, stress, Body Battery, sleep, respiration, SpO2, HRV, readiness, training status and VO2 max, plus the profile, devices, gear, records, badges, goals, workouts, calendar and courses.

-   :material-speedometer:{ .lg .middle } **Fast, and gentle on Garmin**

    ---

    Tools read the mirror. Garmin is asked only for data that can still change and is stale for its kind: one request at a time, 1.5 s apart, a 30-minute pause on any 429, and never a login on its own.

-   :material-table-column:{ .lg .middle } **Typed tables and SQL**

    ---

    Every response is kept verbatim with its history, then projected into typed tables with a description and unit on every column. `garmin_query` runs read-only SQL over all of it.

-   :material-swap-horizontal:{ .lg .middle } **Drop-in tool names**

    ---

    The <!-- count:tools-upstream -->157<!-- /count --> tools of [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) with the same names, parameters and descriptions, so existing prompts and client configurations keep working, plus <!-- count:tools-mirror -->3<!-- /count --> for the mirror itself. Every result ends with a provenance line.

-   :material-camera-timer:{ .lg .middle } **A snapshot for everything else**

    ---

    A read-only copy of the database is published next to the mirror and, optionally, over HTTP. Compose profiles ship Grafana dashboards, marimo notebooks and nightly backups over it.

-   :material-run-fast:{ .lg .middle } **Optional Strava sync**

    ---

    With [strava-mcp](https://justmytwospence.github.io/strava-mcp/) next to it, names you give activities on Strava reach Garmin, and courses Strava's route sync left behind are cleaned up.

-   :material-shield-key:{ .lg .middle } **Bearer token or OAuth 2.1**

    ---

    A static token for the clients you configure yourself, or an OAuth profile with your own OpenID Connect provider for claude.ai, ChatGPT and phones.

-   :material-docker:{ .lg .middle } **One `docker compose up`, one login**

    ---

    An image for amd64 and arm64, a compose file with optional profiles, and a one-time `garmin-mcp-auth` that handles Garmin's MFA. Or `uvx --from garmin-mirror-mcp garmin-mcp` without Docker.

</div>

## Things you can ask

- "How has my resting heart rate moved since 2021, by quarter?"
- "Which weeks this year had the highest training load, and how did I sleep those weeks?"
- "Compare my last five long runs: pace, average heart rate, cadence and the split where I faded."
- "Show the power duration curve of yesterday's ride and how it compares to my best 20 minutes this season."
- "When does my Body Battery usually bottom out, and what did I do on the days it recovered fastest?"
- "List every activity on the shoes I bought in March and the total distance on them."
- "Build a 10 km progression workout for Thursday and put it on my calendar."
- "Run `SELECT activity_type, count(*), round(sum(distance)/1000) AS km FROM activity WHERE deleted_at IS NULL GROUP BY 1 ORDER BY 3 DESC`."

Every answer ends with a provenance line: served from the mirror (and how old it is), fetched from Garmin just now, or stale because Garmin could not be asked.

## Why a mirror

Garmin Connect has no public API. [python-garminconnect](https://github.com/cyberjunky/python-garminconnect) speaks the private one the Connect app uses, which Garmin rate-limits, changes without notice, and protects with logins that need an MFA code. A stateless server that calls Garmin for every tool call is slow, trips rate limits during a long conversation, and cannot answer questions that span years.

The mirror turns that around. Most calls read the mirror and cost no Garmin request; data that can still change is fetched on demand when it is stale for its kind, and the stored copy is served, marked stale, when Garmin cannot be asked. Every response lands verbatim in `raw_payload` (a new row only when it changed, so it doubles as edit history) and is projected into typed tables. You log in once; the tokens refresh in place for months and the server never logs in on its own. And it is just a DuckDB file: Grafana, notebooks, pandas, the DuckDB CLI, anything that reads DuckDB reads your data.

## How it compares

| | garmin-mcp | [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) | [GarminDB](https://github.com/tcgoetz/GarminDB) | [python-garminconnect](https://github.com/cyberjunky/python-garminconnect), [garth](https://github.com/matin/garth) |
|---|---|---|---|---|
| What it is | MCP server over a local mirror | MCP server calling Garmin per tool call | Downloader and SQLite database with its own CLI and plots | Python libraries for the Connect API |
| Tool call cost | Mirror read; Garmin only when stale | One or more Garmin requests each | Not an MCP server | Not an MCP server |
| Questions spanning years | From the mirror | Limited by rate limits and per-call ranges | From the database | Your own code |
| Data kept | Every response verbatim plus typed tables and decoded FIT files, in DuckDB | None | Summaries, monitoring and activities in SQLite | None |
| Tool set | The upstream tools plus SQL over the mirror | <!-- count:tools-upstream -->157<!-- /count --> tools, workout builders, FIT analyses | Not applicable | Not applicable |
| Other readers | Read-only snapshot for Grafana, notebooks, backups, httpfs | Not applicable | SQLite file | Not applicable |

Other Garmin MCP servers exist with smaller tool sets and the same stateless design as upstream; the comparison is the same as for the upstream column. garmin-mcp vendors upstream's tool definitions, builders and analyses (see [NOTICE](https://github.com/justmytwospence/garmin-mcp/blob/main/NOTICE)) and answers them from the mirror.

## Requirements

- Docker with Compose v2, or Python 3.12+ with [uv](https://docs.astral.sh/uv/). Images are built for amd64 and arm64 (a Raspberry Pi 4 or 5 works).
- About 1.5 GB of RAM for the default compose stack (the container limit; DuckDB is capped at 512 MB and the rest is headroom for parsing), less without Docker.
- Disk: roughly 3x the snapshot size plus the FIT files; a multi-year account with FIT files comes to a few GB.
- A Garmin Connect account. MFA is supported through the one-time interactive login; Garmin Connect China through `GARMIN_IS_CN`.

Questions about privacy, rate limits, several accounts and uninstalling are in the [FAQ](faq.md).

## Works well with strava-mcp

[strava-mcp](https://justmytwospence.github.io/strava-mcp/) is the same idea for Strava: a local DuckDB mirror of your Strava account behind an MCP server. Run both and a client can ask across them. With the optional [Strava sync](strava-sync.md), garmin-mcp also reads strava-mcp's export to carry activity names from Strava onto Garmin and to remove the courses Strava's route sync leaves behind, without any Strava credentials of its own.

## Credits

Tool definitions, descriptions, the workout builders and validators, the FIT analyses, the challenge mappings and the nutrition tools are adapted or vendored from [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) (MIT). Garmin Connect access goes through [python-garminconnect](https://github.com/cyberjunky/python-garminconnect) (MIT); FIT decoding through Garmin's [FIT SDK](https://developer.garmin.com/fit/). Garmin's API is unofficial; this project is not affiliated with, endorsed by or supported by Garmin, and is meant for one person's own data.

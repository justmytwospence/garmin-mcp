# Quickstart

You need Docker with Compose v2 (or Python 3.12 with uv, see [Without Docker](#without-docker)), a Garmin Connect account, and a way to receive its MFA code (Garmin emails one on most logins). Everything the server keeps lives in one named volume.

## 1. Configure

```sh
git clone https://github.com/justmytwospence/garmin-mcp.git
cd garmin-mcp
cp .env.example .env
```

Open `.env` and set `MCP_BEARER_TOKEN` to a secret your MCP clients will send (`openssl rand -hex 32` makes a good one). Everything else has a default; [Configuration](configuration.md) lists each setting.

## 2. Log in once

```sh
docker compose run --rm garmin-mcp garmin-mcp-auth
```

It asks for your Garmin Connect email and password, then for the MFA code Garmin sends, and saves the tokens into the volume (`/data/tokens/garmin_tokens.json`, owner-only). python-garminconnect refreshes them in place for months; the server never logs in on its own. Accounts on Garmin Connect China set `GARMIN_IS_CN=true` in `.env` first.

`docker compose run --rm garmin-mcp garmin-mcp-auth --check` verifies the stored tokens at any time. Every way a login can fail is explained, with the command that fixes it, in [Troubleshooting](troubleshooting.md#login-problems).

## 3. Start the server

```sh
docker compose up -d
```

The server listens on `http://localhost:8000/mcp` (Streamable HTTP) with a health check at `/health`, reads the tokens, and starts the backfill. (If you started it before logging in, `docker compose restart garmin-mcp` makes it pick the tokens up: it reads them at start only.)

## 4. Connect a client

Streamable HTTP with the bearer token, for Claude Code:

```sh
claude mcp add --transport http garmin http://localhost:8000/mcp \
  --header "Authorization: Bearer $MCP_BEARER_TOKEN"
```

Or in any client's JSON configuration:

```json
{
  "mcpServers": {
    "garmin": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer <MCP_BEARER_TOKEN>" }
    }
  }
}
```

[Clients](clients.md) has the equivalent for Claude Desktop, claude.ai (which needs the [OAuth profile](authentication.md#oauth-21-with-your-own-provider)), Cursor, VS Code, Codex, ChatGPT, Gemini CLI and others.

## 5. Watch the first sync

Ask the client to call `garmin_sync_status`, or look at the log:

```sh
docker compose logs -f garmin-mcp
```

The first run loads the profile and lists every activity, then backfills newest first: the fast per-range daily summaries, then each day's datasets and each activity's detail and FIT file, then the slow snapshots. Requests go out one at a time, about 1.5 s apart, so a few years of history take about a week; the last months are there within hours and tools work from the first minute, fetching on demand what the mirror does not have yet. [How sync works](sync.md) explains the order and the pacing; Garmin's [account export](operations.md#importing-garmins-account-export) shortens a long backfill to hours.

## 6. Optional: dashboards, notebooks, backups

Each is a compose profile over the read-only snapshot the server publishes:

```sh
docker compose --profile grafana --profile backup up -d
```

See [Extras](extras.md). To expose the server on the internet for claude.ai, ChatGPT or a phone, read [Authentication](authentication.md) first.

## Without Docker

The package is `garmin-mirror-mcp` on PyPI (published from the next release on; the name `garmin-mcp` belongs to another project). The commands are `garmin-mcp` and `garmin-mcp-auth`.

```sh
uv tool install garmin-mirror-mcp==1.5.1    # or: uvx --from garmin-mirror-mcp==1.5.1 garmin-mcp-auth
garmin-mcp-auth                              # once; tokens go to ~/.garminconnect
garmin-mcp                                   # stdio server, for Claude Desktop and similar clients
MCP_TRANSPORT=http MCP_BEARER_TOKEN=... garmin-mcp   # or the HTTP service on http://localhost:8000/mcp
```

Until the first PyPI release, `uv tool install git+https://github.com/justmytwospence/garmin-mcp` installs from the repository.

The mirror lives in your user data directory (`~/.local/share/garmin-mcp/garmin.duckdb` on Linux, `~/Library/Application Support/garmin-mcp/` on macOS, `%LOCALAPPDATA%\garmin-mcp\` on Windows) unless `GARMIN_DB_PATH` says otherwise; FIT files, the snapshot and DuckDB's spill files go next to it. The mirror is one file per process: run either the HTTP service or a stdio server against a given `GARMIN_DB_PATH`, not both. Downloads (`download_activity_file`, `download_course_gpx`) go where `GARMIN_FIT_DOWNLOAD_DIR` or the tool's `output_dir` points; in Docker that is `/data/downloads` inside the container, so copy them out with `docker compose cp garmin-mcp:/data/downloads ./downloads`.

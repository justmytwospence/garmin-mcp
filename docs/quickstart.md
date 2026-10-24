# Quickstart

You need Docker with Compose v2, a Garmin Connect account, and a way to receive its MFA code (Garmin emails one on most logins). Everything the server keeps lives in one named volume.

## 1. Start the server

```sh
git clone https://github.com/justmytwospence/garmin-mcp.git
cd garmin-mcp
cp .env.example .env
```

Open `.env` and set `MCP_BEARER_TOKEN` to a secret your MCP clients will send (`openssl rand -hex 32` makes a good one). Then:

```sh
docker compose up -d
```

The server listens on `http://localhost:8000/mcp` (Streamable HTTP) with a health check at `/health`. It starts without tokens: tools answer "not logged in", background sync waits.

## 2. Log in once

```sh
docker compose run --rm garmin-mcp garmin-mcp-auth
```

It asks for your Garmin Connect email and password, then for the MFA code Garmin sends, and saves the tokens into the volume (`/data/tokens/garmin_tokens.json`, owner-only). python-garminconnect refreshes them in place for months; the server never logs in on its own. Then restart so the server picks them up:

```sh
docker compose restart garmin-mcp
```

`docker compose run --rm garmin-mcp garmin-mcp-auth --check` verifies the stored tokens at any time. Accounts on Garmin Connect China set `GARMIN_IS_CN=true` in `.env` before both commands. Every way a login can fail is explained, with the command that fixes it, in [Operations](operations.md#login-problems).

## 3. Connect a client

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

[Clients](clients.md) has the equivalent for Claude Desktop (stdio), claude.ai (which needs the [OAuth profile](authentication.md#oauth-21-with-your-own-provider)), Cursor and pi.

## 4. Watch the first sync

Ask the client to call `garmin_sync_status`, or look at the log:

```sh
docker compose logs -f garmin-mcp
```

The first run loads the profile and lists every activity, then backfills newest first: the fast per-range daily summaries, then each day's datasets and each activity's detail and FIT file, then the slow snapshots. Requests go out one at a time, about 1.5 s apart, so a few years of history take about a week; the last months are there within hours and tools work from the first minute, fetching on demand what the mirror does not have yet. [How sync works](sync.md) explains the order and the pacing.

## 5. Optional: dashboards, notebooks, backups

Each is a compose profile over the read-only snapshot the server publishes:

```sh
docker compose --profile grafana --profile backup up -d
```

See [Extras](extras.md). To expose the server on the internet for claude.ai or a phone, read [Authentication](authentication.md) first.

## Without Docker

```sh
uv tool install git+https://github.com/justmytwospence/garmin-mcp   # or: uv sync in a checkout
garmin-mcp-auth                 # tokens go to ~/.garminconnect
GARMIN_DB_PATH=~/garmin/garmin.duckdb garmin-mcp   # stdio, for Claude Desktop and similar
MCP_TRANSPORT=http GARMIN_DB_PATH=~/garmin/garmin.duckdb MCP_BEARER_TOKEN=... garmin-mcp
```

The mirror is one file per process: run either the HTTP service or a stdio server against a given `GARMIN_DB_PATH`, not both.

# Clients

The server speaks MCP over Streamable HTTP at `/mcp` (the image's default) or over stdio when a client spawns it. Tool names are the ones of [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp), so a prompt or client configuration written for it works unchanged; `garmin_query`, `garmin_schema` and `garmin_sync_status` are new.

Replace `<token>` with your `MCP_BEARER_TOKEN` and `http://localhost:8000` with wherever the server runs. The [tool profiles](reference/tools.md#profiles) are worth a look: a client that only needs health data carries <!-- count:profile-health -->56<!-- /count --> tool definitions in its context instead of <!-- count:tools -->160<!-- /count -->.

## Claude Code

```sh
claude mcp add --transport http garmin http://localhost:8000/mcp \
  --header "Authorization: Bearer <token>"
```

Or in `.mcp.json` (project) or `~/.claude.json` (user):

```json
{
  "mcpServers": {
    "garmin": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

With the [OAuth profile](authentication.md#oauth-21-with-your-own-provider), drop the header and use the public URL; Claude Code opens the browser login on first use.

## Claude Desktop

Claude Desktop launches stdio servers. Install the package locally, log in, and point it at the mirror:

```sh
uv tool install git+https://github.com/justmytwospence/garmin-mcp
garmin-mcp-auth
```

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "garmin-mcp",
      "env": { "GARMIN_DB_PATH": "/Users/you/garmin/garmin.duckdb" }
    }
  }
}
```

Each launch opens the mirror, logs in from the stored tokens in the background, and runs the sync engine while Claude Desktop is open. Do not point a stdio server and the Docker service at the same file: DuckDB allows one writing process. To use the Docker service from Claude Desktop instead, add it as a remote server under Settings, Connectors (an HTTP URL with the OAuth profile), or run a local stdio bridge such as `mcp-remote` with the bearer header.

## claude.ai

claude.ai connects to remote servers over HTTPS and authenticates with OAuth, so it needs the [OAuth profile](authentication.md#oauth-21-with-your-own-provider) behind a TLS reverse proxy. Then, in Settings, Connectors, add a custom connector with `https://garmin.example.com/mcp`; the login runs through your provider. A static bearer token cannot be configured there.

## Cursor

`~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project):

```json
{
  "mcpServers": {
    "garmin": {
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

## pi

In `~/.pi/agent/settings.json` (or a project's `.pi/settings.json`), under `mcpServers`:

```json
{
  "mcpServers": {
    "garmin": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

pi exposes MCP servers to its codemode tool, so a session can batch several Garmin calls in one script.

## Anything else

Any MCP client that supports Streamable HTTP with a static header works the same way. For clients that only speak stdio, the `garmin-mcp` command is the stdio server (`MCP_TRANSPORT` defaults to `stdio` outside the image), and `mcp-remote` bridges stdio to the HTTP service.

## First questions to ask

- "What does `garmin_sync_status` say about coverage?" tells you how far the backfill has come.
- "Show my resting heart rate and HRV trend for the last 90 days" uses the mirror only.
- "Run `SELECT activity_type, count(*), round(sum(distance)/1000) AS km FROM activity WHERE deleted_at IS NULL GROUP BY 1 ORDER BY 3 DESC` with `garmin_query`" answers in milliseconds and costs no Garmin request.
- Every answer ends with a provenance line saying whether it came from the mirror (and how old it is), from Garmin just now, or is stale because Garmin could not be asked.

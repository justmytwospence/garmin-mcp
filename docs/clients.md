# Clients

The server speaks MCP over Streamable HTTP at `/mcp` (the image's default) or over stdio when a client spawns it. Tool names are the ones of [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp), so a prompt or client configuration written for it works unchanged; `garmin_query`, `garmin_schema` and `garmin_sync_status` are new.

Replace `<token>` with your `MCP_BEARER_TOKEN` and `http://localhost:8000` with wherever the server runs. The [tool profiles](reference/tools.md#profiles) are worth a look: a client that only needs health data carries <!-- count:profile-health -->56<!-- /count --> tool definitions in its context instead of <!-- count:tools -->160<!-- /count -->.

Two ways to connect, whatever the client:

- **Remote (Streamable HTTP).** The Docker service, reached with the bearer token or, through the [OAuth profile](authentication.md#oauth-21-with-your-own-provider), with a browser login. One mirror, shared by every client.
- **Local (stdio).** The client starts `garmin-mcp` itself. Install the package (`uv tool install garmin-mirror-mcp`) and log in once with `garmin-mcp-auth`; each launch opens the mirror and runs the sync while the client is open. Do not point a stdio server and the Docker service at the same `GARMIN_DB_PATH`: DuckDB allows one writing process.

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

With the OAuth profile, drop the header and use the public URL; Claude Code opens the browser login on first use.

## Claude Desktop

Claude Desktop runs local stdio servers from `claude_desktop_config.json` (Settings, Developer, *Edit Config*; `~/Library/Application Support/Claude/` on macOS, `%APPDATA%\Claude\` on Windows). It does not inherit your shell's `PATH`, so give the absolute path of the executable (`which garmin-mcp`, or `which uvx`):

```json
{
  "mcpServers": {
    "garmin": {
      "command": "/Users/you/.local/bin/garmin-mcp",
      "env": { "GARMIN_DB_PATH": "/Users/you/garmin/garmin.duckdb" }
    }
  }
}
```

Or without installing: `"command": "/Users/you/.local/bin/uvx", "args": ["--from", "garmin-mirror-mcp", "garmin-mcp"]`. Log in once beforehand with `garmin-mcp-auth` in a terminal (the tokens go to `~/.garminconnect`), then restart Claude Desktop.

To use the Docker service instead, add it under Settings, Connectors as a custom connector; that needs the public HTTPS URL and the OAuth profile, as for claude.ai below.

## claude.ai

claude.ai connects to remote servers over HTTPS and authenticates with OAuth, so it needs the [OAuth profile](authentication.md#oauth-21-with-your-own-provider) behind a TLS reverse proxy. Then, in Settings, Connectors, add a custom connector with `https://garmin.example.com/mcp`; the login runs through your provider. A static bearer token cannot be configured there.

## Cursor

`~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project):

```json
{
  "mcpServers": {
    "garmin": {
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer ${env:GARMIN_MCP_TOKEN}" }
    }
  }
}
```

`${env:NAME}` reads the token from your environment; a literal `"Bearer <token>"` works too. For the OAuth profile, use the public URL without `headers`; Cursor runs the browser login.

## VS Code (GitHub Copilot)

The *MCP: Add Server* command writes the entry for you. By hand, in the workspace's `.vscode/mcp.json` (or the user profile's `mcp.json`), under `servers`:

```json
{
  "servers": {
    "garmin": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer ${env:GARMIN_MCP_TOKEN}" }
    }
  }
}
```

The portable `.mcp.json` at the workspace root uses `mcpServers` instead, as the other clients do. VS Code's `${input:...}` prompts can hold the token instead of an environment variable.

## OpenAI Codex CLI

`~/.codex/config.toml` (or a trusted project's `.codex/config.toml`); the ChatGPT desktop app and the IDE extension share it:

```toml
[mcp_servers.garmin]
url = "http://localhost:8000/mcp"
bearer_token_env_var = "GARMIN_MCP_TOKEN"
```

`codex mcp add garmin --url http://localhost:8000/mcp` writes the entry; add the `bearer_token_env_var` line (or `http_headers = { "Authorization" = "Bearer <token>" }`) by hand. For a local stdio server, `command = "garmin-mcp"` with an optional `[mcp_servers.garmin.env]` table.

## ChatGPT

ChatGPT connects to remote servers only, over HTTPS, and authenticates with OAuth or not at all, so it needs the [OAuth profile](authentication.md#oauth-21-with-your-own-provider) behind a TLS reverse proxy. Turn on developer mode (Settings, Connectors, Advanced), then add a custom connector with `https://garmin.example.com/mcp` and OAuth as the authentication. Full MCP support in ChatGPT is still rolling out by plan; the server works like any other custom connector.

## Gemini CLI

`~/.gemini/settings.json` (user) or `.gemini/settings.json` (project):

```json
{
  "mcpServers": {
    "garmin": {
      "httpUrl": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

Or `gemini mcp add --transport http --header "Authorization: Bearer <token>" garmin http://localhost:8000/mcp`. Note the key is `httpUrl` (Streamable HTTP); `url` would mean the legacy SSE transport.

## Other clients

Any MCP client that supports Streamable HTTP with a static header works the same way; most use the `mcpServers` object with `url` and `headers`. For clients that only speak stdio, the `garmin-mcp` command is the stdio server (`MCP_TRANSPORT` defaults to `stdio` outside the image), and `mcp-remote` bridges stdio to the HTTP service with the bearer header.

### pi

In `~/.pi/agent/mcp.json` (user) or `.pi/mcp.json` (project), under `mcpServers`; `${NAME}` reads an environment variable:

```json
{
  "mcpServers": {
    "garmin": {
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer ${GARMIN_MCP_TOKEN}" },
      "description": "Garmin Connect: activities, sleep, training and the DuckDB mirror behind them"
    }
  }
}
```

Or `pi mcp add garmin --url http://localhost:8000/mcp --bearer-token-env-var GARMIN_MCP_TOKEN`, then `pi mcp list` to check the connection. pi exposes MCP servers to its codemode tool, so a session can batch several Garmin calls in one script; `description` is how it ranks the server's tools.
